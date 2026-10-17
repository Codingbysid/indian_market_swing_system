"""Event flags. Unknown coverage is not good news, and headlines cannot raise size."""

from __future__ import annotations


def event_block(events: list[dict] | None, symbol: str, session: str) -> str | None:
    if events is None:
        return "event_coverage_unknown"
    for event in events:
        if event.get("symbol") != symbol:
            continue
        start = event.get("block_from")
        end = event.get("block_until")
        if start and end and start <= session <= end:
            return "earnings_or_material_event"
    return None


def quantity_after_news(quantity: int, tone: float | None) -> int:
    if tone is None:
        return quantity
    if tone < 0:
        return 0
    return quantity
