"""NSE session helpers. Completed daily bars vs the still-forming session."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
# Treat the daily candle as complete after the cash close plus a short buffer.
SESSION_COMPLETE_HOUR = 15
SESSION_COMPLETE_MINUTE = 45


def now_ist(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=IST)
    return now.astimezone(IST)


def is_session_complete(now: datetime | None = None) -> bool:
    current = now_ist(now)
    return (current.hour, current.minute) >= (
        SESSION_COMPLETE_HOUR,
        SESSION_COMPLETE_MINUTE,
    )


def completed_bar_iloc(index, now: datetime | None = None) -> int | None:
    """Return iloc of the last COMPLETED exchange session, or None.

    At 09:25 the current daily bar is incomplete — do not use iloc[-1]
    if that bar's date is today. After ~15:45 IST, today's bar is usable.
    """
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
    return -2 if n >= 2 else None
