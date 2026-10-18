"""Record a research snapshot. Production eligibility stays disabled."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from swing_v5.forward_log import append_event, classify, event_id, now_iso
from swing_v5.paper import policy_hash, provenance

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "artifacts" / "v5" / "forward" / "ledger.jsonl"
IST = ZoneInfo("Asia/Kolkata")


def next_execution(created: datetime) -> str | None:
    if created.weekday() >= 5:
        days = 7 - created.weekday()
        session = (created + timedelta(days=days)).replace(hour=9, minute=25, second=0, microsecond=0)
        return session.isoformat(timespec="seconds")
    if (created.hour, created.minute) < (9, 25):
        return created.replace(hour=9, minute=25, second=0, microsecond=0).isoformat(timespec="seconds")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    created = now_iso()
    clock = datetime.now(IST)
    market_session = None if clock.weekday() >= 5 else clock.date().isoformat()
    earliest = next_execution(clock)
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    screen_path = ROOT / "artifacts" / "v5" / "forward" / "screens.json"
    source = screen_path.read_bytes() if screen_path.exists() else b""
    record = provenance(
        requested_as_of=args.as_of,
        source_hash=hashlib.sha256(source).hexdigest() if source else "",
        universe_hash=hashlib.sha256((ROOT / "universes" / "india_150.csv").read_bytes()).hexdigest(),
        strategy_version="qtv_v1_frozen",
        policy_digest=policy_hash(policy),
        market_session=market_session,
        earliest_execution_at=earliest,
    )
    record.update({
        "event_id": event_id("snapshot", record["decision_created_at"], args.as_of or ""),
        "signals": [],
        "orders": [],
        "production_eligibility": "disabled",
        "classification": classify(created, earliest, requested_as_of=args.as_of),
        "note": "No equity session is invented on a weekend. Screen membership is first seen at retrieval, not on the prior session.",
    })
    saved = append_event(LOG, record)
    print(json.dumps({"classification": saved["classification"], "market_session": market_session, "production_eligibility": "disabled", "duplicate_skipped": saved.get("duplicate_skipped", False)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
