"""Regenerate a research report from the append-only ledger."""

from __future__ import annotations

import json
from pathlib import Path

from swing_v5.forward_log import load_events

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    path = ROOT / "artifacts" / "v5" / "forward" / "ledger.jsonl"
    rows = load_events(path)
    report = {
        "events": len(rows),
        "classifications": sorted({row.get("classification") for row in rows}),
        "production_buys": 0,
        "paper_orders": sum(1 for row in rows if row.get("paper_action") == "PAPER_ORDER"),
        "note": "Derived from the ledger. Retrospective price-only results are not in this file.",
    }
    out = ROOT / "artifacts" / "v5" / "forward" / "report.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
