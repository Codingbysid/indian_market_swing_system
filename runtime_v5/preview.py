"""Dry run. Notifications and state writes stay at zero."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from swing_v5.decision import evaluate
from swing_v5.portfolio import book_from_reference

ROOT = Path(__file__).resolve().parents[1]


class RecordingServices:
    def __init__(self):
        self.writes = 0
        self.notifications = 0

    def put(self, *_args, **_kwargs):
        self.writes += 1

    def notify(self, *_args, **_kwargs):
        self.notifications += 1


def handle(event: dict, book: dict, policy: dict, services: RecordingServices) -> dict:
    dry = bool(event.get("dry_run"))
    result = evaluate({**event, "candidates": event.get("candidates") or [], "dry_run": dry}, book, policy)
    if dry:
        return {**result, "writes": services.writes, "notifications": services.notifications, "destination": "null"}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--book", required=True)
    args = parser.parse_args(argv)
    raw = json.loads((ROOT / args.book).read_text())
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    services = RecordingServices()
    result = handle({"action": "recommender", "dry_run": args.dry_run, "as_of": raw["as_of"], "regime": "DATA_INVALID"}, book_from_reference(raw), policy, services)
    print(json.dumps({"writes": result["writes"], "notifications": result["notifications"], "buys": [row for row in result["intents"] if row["action"] == "BUY"]}, indent=2))
    return 0 if result["writes"] == 0 and result["notifications"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
