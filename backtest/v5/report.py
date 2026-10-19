"""Regenerate the paper-book report from the persistent research ledger."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from swing_core.messages import research_message
from swing_v5.pipeline import _cash
from swing_v5.research_ledger import ResearchLedger

ROOT = Path(__file__).resolve().parents[2]
FORWARD = ROOT / "artifacts" / "v5" / "forward"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default="artifacts/v5/forward/paper_book.sqlite")
    args = parser.parse_args(argv)
    ledger = ResearchLedger(ROOT / args.ledger)
    events = ledger.events()
    snapshots = [row for row in events if row.get("kind") == "research_snapshot"]
    latest = snapshots[-1] if snapshots else {}
    orders = [row for row in events if row.get("kind") == "paper_order"]
    fills = [row for row in events if row.get("kind") == "paper_fill"]
    exits = [row for row in events if row.get("kind") == "paper_exit"]
    exited = {row["order_id"] for row in exits}
    filled = {row["order_id"] for row in fills}
    positions = [row for row in fills if row["order_id"] not in exited]
    pending = [row for row in orders if row["order_id"] not in filled and row["order_id"] not in {row["order_id"] for row in events if row.get("kind") == "paper_expiry"}]
    cash = _cash(ledger, 5748.0) if any(row.get("kind") == "book_open" for row in events) else None
    book = {
        "orders": len(orders),
        "pending": pending,
        "open_positions": positions,
        "exits": exits,
        "advance": "noop" if not pending and not positions else "pending_or_open",
    }
    reconciliation = {
        "opening_cash_reference": 5748.0,
        "opening_source": "october17_reference",
        "cash_after_reservations_and_fills": cash,
        "reserved_orders": len(pending),
        "open_positions": len(positions),
        "production_book": "not_modified",
    }
    (FORWARD / "paper_orders_and_positions.json").write_text(json.dumps(book, indent=2) + "\n")
    (FORWARD / "cash_risk_reconciliation.json").write_text(json.dumps(reconciliation, indent=2) + "\n")
    funnel_path = FORWARD / "candidate_funnel.csv"
    fields = ["symbol", "quality_detection", "trend_detection", "value_detection", "admission", "paper_action", "production_eligibility"]
    seen = set()
    with funnel_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in latest.get("detections") or []:
            seen.add(row.get("symbol"))
            detected = bool(row.get("funnel", {}).get("detected"))
            writer.writerow({
                "symbol": row.get("symbol"),
                "quality_detection": "not_evaluated_no_point_in_time_snapshot",
                "trend_detection": "detected" if detected and row.get("family") == "trend_transition" else "no_setup" if row.get("family") == "trend_transition" else "not_evaluated",
                "value_detection": "not_evaluated_no_point_in_time_snapshot",
                "admission": row.get("funnel", {}).get("admission") or "no_order",
                "paper_action": row.get("paper_action") or "NO_SIGNAL",
                "production_eligibility": "disabled",
            })
        readiness = FORWARD / "per_symbol_readiness.csv"
        if readiness.exists():
            for row in csv.DictReader(readiness.open()):
                if row["symbol"] in seen or row.get("stage") == "evaluable":
                    continue
                writer.writerow({
                    "symbol": row["symbol"],
                    "quality_detection": "not_evaluated",
                    "trend_detection": "not_evaluated",
                    "value_detection": "not_evaluated",
                    "admission": row.get("stage"),
                    "paper_action": "BLOCKED",
                    "production_eligibility": "disabled",
                })
    notice = research_message(latest, book, reconciliation)
    (FORWARD / "message_preview.txt").write_text(notice.body + "\n", encoding="utf-8")
    (FORWARD / "message_preview.html").write_text(notice.html, encoding="utf-8")
    print(json.dumps({"snapshots": len(snapshots), "orders": len(orders), "positions": len(positions), "cash": cash}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
