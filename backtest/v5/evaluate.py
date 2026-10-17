"""Price-only control versus blocked fundamental families. v4's loss is kept in the artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from backtest.v5.scan import price_only_events

ROOT = Path(__file__).resolve().parents[2]


def _pf(series: pd.Series) -> float | None:
    wins = float(series[series > 0].sum())
    losses = float(-series[series < 0].sum())
    if losses <= 0:
        return None
    return wins / losses


def _v4_baseline() -> dict:
    path = ROOT / "backtest" / "artifacts" / "v4" / "metrics.json"
    if not path.exists():
        return {"status": "missing"}
    metrics = json.loads(path.read_text())
    rules = metrics["replay"]["rules_fixed_risk"]
    return {
        "preserved": True,
        "net_completed_pnl": rules["net_completed_pnl"],
        "profit_factor": rules["profit_factor"],
        "completed_fills": rules["completed_fills"],
        "fortnight_two_intent_fraction": rules["activity"]["fraction_14_calendar_day_windows_at_least_2"],
        "quarter_kelly": "zero_for_every_v4_family",
        "note": "Development evidence. Not an untouched holdout and not erased by the v5 rebuild.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", default="config/v5/research_policy.json")
    args = parser.parse_args(argv)
    policy_path = ROOT / args.protocol
    raw = policy_path.read_bytes()
    protocol_hash = hashlib.sha256(raw).hexdigest()
    out = ROOT / "artifacts" / "v5" / "corrected"
    out.mkdir(parents=True, exist_ok=True)
    (out / "protocol.json").write_text(json.dumps({"sha256": protocol_hash, "path": args.protocol}, indent=2) + "\n")
    events, coverage = price_only_events()
    events.to_csv(out / "events.csv", index=False)
    filled = events[events["long_status"] == "filled"] if len(events) else events
    net = pd.to_numeric(filled["long_net_r"], errors="coerce").dropna() if len(filled) else pd.Series(dtype=float)
    short = pd.to_numeric(events.loc[events["short_status"] == "filled", "short_net_r"], errors="coerce").dropna() if len(events) else pd.Series(dtype=float)
    metrics = {
        "promoted": False,
        "protocol_sha256": protocol_hash,
        "data_label": "reused_v3_v4_cached_prices_development_not_untouched_holdout",
        "fundamental_historical_test": "blocked",
        "v4_negative_baseline": _v4_baseline(),
        "coverage": {key: value for key, value in coverage.items() if key not in ("missing", "short")},
        "missing_symbols": coverage.get("missing", []),
        "price_only_trend_transition": {
            "events": int(len(events)),
            "long_filled": int((events["long_status"] == "filled").sum()) if len(events) else 0,
            "long_unfilled": int((events["long_status"] == "unfilled").sum()) if len(events) else 0,
            "long_mean_net_r": None if net.empty else float(net.mean()),
            "long_profit_factor": None if net.empty else _pf(net),
            "long_wins": int((filled["long_label"] == 1).sum()) if len(filled) else 0,
            "long_losses": int((filled["long_label"] == 0).sum()) if len(filled) else 0,
            "short_filled": int(short.shape[0]),
            "short_mean_net_r": None if short.empty else float(short.mean()),
            "short_profit_factor": None if short.empty else _pf(short),
            "names": int(filled["symbol"].nunique()) if len(filled) else 0,
            "live_admission": "blocked_missing_restriction_metadata_and_unpromoted",
            "schema": "price_only_not_fundamental_validation",
            "development_data": "reused cache through 2026-10-08; not an untouched forward period",
        },
        "quality_continuation": coverage.get("quality_continuation"),
        "improving_value": coverage.get("improving_value"),
        "promotion_gates": {
            "min_unique_oos_portfolio_fills": 200,
            "met": False,
            "reason": "fundamental families have no point-in-time history; price-only control is not a promoted strategy",
        },
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps({"promoted": False, "events": metrics["price_only_trend_transition"]["events"], "long_mean_net_r": metrics["price_only_trend_transition"]["long_mean_net_r"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
