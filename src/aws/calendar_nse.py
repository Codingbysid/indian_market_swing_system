"""NSE cash market holiday guards, completed session clock, and IST time helpers."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

# Resolve config directory: supports local workspace, root, and Lambda container (/var/task)
def _find_config_dir() -> Path:
    # 1. Environment variable if set
    if os.getenv("CONFIG_DIR"):
        p = Path(os.getenv("CONFIG_DIR"))
        if p.exists():
            return p
    # 2. Container task root
    task_root = os.getenv("LAMBDA_TASK_ROOT")
    if task_root:
        p = Path(task_root) / "config"
        if p.exists():
            return p
    # 3. Relative to this source file (src/aws/calendar_nse.py -> repo_root/config)
    p = Path(__file__).resolve().parents[2] / "config"
    if p.exists():
        return p
    return Path("config")


CONFIG_DIR = _find_config_dir()

# Cash market trading hours (IST)
PRE_OPEN = time(9, 0)
OPEN = time(9, 15)
CLOSE = time(15, 30)

SESSION_COMPLETE_HOUR = 15
SESSION_COMPLETE_MINUTE = 45


def now_ist(now: datetime | None = None) -> datetime:
    """Return timezone-aware IST datetime."""
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def holiday_dates(path: Path | None = None) -> set[date]:
    """Load official NSE Cash Market holiday dates from JSON."""
    source = path or (CONFIG_DIR / "nse_cm_holidays_2026.json")
    if not source.exists():
        # Fallback to local config path if needed
        local_p = Path("config") / "nse_cm_holidays_2026.json"
        if local_p.exists():
            source = local_p
        else:
            return set()
    try:
        payload = json.loads(source.read_text())
        return {date.fromisoformat(day) for day in payload.get("dates", [])}
    except Exception:
        return set()


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
            raise ValueError("SessionClock requires timezone-aware datetime")
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


def completed_bar_iloc(index, now: datetime | None = None) -> int | None:
    """Return iloc of the last completed session bar, or None."""
    if index is None or len(index) == 0:
        return None
    current = now_ist(now)
    today = current.date()
    last = pd.Timestamp(index[-1]).tz_localize(None).date()
    if last < today:
        return -1
    if last == today and is_session_complete(current):
        return -1
    if last == today:
        return -2 if len(index) >= 2 else None
    return None
