"""Append-only research ledger. A caller's as-of date is not a knowledge timestamp."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def event_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


def classify(decision_created_at: str, earliest_execution_at: str | None, *, requested_as_of: str | None) -> str:
    """A historical as-of date does not become the time the data was known."""
    if requested_as_of and requested_as_of < decision_created_at[:10]:
        return "retrospective_replay"
    if earliest_execution_at and decision_created_at < earliest_execution_at:
        return "prospective_observation"
    if not earliest_execution_at:
        return "incomplete_blocked"
    return "retrospective_replay"


from .research_ledger import LedgerConflict, LedgerCorrupt, append_event, load_events, recover_ledger  # noqa: F401
