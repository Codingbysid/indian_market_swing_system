"""Exchange clock. An unverified calendar does not invent the next Monday open."""

from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN = time(9, 25)
CLOSE = time(15, 30)


class SessionClock:
    def __init__(self, now: datetime, holidays: set | None, *, calendar_verified: bool):
        if now.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self.now = now.astimezone(IST)
        self.holidays = holidays or set()
        self.calendar_verified = calendar_verified

    def is_weekend(self, day) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day) -> bool:
        return day in self.holidays

    def is_trading_day(self, day) -> bool:
        if not self.calendar_verified:
            return False
        return not self.is_weekend(day) and not self.is_holiday(day)

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

    def latest_completed_session(self):
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

    def next_execution(self):
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
