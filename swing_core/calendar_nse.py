"""NSE session helpers, official CM holiday guards, and trading clock."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]

# Cash market trading hours (IST)
PRE_OPEN = time(9, 0)
OPEN = time(9, 15)
CLOSE = time(15, 30)

# Treat the daily candle as complete after the cash close plus a short buffer.
SESSION_COMPLETE_HOUR = 15
SESSION_COMPLETE_MINUTE = 45


def now_ist(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def holiday_dates(path: Path | None = None) -> set[date]:
    source = path or (ROOT / "config" / "nse_cm_holidays_2026.json")
    if not source.exists():
        return set()
    payload = json.loads(source.read_text())
    return {date.fromisoformat(day) for day in payload.get("dates", [])}


def is_weekend(day: date | datetime) -> bool:
    d = day.date() if isinstance(day, datetime) else day
    return d.weekday() >= 5


def is_holiday(day: date | datetime, holidays: set[date] | None = None) -> bool:
    d = day.date() if isinstance(day, datetime) else day
    active_holidays = holidays if holidays is not None else holiday_dates()
    return d in active_holidays


def is_trading_day(day: date | datetime, holidays: set[date] | None = None) -> bool:
    d = day.date() if isinstance(day, datetime) else day
    return not is_weekend(d) and not is_holiday(d, holidays=holidays)


class SessionClock:
    def __init__(self, now: datetime, holidays: set[date] | None = None, *, calendar_verified: bool = True):
        if now.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self.now = now.astimezone(IST)
        self.holidays = holidays if holidays is not None else holiday_dates()
        self.calendar_verified = calendar_verified

    def is_weekend(self, day: date | datetime) -> bool:
        return is_weekend(day)

    def is_holiday(self, day: date | datetime) -> bool:
        return is_holiday(day, holidays=self.holidays)

    def is_trading_day(self, day: date | datetime) -> bool:
        if not self.calendar_verified:
            return False
        return is_trading_day(day, holidays=self.holidays)

    def phase(self) -> str:
        day = self.now.date()
        if not self.calendar_verified:
            return "calendar_unverified"
        if not self.is_trading_day(day):
            return "closed"
        clock = self.now.timetz().replace(tzinfo=None)
        if clock < OPEN:
            return "before_open"
        if clock < CLOSE:
            return "intraday"
        return "after_close"

    def latest_completed_session(self) -> date | None:
        if not self.calendar_verified:
            return None
        day = self.now.date()
        if self.is_trading_day(day) and self.phase() == "after_close":
            return day
        cursor = day - timedelta(days=1)
        for _ in range(14):
            if self.is_trading_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        return None

    def next_execution(self) -> datetime | None:
        if not self.calendar_verified:
            return None
        day = self.now.date()
        clock = self.now.timetz().replace(tzinfo=None)
        start = day if self.is_trading_day(day) and clock < OPEN else day + timedelta(days=1)
        for offset in range(21):
            candidate = start + timedelta(days=offset)
            if self.is_trading_day(candidate):
                return datetime.combine(candidate, OPEN, IST)
        return None


def clock_for(now: datetime | None = None, *, verified: bool = True) -> SessionClock:
    dt = now_ist(now)
    return SessionClock(dt, holiday_dates(), calendar_verified=verified)


def is_session_complete(now: datetime | None = None) -> bool:
    current = now_ist(now)
    return (current.hour, current.minute) >= (
        SESSION_COMPLETE_HOUR,
        SESSION_COMPLETE_MINUTE,
    )


def is_fresh_bar(bar_date, as_of, *, max_session_gap: int = 1) -> bool:
    """A candle is fresh only if it is the as-of session or the previous one."""
    bar = pd.Timestamp(bar_date).tz_localize(None).normalize()
    today = pd.Timestamp(as_of).tz_localize(None).normalize()
    if bar > today:
        return False
    sessions = pd.bdate_range(bar, today)
    return len(sessions) - 1 <= max_session_gap


def completed_bar_iloc(index, now: datetime | None = None) -> int | None:
    """Return iloc of the last COMPLETED exchange session, or None."""
    if index is None:
        return None
    n = len(index)
    if n < 1:
        return None
    current = now_ist(now)
    today = current.date()
    last = pd.Timestamp(index[-1]).tz_localize(None).date()
    if last < today:
        return -1
    if last == today and is_session_complete(current):
        return -1
    if last == today:
        return -2 if n >= 2 else None
    return None
