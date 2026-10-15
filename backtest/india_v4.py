"""Frozen, exploratory India150 study. Never promotes or writes a production model.
python -m backtest.india_v4 --config config/research_v4.json
"""

from __future__ import annotations
import argparse, hashlib, json, math, time
from collections import Counter
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import brier_score_loss, log_loss
from xgboost import XGBClassifier
from backtest.splits import fold_windows, session_map, split_fold
from swing_core.india import (
    FEATURE_NAMES,
    SCHEMA_ID,
    FAMILIES,
    clean_bars,
    build_panel,
    feature_row,
)
from swing_core.labels import label_triple_barrier
from swing_core.risk_v4 import size_trade, kelly_evidence, pair_correlation
from swing_core.scoring import score_model
from swing_core.costs import entry_charges, round_trip_charges

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "backtest/artifacts/v4"
XGB = dict(
    n_estimators=160,
    max_depth=2,
    learning_rate=0.03,
    min_child_weight=50,
    reg_lambda=20,
    reg_alpha=1,
    gamma=1,
    subsample=0.8,
    colsample_bytree=0.8,
    objective="binary:logistic",
    eval_metric="logloss",
    random_state=41,
    n_jobs=2,
)


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, allow_nan=False, default=str) + "\n")


def load_data(config):
    meta = pd.read_csv(ROOT / "universes/india_150.csv").fillna("")
    frames = {}
    fail = []
    for s in meta.Symbol:
        p = ROOT / config["data_dir"] / f"NSE_{s}.csv"
        if not p.exists():
            fail.append({"symbol": s, "reason": "missing"})
            continue
        d = clean_bars(pd.read_csv(p), as_of=config["as_of"])
        if len(d) < 300:
            fail.append({"symbol": s, "reason": "short"})
            continue
        frames[s] = d
    nifty = clean_bars(
        pd.read_csv(ROOT / config["data_dir"] / "NSE_NIFTY.csv"), as_of=config["as_of"]
    )
    if len(nifty) < 500 or nifty.index[-1] != pd.Timestamp(config["as_of"]):
        raise ValueError("Nifty stale or insufficient")
    if len(frames) < 135:
        raise ValueError(f"Insufficient panel: {len(frames)}/150; requires 90%")
    return meta, frames, nifty, fail


def make_events(panel, config):
    rows = []
    counts = Counter()
    for symbol, d in panel.items():
        for i in np.flatnonzero(d.family.notna().to_numpy()):
            if i < config["warmup"]:
                continue
            row = d.iloc[i]
            counts[str(row.family)] += 1
            # Fixed repair-budget reference; dynamic replay resizes again before every alert/fill.
            trade = size_trade(
                float(row.Close),
                float(row.ATR),
                15255.50,
                121733.40,
                gap_atr=max(0.5, float(row.downside_gap95_60)),
            )
            record = {
                "date": str(d.index[i].date()),
                "symbol": symbol,
                "family": row.family,
                "regime": row.regime,
                "sector": row.sector,
                "signal_price": float(row.Close),
                "entry_atr": float(row.ATR),
                "gap_atr": float(row.downside_gap95_60),
                "qty": trade["qty"],
                "status": "unaffordable",
                "label": None,
                "exit_date": None,
            }
            if trade["qty"] > 0:
                features = feature_row(row, trade["qty"])
                if not all(math.isfinite(v) for v in features.values()):
                    record["status"] = "invalid_features"
                else:
                    record.update(features)
                    result = label_triple_barrier(
                        d,
                        i,
                        qty=trade["qty"],
                        horizon=config["horizon_sessions"],
                        stop_mult=config["stop_atr"],
                        target_mult=config["target_atr"],
                        entry_band_atr=config["entry_band_atr"],
                    )
                    record.update(
                        {
                            "status": (
                                "filled" if result.label is not None else result.reason
                            ),
                            "label": result.label,
                            "net_r": result.net_r,
                            "gross_r": result.gross_r,
                            "net_inr": result.net_inr,
                            "fees": result.fees,
                            "entry_price": result.entry_price,
                            "exit_price": result.exit_price,
                            "bars_held": result.bars_held,
                            "reason": result.reason,
                            "cost_R": features["cost_R"],
                            "entry_date": (
                                str(d.index[i + 1].date()) if i + 1 < len(d) else None
                            ),
                            "exit_date": (
                                str(d.index[i + result.bars_held].date())
                                if result.bars_held
                                else None
                            ),
                        }
                    )
            rows.append(record)
    return pd.DataFrame(rows), dict(counts)


def export_native(model, scaler=None):
    if scaler is not None:
        return {
            "type": "logistic",
            "feature_names": FEATURE_NAMES,
            "coefficients": model.coef_[0].tolist(),
            "intercept": float(model.intercept_[0]),
            "mean": scaler.mean_.tolist(),
            "scale": scaler.scale_.tolist(),
        }
    b = model.get_booster()
    cfg = json.loads(b.save_config())
    base = cfg["learner"]["learner_model_param"]["base_score"]
    base = float(str(base).strip("[]").split(",")[0])
    return {
        "type": "xgboost_dump",
        "feature_names": FEATURE_NAMES,
        "base_score": base,
        "trees": [json.loads(t) for t in b.get_dump(dump_format="json")],
    }


def fit_models(train, calib, selection):
    x = train[FEATURE_NAMES]
    y = train.label.astype(int)
    # Balance dates/names without changing the posterior class prior.
    day_counts = train.groupby("date").date.transform("size")
    name_counts = train.groupby("symbol").symbol.transform("size")
    weight = 1 / np.sqrt(day_counts * name_counts)
    weight = weight / weight.mean()
    scaler = StandardScaler().fit(x)
    logit = LogisticRegression(C=0.1, max_iter=1500, random_state=41)
    logit.fit(scaler.transform(x), y, sample_weight=weight)
    xgb = XGBClassifier(**XGB)
    xgb.fit(x, y, sample_weight=weight)
    models = {
        "prior": {
            "type": "constant_prior",
            "feature_names": FEATURE_NAMES,
            "probability": float((y.sum() + 1) / (len(y) + 2)),
        },
        "logistic": export_native(logit, scaler),
        "xgb": export_native(xgb),
    }
    calibrators = {}
    for name in ("logistic", "xgb"):
        raw = (
            logit.predict_proba(scaler.transform(calib[FEATURE_NAMES]))[:, 1]
            if name == "logistic"
            else xgb.predict_proba(calib[FEATURE_NAMES])[:, 1]
        )
        raw = np.clip(raw, 1e-7, 1 - 1e-7)
        z = np.log(raw / (1 - raw))[:, None]
        platt = LogisticRegression(C=1, max_iter=1000).fit(z, calib.label.astype(int))
        calibrators[name] = platt
        models[name]["calibrator"] = {
            "type": "platt_logit",
            "a": float(platt.coef_[0, 0]),
            "b": float(platt.intercept_[0]),
        }
    losses = {}
    for name, payload in models.items():
        p = np.array(
            [
                score_model(payload, r)
                for r in selection[FEATURE_NAMES].to_dict("records")
            ]
        )
        losses[name] = {
            "brier": float(brier_score_loss(selection.label.astype(int), p)),
            "logloss": float(log_loss(selection.label.astype(int), p, labels=[0, 1])),
        }
    # Selection uses held-out calibration loss, never predicted return or outer-test outcomes.
    chosen = min(
        models,
        key=lambda name: (
            losses[name]["brier"],
            ["prior", "logistic", "xgb"].index(name),
        ),
    )
    parity = {}
    sample = selection[FEATURE_NAMES].iloc[:100]
    for name, native in [("logistic", logit), ("xgb", xgb)]:
        raw = native.predict_proba(
            scaler.transform(sample) if name == "logistic" else sample
        )[:, 1]
        rawpayload = {k: v for k, v in models[name].items() if k != "calibrator"}
        walker = np.array(
            [score_model(rawpayload, r) for r in sample.to_dict("records")]
        )
        parity[name] = {
            "max_raw_probability_gap": float(np.max(np.abs(raw - walker))),
            "passed": bool(np.max(np.abs(raw - walker)) < 1e-5),
        }
        z = np.log(np.clip(raw, 1e-7, 1 - 1e-7) / (1 - np.clip(raw, 1e-7, 1 - 1e-7)))[
            :, None
        ]
        native_calibrated = calibrators[name].predict_proba(z)[:, 1]
        json_calibrated = np.array(
            [score_model(models[name], r) for r in sample.to_dict("records")]
        )
        parity[name]["max_calibrated_gap"] = float(
            np.max(np.abs(native_calibrated - json_calibrated))
        )
        parity[name]["passed"] &= parity[name]["max_calibrated_gap"] < 1e-5
    b = xgb.get_booster()
    for tree in models["xgb"]["trees"]:
        if "split" not in tree:
            continue
        t = np.float32(tree["split_condition"])
        name = tree["split"]
        gaps = []
        for value in (
            np.nextafter(t, np.float32(-np.inf)),
            t,
            np.nextafter(t, np.float32(np.inf)),
        ):
            row = sample.iloc[0].copy()
            row[name] = float(value)
            expected = float(
                xgb.predict_proba(pd.DataFrame([row], columns=FEATURE_NAMES))[0, 1]
            )
            actual = score_model(
                {k: v for k, v in models["xgb"].items() if k != "calibrator"},
                row.to_dict(),
            )
            gaps.append(abs(expected - actual))
        parity["xgb"]["boundary_gap"] = max(gaps)
        parity["xgb"]["passed"] &= max(gaps) < 1e-5
        break
    return models, chosen, losses, parity, b


def walk_forward(events, sessions, config):
    filled = (
        events[events.status.eq("filled")]
        .dropna(subset=FEATURE_NAMES + ["exit_date", "net_r"])
        .copy()
    )
    filled["date"] = pd.to_datetime(filled.date)
    filled["exit_date"] = pd.to_datetime(filled.exit_date)
    windows = fold_windows(
        filled.date.min(),
        pd.Timestamp(config["as_of"]) - pd.Timedelta(days=14),
        train_months=config["train_months"],
        calib_months=config["calibration_months"],
        test_months=config["test_months"],
    )
    smap = session_map(sessions)
    rows = []
    folds = []
    exports = {}
    last_booster = None
    for fid, w in enumerate(windows):
        parts = split_fold(filled, w, smap, embargo_sessions=config["embargo_sessions"])
        train = parts["train"]
        cal = parts["calib"]
        test = parts["test"]
        if min(len(train), len(cal), len(test)) < 100 or train.label.nunique() < 2:
            continue
        dates = sorted(cal.date.unique())
        boundary = pd.Timestamp(dates[len(dates) // 2])
        boundary_i = sessions.searchsorted(boundary)
        cut = sessions[max(0, boundary_i - config["embargo_sessions"])]
        calfit = cal[(cal.date < boundary) & (cal.exit_date < cut)]
        selection = cal[cal.date >= boundary]
        if min(len(calfit), len(selection)) < 40 or calfit.label.nunique() < 2:
            continue
        models, chosen, losses, parity, booster = fit_models(train, calfit, selection)
        evidence = {}
        payoff = {}
        for fam in FAMILIES:
            # Family economics conditional on the live regime, fitted before the outer test.
            data = train[train.family.eq(fam) & train.regime.eq("RISK_ON")]
            evidence[fam] = kelly_evidence(
                data.net_r, weeks=data.date.dt.to_period("W").astype(str)
            )
            wins = data[data.label == 1].net_r
            losses_r = -data[data.label == 0].net_r
            payoff[fam] = {
                "win_R": float(wins.mean()) if len(wins) else 0.0,
                "loss_R": float(losses_r.mean()) if len(losses_r) else 1.0,
                "n": len(data),
            }
        exports[str(fid)] = {
            "models": models,
            "chosen": chosen,
            "payoff": payoff,
            "kelly": evidence,
        }
        for name, payload in models.items():
            payload.update(
                {
                    "schema_id": SCHEMA_ID,
                    "promoted": False,
                    "allowed_live_families": [],
                    "training_cutoff": str(train.exit_date.max().date()),
                    "calibration_cutoff": str(selection.exit_date.max().date()),
                }
            )
        for row in test.to_dict("records"):
            record = {**row, "fold": fid, "chosen_model": chosen}
            for name, payload in models.items():
                record["p_" + name] = score_model(payload, row)
            record["p_selected"] = record["p_" + chosen]
            pp = payoff[row["family"]]
            record["expected_R"] = (
                record["p_selected"] * pp["win_R"]
                - (1 - record["p_selected"]) * pp["loss_R"]
            )
            rows.append(record)
        folds.append(
            {
                **w,
                "id": fid,
                "train": len(train),
                "calibration_fit": len(calfit),
                "calibration_selection": len(selection),
                "test": len(test),
                "chosen": chosen,
                "selection_metrics": losses,
                "parity": parity,
                "kelly": evidence,
            }
        )
        last_booster = booster
    oof = pd.DataFrame(rows)
    if oof.empty:
        raise ValueError("No supported purged outer folds")
    return oof, folds, exports, last_booster


def activity_metrics(intents, sessions):
    counts = pd.Series(0, index=sessions, dtype=int)
    for e in intents:
        stamp = pd.Timestamp(e["date"])
        if stamp in counts:
            counts.loc[stamp] += 1
    ten = counts.rolling(10, min_periods=10).sum().dropna()
    daily = counts.reindex(
        pd.date_range(sessions.min(), sessions.max(), freq="D"), fill_value=0
    )
    fortnight = daily.rolling(14, min_periods=14).sum().dropna()
    return {
        "unique_buy_or_sell_intents": len(intents),
        "sessions": len(sessions),
        "rolling_10_session_windows": len(ten),
        "fraction_windows_at_least_2": float(ten.ge(2).mean()) if len(ten) else None,
        "median_intents_per_10_sessions": float(ten.median()) if len(ten) else None,
        "zero_intent_window_fraction": float(ten.eq(0).mean()) if len(ten) else None,
        "fraction_14_calendar_day_windows_at_least_2": (
            float(fortnight.ge(2).mean()) if len(fortnight) else None
        ),
        "median_intents_per_14_calendar_days": (
            float(fortnight.median()) if len(fortnight) else None
        ),
        "definition": "One admitted BUY and one exit per system lot. Repeated reminders, WATCH and target touches excluded. Overlapping windows are descriptive, not independent samples.",
    }


def replay(
    oof,
    exports,
    panel,
    sessions,
    *,
    mode="selected",
    use_kelly=True,
    block_defence_psu=True,
):
    start = pd.Timestamp(oof.date.min())
    end = pd.Timestamp(oof.date.max())
    calendar = sessions[(sessions >= start) & (sessions <= end)]
    cash = 15255.50
    legacy_equity = 106477.90
    positions = {}
    orders = []
    fills = []
    intents = []
    curve = []
    reject = Counter()
    held_symbols = {"HAL", "ZENTEC", "CDSL", "INFY", "BPCL"}
    sector_cap_blocked = {
        "Capital Goods"
    }  # current HAL/ZENTEC breach; legacy valuation frozen
    bydate = {pd.Timestamp(k): v for k, v in oof.groupby("date")}
    for day in calendar:
        # Opening exits are processed first; intraday proceeds cannot finance opening entries.
        def close(symbol, price, reason):
            nonlocal cash
            p = positions.pop(symbol)
            q = p["qty"]
            fee = round_trip_charges(p["entry"], price, q) - entry_charges(
                p["entry"], q
            )
            cash += q * price - fee
            net = q * (price - p["entry"]) - fee - p["entry_fee"]
            r = net / (q * 1.5 * p["atr"])
            fills.append(
                {
                    **p,
                    "exit_date": str(day.date()),
                    "exit_price": price,
                    "reason": reason,
                    "net_inr": net,
                    "net_r": r,
                }
            )
            intents.append(
                {
                    "date": str(day.date()),
                    "symbol": symbol,
                    "side": "SELL",
                    "lot_id": p["lot_id"],
                }
            )

        for symbol, p in list(positions.items()):
            r = panel[symbol].loc[day]
            if not np.isfinite(r.Open):
                continue
            if r.Open <= p["stop"]:
                close(symbol, float(r.Open), "stop_gap")
            elif r.Open >= p["target"]:
                close(symbol, p["target"], "target_gap")
        for order in orders:
            symbol = order["symbol"]
            r = panel[symbol].loc[day]
            px = float(r.Open)
            if (
                not math.isfinite(px)
                or abs(px - order["signal_price"]) > 0.5 * order["atr"]
            ):
                reject["unfilled_gap"] += 1
                continue
            q = order["qty"]
            fee = entry_charges(px, q)
            debit = q * px + fee
            if debit > cash + 1e-8 or symbol in positions:
                reject["entry_cash_or_duplicate"] += 1
                continue
            cash -= debit
            positions[symbol] = {
                **order,
                "entry": px,
                "entry_date": str(day.date()),
                "entry_fee": fee,
                "stop": px - 1.5 * order["atr"],
                "target": px + 3 * order["atr"],
                "entry_i": sessions.get_loc(day),
            }
        orders = []
        for symbol, p in list(positions.items()):
            r = panel[symbol].loc[day]
            if not np.isfinite([r.Low, r.High, r.Close]).all():
                continue
            if r.Low <= p["stop"]:
                close(symbol, p["stop"], "stop")
            elif r.High >= p["target"]:
                close(symbol, p["target"], "target")
            elif sessions.get_loc(day) - p["entry_i"] + 1 >= 10:
                close(symbol, float(r.Close), "vertical")
        marked = sum(
            p["qty"] * float(panel[s].loc[day, "Close"]) for s, p in positions.items()
        )
        equity = legacy_equity + cash + marked
        curve.append(
            {
                "date": str(day.date()),
                "cash": cash,
                "system_mtm": marked,
                "synthetic_total_equity": equity,
                "cash_sleeve_equity": cash + marked,
            }
        )
        candidates = bydate.get(day)
        if candidates is None or day == calendar[-1]:
            continue
        ranked = []
        for row in candidates.to_dict("records"):
            symbol = row["symbol"]
            source = panel[symbol].loc[day]
            if row["regime"] != "RISK_ON":
                reject["regime"] += 1
                continue
            if (
                symbol in {"HAL", "ZENTEC", "CDSL", "INFY", "BPCL"}
                or symbol in positions
            ):
                reject["held_no_add"] += 1
                continue
            if block_defence_psu and source.get("repair_blocked", False):
                reject["theme_block"] += 1
                continue
            if source.sector in sector_cap_blocked:
                reject["legacy_sector_breach"] += 1
                continue
            legacy_corr = [
                (
                    pair_correlation(
                        panel[symbol].loc[:day, "Close"], panel[h].loc[:day, "Close"]
                    )
                    if h in panel
                    else None
                )
                for h in held_symbols
            ]
            if any(c is None or c >= 0.65 for c in legacy_corr):
                reject["legacy_correlation"] += 1
                continue
            exp = exports[str(row["fold"])]
            e = exp["kelly"][row["family"]]
            frac = e["quarter_kelly"] if use_kelly else None
            q = size_trade(
                float(source.Close),
                float(source.ATR),
                cash,
                equity,
                gap_atr=max(0.5, float(source.downside_gap95_60)),
                risk_fraction=frac,
            )
            if q["qty"] == 0:
                reject[q["reason"]] += 1
                continue
            feats = feature_row(source, q["qty"])
            name = exp["chosen"] if mode == "selected" else mode
            probability = score_model(exp["models"][name], feats)
            pay = exp["payoff"][row["family"]]
            edge = probability * pay["win_R"] - (1 - probability) * pay["loss_R"]
            if mode != "rules" and edge <= 0:
                reject["nonpositive_edge"] += 1
                continue
            ranked.append(
                (
                    edge if mode != "rules" else float(source.momentum_rank),
                    symbol,
                    row,
                    q,
                )
            )
        budget_cash = cash
        reserved = 0.0
        reserved_risk = sum(p["stress_risk"] for p in positions.values())
        sectors = {p["sector"] for p in positions.values()}
        for _, symbol, row, q in sorted(
            ranked, reverse=True, key=lambda r: (r[0], r[1])
        ):
            source = panel[symbol].loc[day]
            if len(positions) + len(orders) >= 2:
                reject["slots"] += 1
                continue
            if source.sector in sectors:
                reject["same_sector"] += 1
                continue
            correlations = [
                pair_correlation(
                    panel[symbol].loc[:day, "Close"], panel[p].loc[:day, "Close"]
                )
                for p in list(positions) + [o["symbol"] for o in orders]
            ]
            if any(c is None or c >= 0.65 for c in correlations):
                reject["correlation"] += 1
                continue
            # Reserve maximum admissible entry price, not yesterday's close.
            maxpx = float(source.Close) + 0.5 * float(source.ATR)
            exp = exports[str(row["fold"])]
            frac = exp["kelly"][row["family"]]["quarter_kelly"] if use_kelly else None
            trade = size_trade(
                maxpx,
                float(source.ATR),
                budget_cash,
                equity,
                gap_atr=max(0.5, float(source.downside_gap95_60)),
                risk_fraction=frac,
                remaining_risk=min(0.02 * cash, 0.003 * equity) - reserved_risk,
                ticket_cap=min(
                    0.25 * cash, 0.03 * equity, (cash * 0.8 * 2 / 3) - reserved
                ),
            )
            if trade["qty"] == 0:
                reject[trade["reason"]] += 1
                continue
            if trade["cost_R"] > 0.30:
                reject["cost_R"] += 1
                continue
            if mode != "rules":
                name = exp["chosen"] if mode == "selected" else mode
                prob = score_model(
                    exp["models"][name], feature_row(source, trade["qty"])
                )
                pay = exp["payoff"][row["family"]]
                if (
                    prob is None
                    or prob * pay["win_R"] - (1 - prob) * pay["loss_R"] <= 0
                ):
                    reject["resized_nonpositive_edge"] += 1
                    continue
            order = {
                "symbol": symbol,
                "qty": trade["qty"],
                "signal_price": float(source.Close),
                "signal_date": str(day.date()),
                "atr": float(source.ATR),
                "family": row["family"],
                "sector": source.sector,
                "stress_risk": trade["stress_risk"],
                "lot_id": str(day.date()) + ":" + symbol,
            }
            orders.append(order)
            sectors.add(source.sector)
            reserved += trade["debit"]
            budget_cash -= trade["debit"]
            reserved_risk += trade["stress_risk"]
            # Count actionable alerts even if a later gap prevents a fill; report fills separately.
            next_session = sessions[sessions.get_loc(day) + 1]
            intents.append(
                {
                    "date": str(next_session.date()),
                    "symbol": symbol,
                    "side": "BUY",
                    "lot_id": order["lot_id"],
                }
            )
    curve = pd.DataFrame(curve)
    equ = curve.cash_sleeve_equity
    dd = float((equ / equ.cummax() - 1).min())
    gains = sum(f["net_inr"] for f in fills if f["net_inr"] > 0)
    loss = -sum(f["net_inr"] for f in fills if f["net_inr"] < 0)
    report = {
        "mode": mode,
        "kelly_enabled": use_kelly,
        "completed_fills": len(fills),
        "open_positions_at_end": len(positions),
        "initial_cash": 15255.50,
        "final_cash_sleeve_equity": float(equ.iloc[-1]),
        "net_completed_pnl": sum(f["net_inr"] for f in fills),
        "max_cash_sleeve_drawdown": dd,
        "profit_factor": gains / loss if loss > 0 else None,
        "expectancy_R": float(np.mean([f["net_r"] for f in fills])) if fills else None,
        "rejections": dict(reject),
        "activity": activity_metrics(intents, calendar),
        "interpretation": "Counterfactual current-cash repair sleeve. Legacy valuation frozen, not the historical user book; no future exits credited at the final date.",
    }
    return report, pd.DataFrame(fills), curve, intents


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/research_v4.json")
    a = p.parse_args()
    t = time.perf_counter()
    config = json.loads((ROOT / a.config).read_text())
    ART.mkdir(parents=True, exist_ok=True)
    meta, frames, nifty, fail = load_data(config)
    panel, market = build_panel(
        frames, nifty, meta, min_turnover=config["liquidity_rupees"]
    )
    tags = meta.set_index("Symbol").Tags.to_dict()
    for s, d in panel.items():
        d["repair_blocked"] = any(
            tag in str(tags.get(s, "")).split("|") for tag in ("defence", "psu")
        )
    events, hits = make_events(panel, config)
    events.to_csv(ART / "events.csv", index=False)
    oof, folds, exports, booster = walk_forward(events, nifty.index, config)
    oof.to_csv(ART / "oof_predictions.csv", index=False)
    metrics = {
        "schema_id": SCHEMA_ID,
        "promoted": False,
        "as_of": config["as_of"],
        "universe_size": len(meta),
        "history_available": len(frames),
        "missing_or_short": fail,
        "detector_hits": hits,
        "event_status": events.status.value_counts().to_dict(),
        "outer_folds": len(folds),
        "unique_oos_candidates": len(oof),
        "oos_names": int(oof.symbol.nunique()),
        "folds": folds,
        "replay": {},
        "prediction_metrics": {},
        "family_economics": {},
        "bias": "Current membership/selection. Reused v3 history makes this exploratory. No historical sentiment is backfilled.",
    }
    for name in ["prior", "logistic", "xgb", "selected"]:
        prob = oof["p_" + name]
        metrics["prediction_metrics"][name] = {
            "brier": float(brier_score_loss(oof.label.astype(int), prob)),
            "logloss": float(log_loss(oof.label.astype(int), prob, labels=[0, 1])),
        }
    for fam, g in oof.groupby("family"):
        on = g[g.regime == "RISK_ON"]
        metrics["family_economics"][fam] = {
            "oos_candidates": len(g),
            "risk_on_candidates": len(on),
            "risk_on_mean_R": float(on.net_r.mean()) if len(on) else None,
            "kelly_diagnostic": kelly_evidence(
                on.net_r, weeks=pd.to_datetime(on.date).dt.to_period("W").astype(str)
            ),
        }
    # Rules diagnostic substitutes ranking only, keeping all cash/risk/regime gates.
    rules_exports = {
        k: {**e, "models": {**e["models"], "rules": e["models"]["prior"]}}
        for k, e in exports.items()
    }
    for mode, use in [("rules", False), ("selected", False), ("selected", True)]:
        report, fills, curve, intents = replay(
            oof, rules_exports, panel, nifty.index, mode=mode, use_kelly=use
        )
        key = mode + ("_kelly" if use else "_fixed_risk")
        metrics["replay"][key] = report
        fills.to_csv(ART / f"{key}_fills.csv", index=False)
        curve.to_csv(ART / f"{key}_equity.csv", index=False)
        write_json(ART / f"{key}_intents.json", intents)
    # Current forward-shadow fit is separate from the final historical outer fold.
    filled = (
        events[events.status.eq("filled")]
        .dropna(subset=FEATURE_NAMES + ["exit_date", "net_r"])
        .copy()
    )
    filled["date"] = pd.to_datetime(filled.date)
    filled["exit_date"] = pd.to_datetime(filled.exit_date)
    boundary = pd.Timestamp(config["as_of"]) - pd.DateOffset(months=6)
    boundary_i = nifty.index.searchsorted(boundary)
    purge_date = nifty.index[max(0, boundary_i - config["embargo_sessions"])]
    train = filled[(filled.date < boundary) & (filled.exit_date < purge_date)]
    cal = filled[
        (filled.date >= boundary) & (filled.exit_date < pd.Timestamp(config["as_of"]))
    ]
    split = pd.Timestamp(sorted(cal.date.unique())[len(cal.date.unique()) // 2])
    si = nifty.index.searchsorted(split)
    fit = cal[
        (cal.date < split)
        & (cal.exit_date < nifty.index[max(0, si - config["embargo_sessions"])])
    ]
    selection = cal[cal.date >= split]
    final_models, chosen, selection_metrics, final_parity, final_booster = fit_models(
        train, fit, selection
    )
    payoff = {}
    evidence = {}
    for fam in FAMILIES:
        group = train[train.family.eq(fam) & train.regime.eq("RISK_ON")]
        wins = group[group.label == 1].net_r
        losses = -group[group.label == 0].net_r
        payoff[fam] = {
            "win_R": float(wins.mean()) if len(wins) else 0.0,
            "loss_R": float(losses.mean()) if len(losses) else 1.0,
            "n": len(group),
        }
        evidence[fam] = kelly_evidence(
            group.net_r, weeks=group.date.dt.to_period("W").astype(str)
        )
    last = {
        "models": final_models,
        "chosen": chosen,
        "payoff": payoff,
        "kelly": evidence,
    }
    candidate = {
        **last["models"][last["chosen"]],
        "chosen_model": last["chosen"],
        "kelly": last["kelly"],
        "payoff": last["payoff"],
        "entry_band_atr": 0.5,
        "stop_atr": 1.5,
        "target_atr": 3.0,
        "horizon_sessions": 10,
        "feature_contract": "swing_core.india.FEATURE_NAMES",
        "promotion_required": [
            "fresh_forward_shadow",
            "positive_costed_lower_bound",
            "native_and_calibrated_parity",
            "execution_0925_proxy_validation",
            "reviewed_sentiment_overlay",
            "Lambda_full_scan_budget",
        ],
        "allowed_live_families": [],
        "promoted": False,
    }
    candidate.update(
        schema_id=SCHEMA_ID,
        training_cutoff=str(train.exit_date.max().date()),
        calibration_cutoff=str(selection.exit_date.max().date()),
        source_vendor=(
            "yahoo_chart_research_only"
            if (ART / "research_fetch.json").exists()
            else "tvDatafeed"
        ),
        live_data_parity_verified=False,
        role="current_forward_shadow_fit_not_outer_fold_performance",
    )
    write_json(ART / "model.candidate.json", candidate)
    booster.save_model(ART / "model.last_fold.xgb.json")
    final_booster.save_model(ART / "model.forward_shadow.xgb.json")
    metrics["forward_shadow_fit"] = {
        "chosen": chosen,
        "train": len(train),
        "calibration_fit": len(fit),
        "calibration_selection": len(selection),
        "selection_metrics": selection_metrics,
        "parity": final_parity,
        "live_data_parity_verified": False,
        "out_of_sample_evaluation": "Starts after this frozen fit; not available yet.",
    }
    write_json(ART / "fold_models.json", exports)
    market.to_csv(ART / "regime_history.csv")
    metrics["runtime_seconds"] = time.perf_counter() - t
    write_json(ART / "metrics.json", metrics)
    source = (
        json.loads((ART / "research_fetch.json").read_text())["source"]
        if (ART / "research_fetch.json").exists()
        else "tvDatafeed"
    )
    manifest = {
        "as_of": config["as_of"],
        "source": source,
        "files": [
            {
                "symbol": s,
                "sha256": hashlib.sha256(
                    (ROOT / config["data_dir"] / f"NSE_{s}.csv").read_bytes()
                ).hexdigest(),
                "bars": len(d),
                "last_complete_session": str(d.index[-1].date()),
            }
            for s, d in frames.items()
        ],
        "nifty_sha256": hashlib.sha256(
            (ROOT / config["data_dir"] / "NSE_NIFTY.csv").read_bytes()
        ).hexdigest(),
        "protocol_sha256": hashlib.sha256((ROOT / a.config).read_bytes()).hexdigest(),
    }
    write_json(ART / "data_manifest.json", manifest)
    print(
        json.dumps(
            {
                k: v
                for k, v in metrics.items()
                if k not in ("folds", "family_economics")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
