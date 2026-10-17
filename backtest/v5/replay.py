"""Fixture decision plus an optional price-only cash replay. Neither is a live promotion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from swing_v5.decision import evaluate
from swing_v5.portfolio import book_from_reference
from swing_v5.risk import largest_quantity

ROOT = Path(__file__).resolve().parents[2]


def _replay(events: pd.DataFrame) -> dict:
    if events.empty:
        return {"status": "no_events", "fills": 0}
    filled = events[events["long_status"] == "filled"].sort_values("date")
    cash = 5748.0
    reserve = 1149.60
    open_until = []
    pnl = 0.0
    fills = 0
    rejections = {"zero_quantity": 0, "slots": 0}
    for row in filled.itertuples():
        open_until = [item for item in open_until if item > row.date]
        if len(open_until) >= 2:
            rejections["slots"] += 1
            continue
        entry = float(row.entry or row.close)
        gap = float(row.downside_gap95_63_atr) * float(row.atr) if pd.notna(row.downside_gap95_63_atr) else None
        if gap is None:
            rejections["zero_quantity"] += 1
            continue
        stop = entry - 2 * float(row.atr)
        qty = largest_quantity(
            entry=entry,
            stop=stop,
            gap_buffer=gap,
            cash_left=max(0.0, cash - reserve),
            debit_cap=2299.20,
            trade_risk=86.22,
            aggregate_left=172.44,
            day=str(row.date),
        )
        if qty <= 0:
            rejections["zero_quantity"] += 1
            continue
        trade_pnl = float(row.long_net_r) * qty * (entry - stop)
        pnl += trade_pnl
        cash += trade_pnl
        fills += 1
        open_until.append(str(row.exit_date or row.date))
    return {"status": "research_counterfactual", "fills": fills, "net_pnl": round(pnl, 2), "rejections": rejections, "promoted": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", required=True)
    args = parser.parse_args(argv)
    raw = json.loads((ROOT / args.book).read_text())
    book = book_from_reference(raw)
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    decision = evaluate({"as_of": raw["as_of"], "regime": "DATA_INVALID", "candidates": [], "dry_run": True}, book, policy)
    events_path = ROOT / "artifacts" / "v5" / "events.csv"
    research = _replay(pd.read_csv(events_path)) if events_path.exists() else {"status": "blocked_events_missing"}
    payload = {
        "book_schema": book["schema"],
        "s3_write": False,
        "zentec_plan": next(row["plan_status"] for row in book["holdings"] if row["symbol"] == "ZENTEC"),
        "decision": decision,
        "research_replay": research,
        "promoted": False,
    }
    out = ROOT / "artifacts" / "v5" / "replay.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"buys": [row for row in decision["intents"] if row["action"] == "BUY"], "research_fills": research.get("fills"), "promoted": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
