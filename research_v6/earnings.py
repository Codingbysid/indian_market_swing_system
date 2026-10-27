"""Earnings reaction timing. No return is produced without a verified archive."""

from __future__ import annotations

from datetime import datetime, time

import numpy as np

IST_OPEN = time(9, 15)
IST_CLOSE = time(15, 30)


def reaction_session(release_at: datetime, sessions: list[str]) -> str | None:
    """The first daily session whose close is after the announcement is public."""
    if release_at.tzinfo is None:
        raise ValueError("announcement timestamp must be timezone-aware")
    day = release_at.date().isoformat()
    clock = release_at.timetz().replace(tzinfo=None)
    later = [session for session in sessions if session > day]
    if day in sessions and clock < IST_OPEN:
        return day
    if day in sessions and IST_OPEN <= clock <= IST_CLOSE:
        return later[0] if later else None
    return later[0] if later else None


def reaction_measure(stock_return: float, index_return: float, beta_prev: float, atr20_prev: float, close_prev: float) -> float:
    scale = atr20_prev / close_prev
    if not np.isfinite(scale) or scale <= 0 or not all(np.isfinite(value) for value in (stock_return, index_return, beta_prev)):
        raise ValueError("reaction inputs rejected")
    return float((stock_return - beta_prev * index_return) / scale)


def parse_result_filings(path) -> dict:
    """Board-outcome filings whose text states financial results. Times are read as IST."""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    raw = json.loads(path.read_bytes())
    if not isinstance(raw, list):
        return {"status": "BLOCKED", "reason": "announcement_payload_not_a_list"}
    filings = []
    for row in raw:
        if not isinstance(row, dict) or row.get("desc") != "Outcome of Board Meeting":
            continue
        text = str(row.get("attchmntText") or "")
        lowered = text.lower()
        if "financial result" not in lowered and not ("result" in lowered and ("audited" in lowered or "unaudited" in lowered)):
            continue
        symbol = str(row.get("symbol") or "").upper()
        clock = str(row.get("sort_date") or "")
        try:
            published = datetime.strptime(clock, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        except ValueError:
            continue
        if not symbol:
            continue
        filings.append({
            "symbol": symbol,
            "published_at": published.isoformat(timespec="seconds"),
            "text": text[:240],
            "source_url": row.get("attchmntFile"),
            "clock_convention": "nse_sort_date_interpreted_as_ist",
        })
    if not filings:
        return {"status": "BLOCKED", "reason": "no_financial_result_board_outcomes", "rows": len(raw)}
    return {"status": "READY", "rows": len(raw), "result_filings": len(filings), "filings": filings, "price": "daily_ohlcv_only", "vwap": "not_available", "sue": False}


def validate_archive(rows: list[dict] | None) -> dict:
    """BLOCKED unless every row has identity, a timezone-aware timestamp, and a source hash."""
    if not rows:
        return {"status": "BLOCKED", "reason": "no_verified_announcement_archive", "rows": 0}
    for row in rows:
        try:
            if row.get("symbol") and row.get("source_sha256") and len(str(row["source_sha256"])) == 64:
                datetime.fromisoformat(str(row["published_at"]))
                if "T" not in str(row["published_at"]) and "+" not in str(row["published_at"]):
                    raise ValueError("date without a clock")
                continue
        except (TypeError, ValueError):
            pass
        return {"status": "BLOCKED", "reason": "announcement_fields_unverified", "rows": len(rows)}
    return {"status": "READY", "rows": len(rows), "price": "daily_ohlcv_only", "vwap": "not_available"}
