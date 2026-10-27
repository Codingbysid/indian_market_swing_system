"""Run the frozen v6 matrix. One component changes at a time."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from research_v6 import LAUNCH_CASH, SEED
from research_v6.data import ROOT, UNIVERSE, _rolling_mean, load_study
from research_v6.meta_model import correlations, design_matrix, fit_logistic, fit_xgboost, predict_logistic, predict_xgboost
from research_v6.metrics import brier, log_loss, summarize_trades
from research_v6.ou import entry_allowed, fit_ou
from research_v6.regime_filter import ema, ema50_state, hysteresis
from research_v6.replay import BAND_ATR, STOP_ATR, gross_path, reference_trade, simulate
from research_v6.residual_momentum import raw_momentum, residual_pair, top_decile, window_bounds
from research_v6.shrinkage import expected_net_r, family_payoffs
from swing_core.fundamental_screen import RULES
from swing_v5.costs import fee_components, round_trip

POLICY_PATH = ROOT / "config" / "v5" / "research_policy.json"
LAMBDAS = (0.1, 1.0, 10.0)
PENALTIES = (10.0, 1.0)  # 1/C for C in {0.1, 1}
STRENGTHS = (10, 30, 60)
TRAIN_END = "2024-12-31"
CAL_END = "2025-06-30"
TEST_START = "2025-07-01"


def _policy() -> dict:
    return json.loads(POLICY_PATH.read_text())


def _split(signal_date: str, exit_date: str | None, sessions: list[str]) -> str:
    if exit_date is None:
        return "censored"
    if signal_date <= TRAIN_END:
        name, boundary = "train", "2025-01-01"
    elif signal_date <= CAL_END:
        name, boundary = "calibration", TEST_START
    else:
        return "test"
    if exit_date >= boundary:
        return "purged"
    signal_index = sessions.index(signal_date)
    boundary_index = next(index for index, day in enumerate(sessions) if day >= boundary)
    if boundary_index - signal_index <= 10:
        return "purged"
    return name


def _features(panel: dict, index: int, day: str, percentile: float) -> dict | None:
    atr = float(panel["atr20"][index])
    compression = float(panel["atr_compression"][index])
    impulse = float(panel["volume_impulse"][index])
    tail = float(panel["gap_tail"][index])
    close = float(panel["close"][index])
    if not all(np.isfinite(value) and value > 0 for value in (atr, close)) or not all(np.isfinite(value) for value in (compression, impulse, tail, percentile)):
        return None
    costs = fee_components(close, close, 1, entry_day=day, exit_day=day)["total"]
    return {
        "atr_compression": compression,
        "cost_over_risk": costs / (1.5 * atr),
        "volume_impulse": impulse,
        "rank_percentile": percentile,
        "adverse_gap_tail": tail,
    }


def _percentile(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    ordered = sorted(scores.values())
    size = len(ordered)
    out = {}
    for symbol, value in scores.items():
        below = sum(item < value for item in ordered)
        equal = sum(item == value for item in ordered)
        out[symbol] = (below + (equal + 1) / 2) / size
    return out


def _emit(panel: dict, index: int, day: str, score: float, percentile: float, family: str, regime_on: bool, hysteresis_on: bool, symbol: str) -> dict | None:
    features = _features(panel, index, day, percentile)
    if features is None:
        return None
    close = float(panel["close"][index])
    return {
        "symbol": symbol,
        "signal_date": day,
        "limit": close,
        "atr": float(panel["atr20"][index]),
        "family": family,
        "features": features,
        "score": score,
        "regime_on": regime_on,
        "hysteresis_on": hysteresis_on,
        "h2": bool(np.isfinite(panel["ema20"][index]) and close <= panel["ema20"][index] and panel["volume_impulse"][index] >= 1.2 and panel["atr_compression"][index] <= 1.0),
        "sector": None,
    }


def build_catalog(study: dict, lam: float) -> dict[str, list[dict]]:
    """Raw and residual deciles are formed on the same eligible names for each paired comparison."""
    dates = study["dates"]
    averages = ema(study["nifty"]["close"], 50)
    raw_states = [ema50_state(float(price), float(average)) for price, average in zip(study["nifty"]["close"], averages)]
    held_states = hysteresis(raw_states)
    market = study["nifty"]["return"]
    buckets = {name: [] for name in (
        "A", "C_market_sum", "C_market_vol", "C_market_raw_for_sum", "C_market_raw_for_vol",
        "C_sector_sum", "C_sector_raw_matched", "D_hysteresis", "H2",
    )}
    for index, day in enumerate(dates):
        if window_bounds(index) is None:
            continue
        if index % 250 == 0:
            print(f"v6 signals {day}", flush=True)
        raw_scores, sum_scores, vol_scores, sector_scores = {}, {}, {}, {}
        for symbol, panel in study["panels"].items():
            if not np.isfinite(panel["turnover_prior"][index]) or panel["turnover_prior"][index] < 50_000_000:
                continue
            raw = raw_momentum(panel["return"], index)
            pair = residual_pair(panel["return"], market, None, index, lam)
            if raw is not None:
                raw_scores[symbol] = raw
            if raw is not None and pair is not None:
                sum_scores[symbol] = pair["sum"]
                vol_scores[symbol] = pair["vol_normalized"]
            sector = study["sector_returns"].get(study["sector_of"].get(symbol))
            if sector is not None and raw is not None:
                sector_pair = residual_pair(panel["return"], market, sector, index, lam)
                if sector_pair is not None:
                    sector_scores[symbol] = sector_pair["sum"]
        groups = {
            "A": (raw_scores, "raw_momentum_h1", "regime"),
            "C_market_sum": (sum_scores, "residual_momentum_market_only", "regime"),
            "C_market_vol": (vol_scores, "residual_momentum_vol_normalized", "regime"),
            "C_market_raw_for_sum": ({symbol: raw_scores[symbol] for symbol in sum_scores}, "raw_momentum_h1", "regime"),
            "C_market_raw_for_vol": ({symbol: raw_scores[symbol] for symbol in vol_scores}, "raw_momentum_h1", "regime"),
            "C_sector_sum": (sector_scores, "residual_momentum_sector", "regime"),
            "C_sector_raw_matched": ({symbol: raw_scores[symbol] for symbol in sector_scores}, "raw_momentum_h1", "regime"),
            "D_hysteresis": (raw_scores, "raw_momentum_h1", "hysteresis"),
            "H2": (raw_scores, "raw_momentum_h2", "h2"),
        }
        regime_on = raw_states[index] == "RISK_ON"
        hysteresis_on = held_states[index] == "RISK_ON"
        for name, (scores, family, gate) in groups.items():
            ranks = _percentile(scores)
            for symbol in top_decile(scores):
                signal = _emit(study["panels"][symbol], index, day, scores[symbol], ranks[symbol], family, regime_on, hysteresis_on, symbol)
                if signal is None:
                    continue
                if gate == "regime" and not regime_on:
                    continue
                if gate == "hysteresis" and not hysteresis_on:
                    continue
                if gate == "h2" and (not regime_on or not signal["h2"]):
                    continue
                signal["sector"] = study["sector_of"].get(symbol)
                buckets[name].append(signal)
    return buckets


def choose_lambda(study: dict, train_end: str) -> dict:
    from research_v6.residual_momentum import prediction_errors
    totals = {value: [0.0, 0] for value in LAMBDAS}
    market = study["nifty"]["return"]
    first_date = None
    print(f"v6 choosing residual ridge through {train_end}", flush=True)
    for index, day in enumerate(study["dates"]):
        if day > train_end or window_bounds(index) is None:
            continue
        first_date = day if first_date is None else first_date
        for panel in study["panels"].values():
            for lam in LAMBDAS:
                errors = prediction_errors(panel["return"], market, None, index, lam)
                if errors is None or not np.isfinite(errors).all():
                    continue
                square = float(np.square(errors).mean())
                if not np.isfinite(square):
                    continue
                totals[lam][0] += square
                totals[lam][1] += 1
    ranked = [{"lambda": lam, "mean_square": None if count == 0 else total / count, "windows": count} for lam, (total, count) in totals.items()]
    usable = [row for row in ranked if row["mean_square"] is not None]
    chosen = min(usable, key=lambda row: (row["mean_square"], row["lambda"]))["lambda"] if usable else None
    return {
        "chosen": chosen,
        "attempts": ranked,
        "train_start": first_date,
        "train_end": train_end,
        "selection": "fold_training_mean_square_prediction_error",
    }


def _labels(signals: list[dict], study: dict, policy: dict) -> list[dict]:
    """Reference-budget outcomes. These are not admitted portfolio fills."""
    rows = []
    for signal in signals:
        outcome = reference_trade(signal, study["bars"], study["dates"], policy)
        outcome["features"] = signal.get("features") or {}
        outcome["split"] = _split(signal["signal_date"], outcome.get("exit_date"), study["dates"]) if outcome.get("status") == "completed" else outcome.get("status")
        outcome["label_population"] = "reference_initial_price_risk_5000_not_account_capital"
        rows.append(outcome)
    return rows


def _intents(result: dict) -> list[str]:
    dates = [row["signal_date"] for row in result["admitted"]]
    dates.extend(row["exit_date"] for row in result["completed"])
    return dates


def _account_report(signals: list[dict], study: dict, policy: dict, slippage_bps: float = 10.0) -> dict:
    test_sessions = [day for day in study["dates"] if day >= TEST_START]
    result = simulate(signals, study["bars"], test_sessions, policy, slippage_bps=slippage_bps)
    summary = summarize_trades(result["completed"], result["equity_curve"], result["occupied"], _intents(result), TEST_START, study["dates"][-1])
    summary.update({
        "candidates": len(signals),
        "admitted": len(result["admitted"]),
        "unfilled_or_rejected": len(result["rejected"]),
        "censored": len(result["censored"]),
        "open_positions": result["open_positions"],
        "slippage_bps": slippage_bps,
        "launch_cash": LAUNCH_CASH,
        "cash_after_completed": result["cash"],
        "account": "hypothetical_5748_restarted_at_outer_test",
    })
    return summary


def _fit_models(train_rows: list[dict], calibration_rows: list[dict]) -> dict:
    attempts = []
    if len(train_rows) < 30:
        return {"status": "BLOCKED", "reason": "fewer_than_30_training_trades", "attempts": attempts}
    families = [row["family"] for row in train_rows]
    features, names = design_matrix([row["features"] for row in train_rows], families)
    labels = np.asarray([1.0 if row["net_pnl"] > 0 else 0.0 for row in train_rows])
    if len(set(labels.tolist())) < 2:
        return {"status": "BLOCKED", "reason": "training_label_has_one_class", "attempts": attempts}
    dependence = correlations(features, names)
    experts = "not_attempted_below_200_family_trades"
    if len(train_rows) >= 200 and len(set(families)) > 1:
        experts = "pooled_only_until_each_family_has_200"
    prior = float(labels.mean())
    logistic = {lam: fit_logistic(features, labels, lam) for lam in PENALTIES}
    xgb = fit_xgboost(features, labels)
    cal_features, _ = design_matrix([row["features"] for row in calibration_rows], [row["family"] for row in calibration_rows]) if calibration_rows else (np.empty((0, features.shape[1])), names)
    cal_labels = np.asarray([1.0 if row["net_pnl"] > 0 else 0.0 for row in calibration_rows])
    candidates = [("prior", None, prior)]
    for lam, model in logistic.items():
        candidates.append((f"logistic_{lam}", model, None))
    if xgb is not None:
        candidates.append(("xgboost", xgb, None))
    best = None
    for name, model, constant in candidates:
        if name == "prior":
            probability = np.full(len(cal_labels), constant)
        elif name.startswith("logistic"):
            probability = predict_logistic(model, cal_features) if len(cal_labels) else np.array([])
        else:
            probability = predict_xgboost(model, cal_features) if len(cal_labels) else np.array([])
        score = brier(cal_labels, probability) if len(cal_labels) else None
        attempts.append({"model": name, "calibration_brier": score, "calibration_log_loss": log_loss(cal_labels, probability) if len(cal_labels) else None})
        rank = (score if score is not None else 9, 0 if name == "prior" else 1 if name.startswith("logistic") else 2, 0)
        if best is None or rank < best[0]:
            best = (rank, name, model, constant)
    table = None
    strength = min(STRENGTHS)
    if best is not None:
        net_r = [row["net_r"] for row in train_rows]
        try:
            table = family_payoffs(net_r, families, strength)
        except ValueError as exc:
            return {"status": "BLOCKED", "reason": str(exc), "attempts": attempts, "dependence": dependence}
    return {
        "status": "fitted",
        "selected": None if best is None else best[1],
        "selected_model": None if best is None else best[2],
        "prior": prior,
        "logistic": logistic,
        "xgboost": xgb,
        "feature_names": names,
        "payoffs": table,
        "m": strength,
        "attempts": attempts,
        "dependence": dependence,
        "experts": experts,
        "train_rows": len(train_rows),
        "calibration_rows": len(calibration_rows),
    }


def _probability(bundle: dict, rows: list[dict]) -> np.ndarray:
    features, _ = design_matrix([row["features"] for row in rows], [row["family"] for row in rows])
    name = bundle["selected"]
    if name == "prior":
        return np.full(len(rows), bundle["prior"])
    if name.startswith("logistic"):
        lam = float(name.split("_")[1])
        return predict_logistic(bundle["logistic"][lam], features)
    return predict_xgboost(bundle["xgboost"], features)


def _hmm_observations(study: dict) -> tuple[np.ndarray, list[str], dict]:
    dates = study["dates"]
    close = study["nifty"]["close"]
    log_return = np.full(len(dates), np.nan)
    log_return[1:] = np.log(close[1:] / np.r_[np.nan, close[:-1]][1:])
    span = np.log(study["nifty"]["high"] / study["nifty"]["low"])
    sma = {symbol: _rolling_mean(panel["close"], 50) for symbol, panel in study["panels"].items()}
    breadth = np.full(len(dates), np.nan)
    dispersion = np.full(len(dates), np.nan)
    kept_dates = []
    rows = []
    invalid = 0
    checked = 0
    for index, day in enumerate(dates):
        if not np.isfinite(log_return[index]) or not np.isfinite(span[index]) or span[index] <= 0:
            continue
        checked += 1
        present = 0
        above = 0
        returns = []
        for symbol, panel in study["panels"].items():
            price = panel["close"][index]
            if np.isfinite(price):
                present += 1
                average = sma[symbol][index]
                if np.isfinite(average) and price > average:
                    above += 1
            ret = panel["return"][index]
            if np.isfinite(ret):
                returns.append(ret)
        if present / study["denominator"] < 0.90 or len(returns) / study["denominator"] < 0.90:
            invalid += 1
            continue
        breadth_value = above / study["denominator"]
        dispersion_value = float(np.std(np.asarray(returns), ddof=1))
        kept_dates.append(day)
        rows.append([log_return[index], span[index], breadth_value, dispersion_value])
    status = {"checked_sessions": checked, "invalid_sessions": invalid, "invalid_fraction": None if checked == 0 else invalid / checked, "retained": len(rows), "denominator": study["denominator"]}
    if checked == 0 or status["invalid_fraction"] > 0.05:
        return np.empty((0, 4)), [], {**status, "status": "BLOCKED", "reason": "breadth_coverage_below_gate"}
    return np.asarray(rows, dtype=float), kept_dates, {**status, "status": "ok"}


def _earnings_study(study: dict, policy: dict, out: Path) -> dict:
    from collections import Counter
    from datetime import datetime
    from research_v6.earnings import parse_result_filings, reaction_measure, reaction_session
    archive = out / "earnings_archive"
    archive.mkdir(parents=True, exist_ok=True)
    destination = archive / "nse_response.bin"
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=01-07-2025&to_date=08-10-2026"
    if destination.exists():
        body = destination.read_bytes()
        fetched = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "cached": True, "url": url}
    else:
        import urllib.request
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "indian-swing-research-v6"}), timeout=30) as response:
                body = response.read()
            destination.write_bytes(body)
            fetched = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "cached": False, "url": url}
        except Exception as exc:
            return {"status": "BLOCKED", "reason": "announcement_fetch_failed", "fetch": {"error": type(exc).__name__, "url": url}}
    parsed = parse_result_filings(destination)
    if parsed.get("status") != "READY":
        return {**parsed, "fetch": fetched}
    dates = study["dates"]
    market = study["nifty"]["return"]
    outcomes = []
    counts = Counter()
    for filing in parsed["filings"]:
        panel = study["panels"].get(filing["symbol"])
        if panel is None:
            counts["symbol_not_in_price_panel"] += 1
            continue
        session = reaction_session(datetime.fromisoformat(filing["published_at"]), dates)
        if session not in dates:
            counts["reaction_session_unavailable"] += 1
            continue
        index = dates.index(session)
        previous = index - 1
        if previous < 60:
            counts["reaction_session_unavailable"] += 1
            continue
        stock = panel["return"][previous - 59:index + 1]
        factor = market[previous - 59:index + 1]
        if not np.isfinite(stock).all() or not np.isfinite(factor).all() or float(np.var(factor[:-1])) <= 1e-12:
            counts["reaction_session_unavailable"] += 1
            continue
        beta = float(np.cov(stock[:-1], factor[:-1], ddof=0)[0, 1] / np.var(factor[:-1]))
        atr = float(panel["atr20"][previous])
        close = float(panel["close"][previous])
        try:
            measure = reaction_measure(float(panel["return"][index]), float(market[index]), beta, atr, close)
        except ValueError:
            counts["reaction_session_unavailable"] += 1
            continue
        impulse = float(panel["volume_impulse"][index])
        if measure <= 0 or not np.isfinite(impulse) or impulse < 1.5:
            counts["reaction_not_confirmed"] += 1
            continue
        counts["positive_volume_reaction"] += 1
        anchor = float(panel["close"][index])
        band = float(panel["atr20"][index])
        if index + 3 >= len(dates) or not np.isfinite(band):
            counts["consolidation_incomplete"] += 1
            continue
        if any(not np.isfinite(panel["close"][index + step]) or abs(float(panel["close"][index + step]) - anchor) > band for step in (1, 2, 3)):
            counts["consolidation_failed"] += 1
            continue
        signal_index = index + 3
        signal = {
            "symbol": filing["symbol"], "signal_date": dates[signal_index], "family": "earnings_reaction",
            "limit": float(panel["close"][signal_index]), "atr": float(panel["atr20"][signal_index]), "features": {},
        }
        if not np.isfinite(signal["limit"]) or not np.isfinite(signal["atr"]) or signal["atr"] <= 0:
            counts["consolidation_incomplete"] += 1
            continue
        outcome = reference_trade(signal, study["bars"], dates, policy)
        outcome["reaction"] = measure
        outcome["admission"] = "BLOCKED_fundamental_snapshot_unavailable"
        outcomes.append(outcome)
        counts["confirmed"] += 1
        counts["confirmed_" + outcome.get("status", "unknown")] += 1
    completed = [row for row in outcomes if row.get("status") == "completed"]
    summary = summarize_trades(completed, [], [], [row["signal_date"] for row in completed], "2025-07-01", dates[-1])
    summary.update({
        "status": "reaction_measured_admission_blocked",
        "sue": False,
        "vwap": "daily_range_not_used",
        "clock": "nse_sort_date_interpreted_as_ist",
        "filings": parsed["result_filings"],
        "paths": dict(counts),
        "admission": "BLOCKED_fundamental_snapshot_unavailable",
        "fetch": fetched,
        "production": "disabled",
    })
    return summary


def _fundamentals() -> dict:
    store = json.loads((ROOT / "config" / "ops" / "fundamentals_snapshots.json").read_text())
    if not store.get("snapshots"):
        return {"status": "BLOCKED", "reason": "no_point_in_time_fundamental_history", "conjunction": "AND", "rules_hash": hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()}
    return {"status": "READY"}


def _ou_exit(signal: dict, panel: dict, market: np.ndarray, coef: np.ndarray, fit: dict, index: int, study: dict, policy: dict) -> dict:
    dates = study["dates"]
    atr = float(signal["atr"])
    limit = float(signal["limit"])
    worst = limit + BAND_ATR * atr
    allowance = float(policy["risk"]["max_cost_fraction_initial_price_risk"]) * STOP_ATR * atr
    if round_trip(worst, worst, 1, day=signal["signal_date"]) > allowance:
        return {"status": "rejected", "reason": "risk_or_cost", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    start = index + 1
    if start >= len(dates):
        return {"status": "censored", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    entry_bar = study["bars"].get(signal["symbol"], {}).get(dates[start])
    if entry_bar is None or entry_bar["open"] <= limit - STOP_ATR * atr or entry_bar["open"] > worst:
        return {"status": "unfilled" if entry_bar is not None else "rejected", "reason": "unfilled", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    entry = float(entry_bar["open"])
    stop = entry - STOP_ATR * atr
    stock = panel["return"]
    level_start = index - 250
    history = stock[level_start:index] - coef[0] - coef[1] * market[level_start:index]
    if not np.isfinite(history).all():
        return {"status": "rejected", "reason": "residual_history", "symbol": signal["symbol"], "signal_date": signal["signal_date"]}
    running = float(np.cumsum(history)[-1])
    held = 0
    for cursor in range(start, len(dates)):
        day = dates[cursor]
        current = study["bars"].get(signal["symbol"], {}).get(day)
        if current is None or not np.isfinite(stock[cursor]) or not np.isfinite(market[cursor]):
            return {"status": "censored", "symbol": signal["symbol"], "signal_date": signal["signal_date"], "entry_date": dates[start]}
        held += 1
        if current["low"] <= stop:
            price, reason = min(current["open"], stop), "stop"
        else:
            running += float(stock[cursor] - coef[0] - coef[1] * market[cursor])
            price = reason = None
            if running >= fit["mean"]:
                price, reason = current["close"], "residual_mean"
            elif held >= fit["vertical_sessions"]:
                price, reason = current["close"], "vertical"
        if price is None:
            continue
        costs = fee_components(entry, price, 1, entry_day=dates[start], exit_day=day)["total"]
        net = price - entry - costs
        risk = entry - stop
        return {
            "status": "completed", "symbol": signal["symbol"], "family": "ou_residual",
            "signal_date": signal["signal_date"], "entry_date": dates[start], "exit_date": day,
            "reason": reason, "qty": 1, "net_pnl": net, "net_r": net / risk if risk > 0 else None,
            "half_life": fit["half_life"], "vertical_policy": fit["vertical_sessions"],
        }
    return {"status": "censored", "symbol": signal["symbol"], "signal_date": signal["signal_date"], "entry_date": dates[start]}


def _ou(study: dict, policy: dict) -> dict:
    dates = study["dates"]
    market = study["nifty"]["return"]
    outcomes = []
    rejected = {}
    for symbol, panel in study["panels"].items():
        stock = panel["return"]
        for index, day in enumerate(dates):
            if day < TEST_START or index < 520:
                continue
            fit_start, fit_stop = index - 500, index - 250
            level_start = index - 250
            design = np.column_stack([np.ones(250), market[fit_start:fit_stop]])
            target = stock[fit_start:fit_stop]
            if not np.isfinite(design).all() or not np.isfinite(target).all():
                continue
            coef, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
            window_market = market[level_start:index + 1]
            window_stock = stock[level_start:index + 1]
            if not np.isfinite(window_market).all() or not np.isfinite(window_stock).all():
                continue
            residual = window_stock - coef[0] - coef[1] * window_market
            level = np.cumsum(residual)
            fit = fit_ou(level[:-1])
            if not fit.get("valid"):
                rejected[fit.get("reason", "invalid")] = rejected.get(fit.get("reason", "invalid"), 0) + 1
                continue
            if not entry_allowed(float(level[-2]), fit):
                continue
            signal = {
                "symbol": symbol,
                "signal_date": day,
                "limit": float(panel["close"][index]),
                "atr": float(panel["atr20"][index]),
                "family": "ou_residual",
                "features": {},
            }
            if not np.isfinite(signal["atr"]) or signal["atr"] <= 0:
                continue
            outcome = _ou_exit(signal, panel, market, coef, fit, index, study, policy)
            outcomes.append(outcome)
            rejected[outcome.get("status", "unknown")] = rejected.get(outcome.get("status", "unknown"), 0) + 1
    completed = [row for row in outcomes if row.get("status") == "completed"]
    summary = summarize_trades(completed, [], [], [row["signal_date"] for row in completed], TEST_START, dates[-1])
    summary.update({"status": "research_only", "production": "disabled", "entries": len(outcomes), "rejections": rejected, "exit": "stop_or_residual_mean_or_fixed_vertical"})
    return summary


FOLDS = (
    {"name": "fold_2023h2", "train_end": "2022-12-31", "cal_start": "2023-01-01", "cal_end": "2023-06-30", "test_start": "2023-07-01", "test_end": "2023-12-31"},
    {"name": "fold_2024h2", "train_end": "2023-12-31", "cal_start": "2024-01-01", "cal_end": "2024-06-30", "test_start": "2024-07-01", "test_end": "2024-12-31"},
    {"name": "fold_2025", "train_end": "2024-12-31", "cal_start": "2025-01-01", "cal_end": "2025-06-30", "test_start": "2025-07-01", "test_end": "2026-10-08"},
)


def _in_test(row: dict, fold: dict) -> bool:
    return fold["test_start"] <= row["signal_date"] <= fold["test_end"]


def _gross_summary(signals: list[dict], study: dict) -> dict:
    paths = [gross_path(signal, study["bars"], study["dates"]) for signal in signals]
    done = [row for row in paths if row.get("status") == "completed"]
    returns = [row["gross_return"] for row in done]
    return {
        "population": "gross_signal_diagnostic",
        "candidates": len(signals),
        "completed": len(done),
        "unfilled": sum(row.get("status") == "unfilled" for row in paths),
        "censored": sum(row.get("status") == "censored" for row in paths),
        "invalid": sum(row.get("status") == "invalid" for row in paths),
        "mean_gross_return": float(np.mean(returns)) if returns else None,
        "mean_mae_r": float(np.mean([row["mae_r"] for row in done])) if done else None,
        "mean_mfe_r": float(np.mean([row["mfe_r"] for row in done])) if done else None,
        "time_exits": sum(row.get("reason") == "vertical" for row in done),
        "note": "hypothetical price paths, not recommendations and not a profitability claim",
    }


def _reference_paths(signals: list[dict], study: dict, policy: dict, start: str, end: str) -> list[dict]:
    return [reference_trade(signal, study["bars"], study["dates"], policy) for signal in signals if start <= signal["signal_date"] <= end]


def _exposure(paths: list[dict], sessions: list[str]) -> dict:
    done = [row for row in paths if row.get("status") == "completed" and row.get("notional") is not None]
    if not done:
        return {"trades": 0}
    notionals = np.asarray([row["notional"] for row in done], dtype=float)
    debits = np.asarray([row["debit"] for row in done], dtype=float)
    load: dict[str, float] = {}
    for row in done:
        for day in sessions:
            if row["entry_date"] <= day <= row["exit_date"]:
                load[day] = load.get(day, 0.0) + float(row["notional"])
    return {
        "trades": len(done),
        "median_notional": float(np.median(notionals)),
        "p90_notional": float(np.quantile(notionals, 0.9)),
        "max_notional": float(notionals.max()),
        "median_debit": float(np.median(debits)),
        "p90_debit": float(np.quantile(debits, 0.9)),
        "max_debit": float(debits.max()),
        "max_overlapping_notional": max(load.values()) if load else 0.0,
        "note": "₹5000 is initial price risk per independent reference trade, not ticket size or account capital. Overlapping notionals are not a funded portfolio.",
    }


def _reference_summary(signals: list[dict], study: dict, policy: dict, start: str, end: str) -> dict:
    paths = _reference_paths(signals, study, policy, start, end)
    done = [row for row in paths if row.get("status") == "completed" and row.get("net_r") is not None]
    summary = summarize_trades(done, [], [], [row["exit_date"] for row in done], start, end)
    summary.update({
        "population": "reference_initial_price_risk_5000_not_account_capital",
        "candidates": len(signals),
        "completed": len(done),
        "reference_quantity_zero": sum(row.get("status") == "reference_quantity_zero" for row in paths),
        "unfilled": sum(row.get("status") == "unfilled" for row in paths),
        "censored": sum(row.get("status") == "censored" for row in paths),
        "account_would_reject": sum(bool(row.get("account_failed")) for row in done),
        "exposure": _exposure(paths, study["dates"]),
        "note": "costed at the declared initial-price-risk quantity; not admitted by the ₹5,748 account and not a funded sum",
    })
    return summary


def _rejection_counts(result: dict) -> dict:
    counts = {}
    for row in result["rejected"]:
        counts[row.get("reason", "unknown")] = counts.get(row.get("reason", "unknown"), 0) + 1
    return counts


def _account_window(signals: list[dict], study: dict, policy: dict, start: str, end: str) -> dict:
    sessions = [day for day in study["dates"] if start <= day <= end]
    result = simulate([row for row in signals if start <= row["signal_date"] <= end], study["bars"], sessions, policy)
    summary = summarize_trades(result["completed"], result["equity_curve"], result["occupied"], _intents(result), start, end)
    summary.update({
        "population": "frozen_5748_account",
        "candidates": sum(start <= row["signal_date"] <= end for row in signals),
        "admitted": len(result["admitted"]),
        "rejection_reasons": _rejection_counts(result),
        "latched": result["latched"],
        "not_modeled": result["not_modeled"],
        "replay_scope": result["replay_scope"],
        "missing_price_marks": result["missing_price_marks"],
        "note": "partial account replay; zero admitted orders is not an alpha estimate",
    })
    return summary


def _fold_split(signal_date: str, exit_date: str | None, sessions: list[str], fold: dict) -> str:
    if exit_date is None:
        return "censored"
    if signal_date <= fold["train_end"]:
        boundary = fold["cal_start"]
        name = "train"
    elif fold["cal_start"] <= signal_date <= fold["cal_end"]:
        boundary = fold["test_start"]
        name = "calibration"
    elif fold["test_start"] <= signal_date <= fold["test_end"]:
        return "test"
    else:
        return "outside"
    if exit_date >= boundary:
        return "purged"
    signal_index = sessions.index(signal_date)
    boundary_index = next(index for index, day in enumerate(sessions) if day >= boundary)
    if boundary_index - signal_index <= 10:
        return "purged"
    return name


def _layer(signals: list[dict], study: dict, policy: dict, start: str, end: str) -> dict:
    chosen = [row for row in signals if start <= row["signal_date"] <= end]
    return {
        "gross": _gross_summary(chosen, study),
        "reference": _reference_summary(chosen, study, policy, start, end),
        "account": _account_window(chosen, study, policy, start, end),
    }


EXIT_CONVENTION = (
    "A completed trade is assigned by its signal date. Train and calibration rows whose exit "
    "is on or after the next boundary are purged, as are signals within 10 sessions of that "
    "boundary. A test row keeps its own exit, which may fall up to 10 sessions after test_end. "
    "Those later prices are not training rows and are not test rows of an earlier fold."
)


def select_test_rows(rows: list[dict], sessions: list[str], fold: dict) -> list[dict]:
    """Test observations for one fold. The stored global split is ignored."""
    chosen = []
    for row in rows:
        if row.get("status") != "completed":
            continue
        if _fold_split(row["signal_date"], row.get("exit_date"), sessions, fold) != "test":
            continue
        if not (fold["test_start"] <= row["signal_date"] <= fold["test_end"]):
            continue
        chosen.append(row)
    return chosen


def _meta_for_fold(signals: list[dict], study: dict, policy: dict, fold: dict) -> dict:
    labelled = _labels(signals, study, policy)
    sessions = study["dates"]
    train = [row for row in labelled if _fold_split(row["signal_date"], row.get("exit_date"), sessions, fold) == "train" and row.get("net_r") is not None]
    calibration = [row for row in labelled if _fold_split(row["signal_date"], row.get("exit_date"), sessions, fold) == "calibration" and row.get("net_r") is not None]
    bundle = _fit_models(train, calibration)
    bundle["label_population"] = "reference_initial_price_risk_5000_not_account_capital"
    bundle["exit_convention"] = EXIT_CONVENTION
    if bundle.get("status") != "fitted":
        return bundle
    test_rows = select_test_rows(labelled, sessions, fold)
    probability = _probability(bundle, test_rows) if test_rows else np.array([])
    labels = np.asarray([1.0 if row["net_pnl"] > 0 else 0.0 for row in test_rows])
    return {
        "status": "fitted",
        "label_population": bundle["label_population"],
        "exit_convention": EXIT_CONVENTION,
        "selected_model": bundle["selected"],
        "m": bundle["m"],
        "train_rows": bundle["train_rows"],
        "calibration_rows": bundle["calibration_rows"],
        "test_rows": len(test_rows),
        "test_signal_start": None if not test_rows else min(row["signal_date"] for row in test_rows),
        "test_signal_end": None if not test_rows else max(row["signal_date"] for row in test_rows),
        "calibration_attempts": bundle["attempts"],
        "test_brier": brier(labels, probability),
        "test_log_loss": log_loss(labels, probability),
        "experts": bundle["experts"],
        "dependence": bundle["dependence"],
        "note": "probabilities describe reference trades, not admitted ₹5,748 fills",
    }


def _suffix_frame(study: dict, dates: list[str]) -> np.ndarray | None:
    index_of = {day: index for index, day in enumerate(study["dates"])}
    sma = {symbol: _rolling_mean(panel["close"], 50) for symbol, panel in study["panels"].items()}
    rows = []
    nifty = study["nifty"]
    for day in dates:
        index = index_of[day]
        previous = nifty["close"][index - 1] if index else np.nan
        if not np.isfinite(previous) or previous <= 0 or not np.isfinite(nifty["close"][index]) or nifty["low"][index] <= 0:
            return None
        log_return = float(np.log(nifty["close"][index] / previous))
        span = float(np.log(nifty["high"][index] / nifty["low"][index]))
        usable = 0
        returns = []
        above = 0
        for symbol, panel in study["panels"].items():
            close = panel["close"][index]
            ret = panel["return"][index]
            if np.isfinite(close) and np.isfinite(sma[symbol][index]) and np.isfinite(ret):
                usable += 1
                returns.append(ret)
                if close > sma[symbol][index]:
                    above += 1
        if usable / 150 < 0.90 or len(returns) / 150 < 0.90 or not np.isfinite(span) or span <= 0:
            return None
        rows.append([log_return, span, above / 150, float(np.std(returns, ddof=1))])
    return np.asarray(rows, dtype=float)


def _paired_sector(sector_paths: list[dict], raw_paths: list[dict]) -> dict:
    """Difference of weekly mean net R. Separate intervals are not this comparison."""
    from research_v6.metrics import SEED as metric_seed
    def grouped(paths: list[dict]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for row in paths:
            if row.get("status") != "completed" or row.get("net_r") is None:
                continue
            week = datetime.fromisoformat(row["signal_date"]).strftime("%G-W%V")
            out.setdefault(week, []).append(float(row["net_r"]))
        return out
    left = grouped(sector_paths)
    right = grouped(raw_paths)
    common = sorted(set(left) & set(right))
    if len(common) < 2:
        return {"status": "insufficient_common_weeks", "weeks": len(common), "variants": ["sector_sum_minus_matched_raw"]}
    weekly = {week: float(np.mean(left[week]) - np.mean(right[week])) for week in common}
    rng = np.random.default_rng(metric_seed)
    draws = []
    for _ in range(400):
        chosen = rng.choice(common, size=len(common), replace=True)
        draws.append(float(np.mean([weekly[week] for week in chosen])))
    low, mid, high = np.quantile(draws, [0.025, 0.5, 0.975])
    sectors = sorted({row.get("sector") for row in sector_paths if row.get("sector")})
    leave = {}
    for sector in sectors:
        reduced_left = grouped([row for row in sector_paths if row.get("sector") != sector])
        reduced_right = grouped([row for row in raw_paths if row.get("sector") != sector])
        shared = sorted(set(reduced_left) & set(reduced_right))
        if not shared:
            leave[sector] = None
            continue
        leave[sector] = float(np.mean([np.mean(reduced_left[week]) - np.mean(reduced_right[week]) for week in shared]))
    weights: dict[str, float] = {}
    for row in sector_paths:
        if row.get("status") == "completed":
            weights[row["symbol"]] = weights.get(row["symbol"], 0.0) + abs(float(row["net_pnl"]))
    total = sum(weights.values())
    leader = max(weights, key=weights.get) if weights else None
    return {
        "status": "exploratory_reused_cache",
        "comparison": "sector_residual_sum_minus_matched_raw",
        "common_weeks": len(common),
        "difference_mean_net_r": float(np.mean(list(weekly.values()))),
        "low": float(low),
        "mid": float(mid),
        "high": float(high),
        "interval_includes_zero": bool(low <= 0 <= high),
        "leave_one_sector_out_point": leave,
        "names": len(weights),
        "sectors": sectors,
        "largest_name_share": None if leader is None or total <= 0 else {"symbol": leader, "share": weights[leader] / total},
        "overlapping_positions": "each reference path is an independent overlapping hypothetical, not one funded account; the bootstrap keeps every name from a signal week inside the same block, while holds that cross weeks can still be split",
        "separate_intervals_are_not_a_difference_test": True,
    }


def _hmm_report(study: dict, catalogue: dict, policy: dict, out: Path) -> dict:
    from research_v6.coverage import audit_coverage
    from research_v6.hmm_fit import HMMFitFailed, causal_posteriors
    coverage = audit_coverage(study)
    report = {
        "status_if_unfitted": "NOT EVALUATED",
        "full_window": {key: coverage[key] for key in ("full_window_status", "full_window_reason", "invalid_fraction", "invalid_sessions", "failure_counts", "denominator")},
    }
    if coverage["suffix_status"] != "ok":
        report["suffix"] = {"status": "NOT EVALUATED", "reason": "no_contiguous_valid_suffix", "rule": coverage["suffix_rule"]}
        return report
    dates = [day for day in study["dates"] if coverage["suffix_start"] <= day <= coverage["suffix_end"]]
    built = _suffix_frame(study, dates)
    if built is None:
        report["suffix"] = {"status": "NOT EVALUATED", "reason": "suffix_observation_missing", "window": [dates[0], dates[-1]]}
        return report
    try:
        posts, blocks = causal_posteriors(built)
    except HMMFitFailed as exc:
        report["suffix"] = {
            "status": "NOT EVALUATED",
            "reason": str(exc),
            "failed_observation": exc.index,
            "window": [dates[0], dates[-1]],
            "attempts": exc.attempts,
            "missing_posteriors_filled": False,
        }
        return report
    (out / "hmm_suffix_model.json").write_text(json.dumps({key: value for key, value in blocks[-1]["model"].items() if key != "diagnostics"}, indent=2))
    posterior_on = {day: posts[index] for index, day in enumerate(dates)}
    usable_dates = {day for day, posterior in posterior_on.items() if posterior is not None and np.isfinite(posterior).all()}
    baseline = [row for row in catalogue["A"] if row["signal_date"] in usable_dates]
    gated = [row for row in baseline if int(np.argmax(posterior_on[row["signal_date"]])) == 2]
    enriched = []
    for row in baseline:
        posterior = posterior_on[row["signal_date"]]
        features = dict(row.get("features") or {})
        features["hmm_state_rank_0"] = float(posterior[0])
        features["hmm_state_rank_1"] = float(posterior[1])
        enriched.append({**row, "features": features})
    report["suffix"] = {
        "status": "evaluated",
        "window": [dates[0], dates[-1]],
        "post_warmup_signals": len(baseline),
        "warmup_excluded": "signals before the first causal posterior are omitted from both sides",
        "model_hash": blocks[-1]["model_hash"],
        "baseline_on_post_warmup_dates": _layer(baseline, study, policy, dates[0], dates[-1]),
        "argmax_state_gate": _layer(gated, study, policy, dates[0], dates[-1]),
        "argmax_is_a_separate_experiment": True,
        "posterior_features_are_the_planned_meta_model_inputs": True,
        "production_risk_off": "unchanged",
    }
    report["posterior_meta_model"] = _meta_for_fold(enriched, study, policy, FOLDS[-1])
    return report


def run(out: Path, *, refresh_sectors: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    policy = _policy()
    study = load_study(out / "sectors", refresh_sectors=refresh_sectors)
    folds = {}
    lambda_report = {}
    sector_comparison = {}
    for fold in FOLDS:
        selected = choose_lambda(study, fold["train_end"])
        lambda_report[fold["name"]] = selected
        catalogue = build_catalog(study, selected["chosen"] if selected["chosen"] is not None else 1.0)
        folds[fold["name"]] = {
            name: _layer(rows, study, policy, fold["test_start"], fold["test_end"])
            for name, rows in catalogue.items()
        }
        folds[fold["name"]]["meta_model"] = _meta_for_fold(catalogue["A"], study, policy, fold)
        sector_paths = _reference_paths(catalogue["C_sector_sum"], study, policy, fold["test_start"], fold["test_end"])
        raw_paths = _reference_paths(catalogue["C_sector_raw_matched"], study, policy, fold["test_start"], fold["test_end"])
        sector_comparison[fold["name"]] = _paired_sector(sector_paths, raw_paths)
        if fold["name"] == FOLDS[-1]["name"]:
            last_catalogue = catalogue
    from research_v6.replay import account_feasibility
    feasibility = []
    last = FOLDS[-1]
    for signal in last_catalogue["A"]:
        if not (last["test_start"] <= signal["signal_date"] <= last["test_end"]):
            continue
        feasibility.append(account_feasibility(signal, policy, LAUNCH_CASH, LAUNCH_CASH, 0.0, 0.0, set(), False))
    import csv
    with (out / "feasibility_fold_2025_raw.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["symbol", "signal_date", "quantity", "debit", "price_risk", "fees", "gap_allowance", "failed"])
        writer.writeheader()
        for row in feasibility:
            writer.writerow({**row, "failed": "; ".join(row["failed"])})
    payload = {
        "seed": SEED,
        "lambda_by_fold": lambda_report,
        "folds_definition": list(FOLDS),
        "exit_convention": EXIT_CONVENTION,
        "sample": {"start": study["dates"][0], "end": study["dates"][-1], "symbols": len(study["panels"]), "missing": study["missing"], "duplicate_dates": study["duplicate_dates"]},
        "data_status": "reused_development_cache_not_untouched_holdout",
        "price_provider": study["provider"],
        "price_adjustment": study["adjustment"],
        "sector_provider": "yahoo_chart_close",
        "sector_adjustment": "yahoo_close_not_the_same_convention_as_tvdatafeed_ohlc",
        "sector_attempts": study["sector_attempts"],
        "experiments": folds,
        "sector_paired_comparison": sector_comparison,
        "hmm": _hmm_report(study, last_catalogue, policy, out),
        "earnings": _earnings_study(study, policy, out),
        "ou": _ou(study, policy),
        "fundamental": _fundamentals(),
        "feasibility_rows": len(feasibility),
        "feasibility_zero_quantity": sum(row["quantity"] == 0 for row in feasibility),
        "kelly": False,
        "replay_scope": "partial_account_replay_correlation_and_theme_caps_not_enforced",
        "conclusion_limit": "account infeasibility is not an alpha estimate; gross and reference results are not a funded portfolio",
    }
    (out / "results.json").write_text(json.dumps(payload, indent=2, default=str))
    return payload


def write_baseline(out: Path, tests_passed: int) -> dict:
    policy_bytes = POLICY_PATH.read_text().encode()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": commit,
        "seed": SEED,
        "python": platform.python_version(),
        "tests_passed_before_v6_experiments": tests_passed,
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "policy_version": json.loads(policy_bytes)["version"],
        "universe_sha256": hashlib.sha256(UNIVERSE.read_bytes()).hexdigest(),
        "launch_cash": LAUNCH_CASH,
        "launch_cash_meaning": "hypothetical_research_capital_not_broker_cash",
        "v4_reference_blob": "34ce678:backtest/artifacts/v4/metrics.json",
        "v4_reference_note": "Not recomputed. Event file is gitignored. Figures are prior.brier 0.2381792265884577, logistic 0.2656782919220534, xgb 0.2672852116782442, rules pnl -1294.5809383599656, profit factor 0.6473721639990169.",
        "v3_metrics_are_a_different_study": True,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline.json").write_text(json.dumps(record, indent=2))
    return record
