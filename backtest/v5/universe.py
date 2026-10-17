"""Freeze at most 150 names. The researched India 150 list is not enlarged."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from swing_v5.universe import select_universe

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--max-symbols", type=int, default=150)
    args = parser.parse_args(argv)
    frame = pd.read_csv(ROOT / "universes" / "india_150.csv")
    if len(frame) > args.max_symbols:
        raise SystemExit("india_150.csv exceeds the unique-equity cap")
    held = ["HAL", "ZENTEC", "CDSL", "BPCL"]
    rows = [
        (str(row.Symbol).upper(), str(row.Sector), float(row.Turnover20 or 0))
        for row in frame.itertuples()
        if str(row.Symbol).upper() not in held
    ]
    selected = select_universe(held=held, pending=[], screens={}, liquid=rows, max_symbols=args.max_symbols)
    payload = {
        "as_of": args.as_of,
        "count": selected["count"],
        "max_symbols": args.max_symbols,
        "pinned": held,
        "fundamental_screens": "not_mixed_into_historical_membership",
        "symbols": [row["symbol"] for row in selected["symbols"]],
    }
    out = ROOT / "artifacts" / "v5" / f"universe_{args.as_of}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"count": payload["count"], "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
