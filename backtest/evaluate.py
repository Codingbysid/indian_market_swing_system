"""Score a finished research run. Does not invent results when inputs are missing.

python -m backtest.evaluate --run backtest/artifacts/v3
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.portfolio_replay import replay_portfolio
from swing_core.tags import tags_for


def _brier(y, p) -> float:
    return float(np.mean((p - y) ** 2))


def _logloss(y, p) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _profit_factor(r: np.ndarray) -> float | None:
    wins = r[r > 0].sum()
    losses = -r[r < 0].sum()
    if losses <= 0:
        return None
    return float(wins / losses)


def _drawdown(equity: list[dict]) -> float:
    if not equity:
        return 0.0
    peak = -1e18
    worst = 0.0
    for row in equity:
        peak = max(peak, row["equity"])
        if peak > 0:
            worst = min(worst, row["equity"] / peak - 1.0)
    return float(worst)


def _bootstrap(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"weeks": 0, "p05": None, "p50": None, "p95": None}
    work = frame.copy()
    work["week"] = pd.to_datetime(work["date"]).dt.to_period("W-FRI").astype(str)
    blocks = [group["net_r"].to_numpy() for _, group in work.groupby("week")]
    if len(blocks) < 8:
        return {"weeks": len(blocks), "p05": None, "p50": None, "p95": None, "reason": "fewer_than_8_week_blocks"}
    rng = np.random.default_rng(0)
    means = []
    for _ in range(400):
        pick = rng.integers(0, len(blocks), len(blocks))
        sample = np.concatenate([blocks[i] for i in pick])
        means.append(float(sample.mean()))
    arr = np.array(means)
    return {
        "weeks": len(blocks),
        "p05": float(np.quantile(arr, 0.05)),
        "p50": float(np.quantile(arr, 0.50)),
        "p95": float(np.quantile(arr, 0.95)),
    }


def _weekly(decisions: pd.DataFrame, fills: list[dict]) -> dict:
    if decisions.empty:
        return {"weeks": 0, "zero_trade_weeks": None}
    start = pd.to_datetime(decisions["date"]).min()
    end = pd.to_datetime(decisions["date"]).max()
    weeks = pd.period_range(start, end, freq="W-FRI")
    fill_weeks = pd.to_datetime(pd.Series([row["date"] for row in fills])).dt.to_period("W-FRI") if fills else pd.Series(dtype="period[W-FRI]")
    counts = fill_weeks.value_counts() if len(fill_weeks) else pd.Series(dtype=int)
    weekly_counts = [int(counts.get(week, 0)) for week in weeks]
    arr = np.array(weekly_counts) if weekly_counts else np.array([0])
    return {
        "weeks": int(len(weeks)),
        "zero_trade_weeks": int((arr == 0).sum()),
        "zero_trade_week_fraction": float((arr == 0).mean()) if len(arr) else None,
        "median_fills": float(np.median(arr)),
        "lower_quartile_fills": float(np.quantile(arr, 0.25)),
    }


def _family_report(oof: pd.DataFrame, support: dict) -> dict:
    out = {}
    for family, group in oof.groupby("primary_family"):
        y = group["label"].to_numpy()
        reasons = []
        if len(group) < int(support["min_fills"]):
            reasons.append("family_fills")
        if int((y == 1).sum()) < int(support["min_positive"]):
            reasons.append("family_positives")
        if int((y == 0).sum()) < int(support["min_negative"]):
            reasons.append("family_negatives")
        if group["symbol"].nunique() < int(support["min_names"]):
            reasons.append("family_names")
        fold_means = group.groupby("fold")["net_r"].mean()
        stable = bool((fold_means > 0).mean() >= 0.6) if len(fold_means) else False
        if not stable:
            reasons.append("family_fold_stability")
        out[str(family)] = {
            "unique_fills": int(len(group)),
            "names": int(group["symbol"].nunique()),
            "positive": int((y == 1).sum()),
            "negative": int((y == 0).sum()),
            "folds": int(group["fold"].nunique()),
            "mean_net_r": float(group["net_r"].mean()),
            "profit_factor": _profit_factor(group["net_r"].to_numpy()),
            "mean_hold_sessions": None,
            "status": "watch",
            "failed_gates": reasons,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    run = Path(args.run)
    if not run.is_absolute():
        run = ROOT / run
    oof_path = run / "oof_predictions.csv"
    events_path = run / "events.csv"
    if not oof_path.exists() or not events_path.exists():
        payload = {
            "status": "blocked",
            "reason": "oof_predictions.csv and events.csv are required. Training did not finish.",
            "promoted": False,
        }
        run.mkdir(parents=True, exist_ok=True)
        (run / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2
    oof = pd.read_csv(oof_path)
    events = pd.read_csv(events_path)
    config = {}
    if (run / "run_config.json").exists():
        config = json.loads((run / "run_config.json").read_text())
    initial_cash = float(config.get("replay_initial_cash", 100000))
    drawdown_limit = -abs(float(config.get("max_drawdown_frac", 0.15)))
    promotion = config.get("promotion") or {
        "min_outer_folds": 3, "min_unique_fills": 200, "min_positive": 50, "min_negative": 50, "min_names": 20,
    }
    support = config.get("family_support") or {"min_fills": 40, "min_positive": 15, "min_negative": 15, "min_names": 8}
    if oof.empty:
        payload = {"status": "blocked", "reason": "no out-of-fold predictions", "promoted": False}
        (run / "metrics.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2

    y = oof["label"].astype(int).to_numpy()
    base = np.full(len(y), float(y.mean()))
    calibration = {}
    for name, column in (("rules", "p_rules"), ("logistic", "p_logistic"), ("xgboost", "p_xgboost"), ("chosen", "p_chosen")):
        prob = oof[column].astype(float).to_numpy()
        calibration[name] = {"brier": _brier(y, prob), "log_loss": _logloss(y, prob)}
    calibration["base_rate"] = {"brier": _brier(y, base), "log_loss": _logloss(y, base), "rate": float(y.mean())}

    replays = {}
    for name, column in (
        ("rules", "expected_net_r_rules"),
        ("logistic", "expected_net_r_logistic"),
        ("xgboost", "expected_net_r_xgboost"),
    ):
        decisions = oof.copy()
        decisions["expected_net_r"] = decisions[column]
        result = replay_portfolio(decisions, initial_cash=initial_cash, initial_equity=initial_cash)
        replays[name] = result
    chosen_name = "logistic"
    if abs(replays["logistic"]["final_equity"] - replays["xgboost"]["final_equity"]) >= 0.02 * initial_cash:
        if replays["xgboost"]["final_equity"] > replays["logistic"]["final_equity"]:
            chosen_name = "xgboost"
    # Model choice for probabilities was made inside training on calibration data.
    # Portfolio comparison here is descriptive and is not used to refit.
    chosen = replays["logistic"] if oof["model_choice"].mode().iloc[0] == "logistic" else replays["xgboost"]
    chosen_key = str(oof["model_choice"].mode().iloc[0])
    equity = chosen["equity"]
    pd.DataFrame(equity).to_csv(run / "equity_curve.csv", index=False)
    pd.DataFrame(chosen["fills"]).to_csv(run / "replay_fills.csv", index=False)
    (run / "rejections.json").write_text(json.dumps(chosen["rejections"], indent=2) + "\n")

    families = _family_report(oof, support)
    failed = []
    if oof["fold"].nunique() < int(promotion["min_outer_folds"]):
        failed.append("outer_folds")
    if len(oof) < int(promotion["min_unique_fills"]):
        failed.append("unique_oos_fills")
    if int((y == 1).sum()) < int(promotion["min_positive"]):
        failed.append("positives")
    if int((y == 0).sum()) < int(promotion["min_negative"]):
        failed.append("negatives")
    if oof["symbol"].nunique() < int(promotion["min_names"]):
        failed.append("names")
    drawdown = _drawdown(equity)
    if drawdown < drawdown_limit:
        failed.append("drawdown")
    uncertainty = _bootstrap(pd.DataFrame(chosen["fills"]))
    if uncertainty.get("p05") is None or uncertainty["p05"] <= 0:
        failed.append("block_bootstrap_uncertainty")
    rules_mean = float(np.mean([row["net_r"] for row in replays["rules"]["fills"]])) if replays["rules"]["fills"] else 0.0
    chosen_mean = float(np.mean([row["net_r"] for row in chosen["fills"]])) if chosen["fills"] else 0.0
    if chosen_mean <= rules_mean or chosen_mean <= 0:
        failed.append("no_improvement_vs_rules_baseline")
    if any(row["failed_gates"] for row in families.values()):
        failed.append("family_support")

    sector = {}
    for row in chosen["fills"]:
        tags = tags_for(row["symbol"]) or ["unknown"]
        for tag in tags:
            sector[tag] = sector.get(tag, 0.0) + float(row["net_inr"])
    weekly = _weekly(oof, chosen["fills"])
    status_counts = events["status"].value_counts().to_dict() if "status" in events else {}
    gap_losses = None
    if "exit_reason" in events.columns:
        lookup = events.drop_duplicates("event_id").set_index("event_id")["exit_reason"]
        reasons = oof["event_id"].map(lookup)
        gap_losses = int(((oof["net_r"] < -1) & (reasons == "stop_gap")).sum())
    metrics = {
        "status": "evaluated",
        "promoted": False,
        "promotion_decision": "watch" if failed else "gates_passed_not_promoted",
        "failed_gates": failed,
        "survivorship": "current_nifty_200_membership_not_point_in_time",
        "execution_proxy": "next_session_open_not_an_observed_0925_fill",
        "october_book_applied_to_history": False,
        "replay_initial_cash": initial_cash,
        "event_rows": int(len(events)),
        "event_status_counts": {str(k): int(v) for k, v in status_counts.items()},
        "unique_oos_fills": int(len(oof)),
        "names": int(oof["symbol"].nunique()),
        "positive": int((y == 1).sum()),
        "negative": int((y == 0).sum()),
        "folds_with_predictions": int(oof["fold"].nunique()),
        "mean_net_r_all_scored": float(oof["net_r"].mean()),
        "calibration": calibration,
        "portfolio": {
            name: {
                "final_equity": result["final_equity"],
                "fills": len(result["fills"]),
                "mean_net_r": float(np.mean([row["net_r"] for row in result["fills"]])) if result["fills"] else None,
                "profit_factor": _profit_factor(np.array([row["net_r"] for row in result["fills"]])) if result["fills"] else None,
                "drawdown": _drawdown(result["equity"]),
                "rejections": result["rejections"],
            }
            for name, result in replays.items()
        },
        "chosen_portfolio_model": chosen_key,
        "chosen_mean_net_r": chosen_mean,
        "rules_mean_net_r": rules_mean,
        "drawdown": drawdown,
        "uncertainty": uncertainty,
        "weekly": weekly,
        "families": families,
        "sector_net_inr": sector,
        "adverse_gap_loss_count": gap_losses,
        "holding_sessions_median": float(pd.to_numeric(events.loc[events["status"] == "filled", "bars_held"], errors="coerce").median()) if "bars_held" in events else None,
    }
    (run / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps({
        "promotion_decision": metrics["promotion_decision"],
        "failed_gates": failed,
        "unique_oos_fills": metrics["unique_oos_fills"],
        "folds_with_predictions": metrics["folds_with_predictions"],
        "chosen_mean_net_r": chosen_mean,
        "weekly": weekly,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
