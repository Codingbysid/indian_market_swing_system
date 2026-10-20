"""Completed-session clock from the bundled NSE cash-market holiday list."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from swing_v5.sessions import IST, SessionClock

ROOT = Path(__file__).resolve().parents[1]


def holiday_dates(path: Path | None = None) -> set[date]:
    source = path or (ROOT / "config" / "nse_cm_holidays_2026.json")
    payload = json.loads(source.read_text())
    return {date.fromisoformat(day) for day in payload["dates"]}


def clock_for(now: datetime, *, verified: bool = True) -> SessionClock:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    return SessionClock(now, holiday_dates(), calendar_verified=verified)
