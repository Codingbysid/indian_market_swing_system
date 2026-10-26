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
from research_v6.replay import BAND_ATR, STOP_ATR, isolate_path, simulate
from research_v6.residual_momentum import raw_momentum, residual_score, top_decile, window_bounds
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
    }


def build_catalog(study: dict, lam: float) -> dict[str, list[dict]]:
    """Raw and residual deciles are formed on the same eligible names for each paired comparison."""
    dates = study["dates"]
    averages = ema(study["nifty"]["close"], 50)
    raw_states = [ema50_state(float(price), float(average)) for price, average in zip(study["nifty"]["close"], averages)]
    held_states = hysteresis(raw_states)
    market = study["nifty"]["return"]
    buckets = {name: [] for name in ("A", "C_market_residual", "C_market_raw_matched", "C_sector_residual", "C_sector_raw_matched", "D_hysteresis", "H2")}
    for index, day in enumerate(dates):
        if window_bounds(index) is None:
            continue
        if index % 250 == 0:
            print(f"v6 signals {day}", flush=True)
        raw_scores, market_scores, sector_scores = {}, {}, {}
        for symbol, panel in study["panels"].items():
            if not np.isfinite(panel["turnover_prior"][index]) or panel["turnover_prior"][index] < 50_000_000:
                continue
            raw = raw_momentum(panel["return"], index)
            residual = residual_score(panel["return"], market, None, index, lam)
            if raw is not None:
                raw_scores[symbol] = raw
            if raw is not None and residual is not None:
                market_scores[symbol] = residual
            sector = study["sector_returns"].get(study["sector_of"].get(symbol))
            if sector is not None and raw is not None:
                sector_residual = residual_score(panel["return"], market, sector, index, lam)
                if sector_residual is not None:
                    sector_scores[symbol] = sector_residual
        groups = {
            "A": (raw_scores, "raw_momentum_h1", "regime"),
            "C_market_residual": (market_scores, "residual_momentum_market_only", "regime"),
            "C_market_raw_matched": ({symbol: raw_scores[symbol] for symbol in market_scores}, "raw_momentum_h1", "regime"),
            "C_sector_residual": (sector_scores, "residual_momentum_sector", "regime"),
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
                buckets[name].append(signal)
    return buckets


def choose_lambda(study: dict) -> dict:
    from research_v6.residual_momentum import prediction_errors
    totals = {value: [0.0, 0] for value in LAMBDAS}
    market = study["nifty"]["return"]
    print("v6 choosing residual ridge on training windows", flush=True)
    for index, day in enumerate(study["dates"]):
        if day > TRAIN_END or window_bounds(index) is None:
            continue
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
    return {"chosen": chosen, "attempts": ranked, "selection": "train_mean_square_prediction_error"}


def _labels(signals: list[dict], study: dict, policy: dict) -> list[dict]:
    rows = []
    for signal in signals:
        outcome = isolate_path(signal, study["bars"], study["dates"], policy)
        outcome["split"] = _split(signal["signal_date"], outcome.get("exit_date"), study["dates"]) if outcome.get("status") == "completed" else outcome.get("status")
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
        outcome = isolate_path(signal, study["bars"], dates, policy)
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


def run(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    policy = _policy()
    study = load_study(out / "sectors")
    lam = choose_lambda(study)
    frozen_lam = lam["chosen"] if lam["chosen"] is not None else 1.0
    catalogue = build_catalog(study, frozen_lam)
    labelled = {name: _labels(rows, study, policy) for name, rows in catalogue.items() if name in {"A", "C_market_residual", "C_sector_residual"}}
    train = [row for row in labelled["A"] if row.get("split") == "train" and row.get("net_r") is not None]
    calibration = [row for row in labelled["A"] if row.get("split") == "calibration" and row.get("net_r") is not None]
    bundle = _fit_models(train, calibration)
    reports = {}
    for name, rows in catalogue.items():
        test_rows = [row for row in rows if row["signal_date"] >= TEST_START]
        reports[name] = _account_report(test_rows, study, policy)
        paths = labelled.get(name, [])
        reports[name]["unit_path_status"] = {status: sum(row.get("status") == status for row in paths) for status in ("completed", "unfilled", "rejected", "censored")}
        reports[name]["unit_completed_test"] = sum(row.get("status") == "completed" and row.get("signal_date", "") >= TEST_START for row in paths)
    if bundle.get("status") == "fitted":
        test_completed = [row for row in labelled["A"] if row.get("split") == "test"]
        probability = _probability(bundle, test_completed) if test_completed else np.array([])
        labels = np.asarray([1.0 if row["net_pnl"] > 0 else 0.0 for row in test_completed])
        kept = []
        for signal in catalogue["A"]:
            if signal["signal_date"] < TEST_START:
                continue
            features, _ = design_matrix([signal["features"]], [signal["family"]])
            if bundle["selected"] == "prior":
                probability_signal = bundle["prior"]
            elif bundle["selected"].startswith("logistic"):
                probability_signal = float(predict_logistic(bundle["logistic"][float(bundle["selected"].split("_")[1])], features)[0])
            else:
                probability_signal = float(predict_xgboost(bundle["xgboost"], features)[0])
            if expected_net_r(probability_signal, signal["family"], bundle["payoffs"]) > 0:
                kept.append(signal)
        reports["B"] = _account_report(kept, study, policy)
        reports["B"]["test_brier"] = brier(labels, probability)
        reports["B"]["test_log_loss"] = log_loss(labels, probability)
        reports["B"]["selected_model"] = bundle["selected"]
        reports["B"]["m"] = bundle["m"]
        reports["B"]["calibration_attempts"] = bundle["attempts"]
        reports["B"]["dependence"] = bundle["dependence"]
        reports["B"]["experts"] = bundle["experts"]
        stress = _account_report(kept, study, policy, slippage_bps=25)
        reports["B_stress_25bps"] = {"net_pnl": stress["net_pnl"], "completed_fills": stress["completed_fills"], "admitted": stress["admitted"]}
    else:
        reports["B"] = bundle
    hmm_frame, hmm_dates, hmm_status = _hmm_observations(study)
    if hmm_status.get("status") == "BLOCKED":
        reports["E"] = hmm_status
    else:
        from research_v6.hmm_fit import causal_posteriors
        try:
            posts, blocks = causal_posteriors(hmm_frame)
            posterior_on = dict(zip(hmm_dates, posts))
            (out / "hmm_last_block.json").write_text(json.dumps(blocks[-1]["model"], indent=2))
            reports["E"] = {"status": "features_built", "blocks": len(blocks), "coverage": hmm_status, "model_hash": blocks[-1]["model_hash"], "note": "probabilities are research features; production RISK_OFF is unchanged"}
            if bundle.get("status") == "fitted":
                augmented_train = []
                for row in train:
                    posterior = posterior_on.get(row["signal_date"])
                    if posterior is None or not np.isfinite(posterior).all():
                        continue
                    features = dict(row["features"])
                    features["hmm_state_rank_0"] = float(posterior[0])
                    features["hmm_state_rank_1"] = float(posterior[1])
                    augmented_train.append({**row, "features": features})
                reports["E"]["trainable_rows"] = len(augmented_train)
                reports["E"]["dropped_without_causal_posterior"] = len(train) - len(augmented_train)
                augmented_cal = []
                for row in calibration:
                    posterior = posterior_on.get(row["signal_date"])
                    if posterior is None or not np.isfinite(posterior).all():
                        continue
                    features = dict(row["features"])
                    features["hmm_state_rank_0"] = float(posterior[0])
                    features["hmm_state_rank_1"] = float(posterior[1])
                    augmented_cal.append({**row, "features": features})
                if len(augmented_train) >= 30 and augmented_cal:
                    refit = _fit_models(augmented_train, augmented_cal)
                    reports["E"]["refit_status"] = refit.get("status")
                    reports["E"]["selected"] = refit.get("selected")
                    reports["E"]["calibration_attempts"] = refit.get("attempts")
        except Exception as exc:
            reports["E"] = {"status": "BLOCKED", "reason": str(exc), "coverage": hmm_status}
    reports["F"] = _earnings_study(study, policy, out)
    reports["G"] = _ou(study, policy)
    reports["fundamental"] = _fundamentals()
    payload = {
        "seed": SEED,
        "lambda": lam,
        "sample": {"start": study["dates"][0], "end": study["dates"][-1], "symbols": len(study["panels"]), "missing": study["missing"], "duplicate_dates": study["duplicate_dates"]},
        "data_status": "reused_development_cache_not_untouched_holdout",
        "provider": study["provider"],
        "adjustment": study["adjustment"],
        "sector_attempts": study["sector_attempts"],
        "experiments": reports,
        "kelly": False,
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
