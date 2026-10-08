"""Build costed events for the six research families.

python -m backtest.labeler --config config/research_v2.json --out backtest/artifacts/v3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.panel import build_events, load_ohlcv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/research_v2.json")
    parser.add_argument("--out", default="backtest/artifacts/v3")
    args = parser.parse_args(argv)
    config_path = ROOT / args.config
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads(config_path.read_text())
    (out / "run_config.json").write_text(json.dumps(config, indent=2) + "\n")
    universe_path = ROOT / config["universe"]
    data_dir = ROOT / config["data_dir"]
    nifty_path = data_dir / "NSE_NIFTY.csv"
    blocker = None
    if not universe_path.exists():
        blocker = f"universe file missing: {universe_path}"
    elif not nifty_path.exists():
        blocker = f"NSE_NIFTY.csv missing in {data_dir}"
    else:
        names = pd.read_csv(universe_path)
        present = [s for s in names["Symbol"].astype(str).str.upper() if (data_dir / f"NSE_{s}.csv").exists()]
        if len(present) < 20:
            blocker = (
                f"only {len(present)} of {len(names)} Nifty 200 histories are cached. "
                "Refusing to substitute the old screener cache. "
                "Run: python -m backtest.fetch_history --universe universes/nifty_200.csv --bars 2000 --refresh"
            )
    if blocker:
        payload = {"status": "blocked", "reason": blocker, "promoted": False}
        (out / "labeler_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))
        return 2
    universe = pd.read_csv(universe_path)
    nifty = load_ohlcv(nifty_path)
    events = build_events(
        data_dir,
        universe,
        nifty,
        tickets=[float(x) for x in config["tickets"]],
        warmup=int(config.get("warmup", 300)),
        liquidity=float(config.get("liquidity_rupees", 20_00_00_000)),
        out_dir=out,
    )
    events.to_csv(out / "events.csv", index=False)
    summary = {
        "status": "ok",
        "rows": int(len(events)),
        "canonical_events": int(events["canonical"].sum()) if len(events) and "canonical" in events else 0,
        "survivorship": "current_nifty_200_membership",
        "promoted": False,
    }
    (out / "labeler_status.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
