"""Fixture decision plus an optional price-only cash replay. Neither is a live promotion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from swing_v5.decision import evaluate
from swing_v5.ledger import replay_ledger
from swing_v5.portfolio import book_from_reference

ROOT = Path(__file__).resolve().parents[2]


def _replay(events: pd.DataFrame) -> dict:
    filled = events[events["long_status"] == "filled"] if "long_status" in events.columns else events
    return replay_ledger(filled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", required=True)
    args = parser.parse_args(argv)
    raw = json.loads((ROOT / args.book).read_text())
    book = book_from_reference(raw)
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    decision = evaluate({"as_of": raw["as_of"], "regime": "DATA_INVALID", "candidates": [], "dry_run": True}, book, policy)
    events_path = ROOT / "artifacts" / "v5" / "corrected" / "events.csv"
    research = _replay(pd.read_csv(events_path)) if events_path.exists() else {"status": "blocked_corrected_events_missing"}
    payload = {
        "book_schema": book["schema"],
        "s3_write": False,
        "zentec_plan": next(row["plan_status"] for row in book["holdings"] if row["symbol"] == "ZENTEC"),
        "decision": decision,
        "research_replay": research,
        "promoted": False,
    }
    out = ROOT / "artifacts" / "v5" / "corrected" / "replay.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"buys": [row for row in decision["intents"] if row["action"] == "BUY"], "research_fills": research.get("fills"), "promoted": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
