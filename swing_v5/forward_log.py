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


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1] if "\n" in text else ""
        path.write_text(text, encoding="utf-8")
    rows = []
    kept = []
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
            kept.append(line)
        except json.JSONDecodeError:
            continue
    if len(kept) != len(text.splitlines()):
        path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    return rows


def append_event(path: Path, event: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {row.get("event_id") for row in load_events(path)}
    if event.get("event_id") in existing:
        event = {**event, "duplicate_skipped": True}
        return event
    line = json.dumps(event, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
    return event
