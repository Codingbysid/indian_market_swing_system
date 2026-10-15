"""Offline snapshot preview; no S3, notifications or broker writes.
The 09:25 scan is simulated with prior completed bars. Later headlines are not fed
back into that earlier simulated decision. Quotes/levels are research proxies.
"""

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import argparse, json, os, time, resource
import pandas as pd
from backtest.india_v4 import load_data, write_json
from swing_core.engine_v4 import evaluate_snapshot

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of")
    args = parser.parse_args()
    cfg = json.loads((ROOT / "config/research_v4.json").read_text())
    policy = json.loads((ROOT / "config/live_v4.json").read_text())
    meta, frames, nifty, fail = load_data(cfg)
    model = json.loads(
        (ROOT / "backtest/artifacts/v4/model.candidate.json").read_text()
    )
    state = json.loads((ROOT / "data/portfolio_state.json").read_text())
    stamp = (
        datetime.fromisoformat(args.as_of)
        if args.as_of
        else datetime.combine(
            pd.Timestamp(cfg["as_of"]).date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=ZoneInfo("Asia/Kolkata"),
        ).replace(hour=9, minute=25)
    )
    t = time.perf_counter()
    result = evaluate_snapshot(
        state,
        {s: d.tail(400) for s, d in frames.items()},
        nifty.tail(400),
        meta,
        model,
        as_of=stamp,
        policy=policy,
    )
    result["preview_only"] = True
    result["price_source"] = "yahoo_chart_research_proxy"
    result["not_a_live_buy_or_sell"] = True
    result["cached_decision_seconds"] = time.perf_counter() - t
    result["max_process_rss_MiB"] = (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    )
    # This helper imports the Lambda functions with dummy credentials solely to
    # prevent metadata credential discovery. It invokes no AWS operation.
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "eu-north-1")
    from lambda_function import compute_holding_levels
    from swing_core.indicators import calculate_rsi, calculate_atr

    rows = [
        compute_holding_levels(
            h,
            frames[h["symbol"]].tail(400),
            {},
            lambda d: calculate_rsi(d.Close),
            calculate_atr,
        )
        for h in state["holdings"]
    ]
    result["legacy_levels_research_preview"] = rows
    write_json(ROOT / "backtest/artifacts/v4/preview.json", result)
    print(
        json.dumps(
            {
                "regime": result["regime"],
                "BUY": len(result["BUY"]),
                "WATCH": len(result["WATCH"]),
                "market": result.get("market"),
                "limits": result["limits"],
                "cached_seconds": result["cached_decision_seconds"],
                "levels": [
                    {
                        k: r[k]
                        for k in [
                            "symbol",
                            "ltp",
                            "be",
                            "t1",
                            "t2",
                            "t1_qty",
                            "t2_qty",
                            "actionable_intents",
                        ]
                    }
                    for r in rows
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
