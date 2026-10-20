"""Advance a durable paper book one completed session at a time."""

from __future__ import annotations

from datetime import date, timedelta

from swing_ops.calendar import holiday_dates
from swing_ops.ledger import event_id, view
from swing_v5.costs import buy_fees, sell_fees

EXECUTION_PROXY = "next_session_open_not_verified_0925"
VERTICAL = {"short": 15, "long": 40}


def exchange_sessions(start: str, end: str) -> list[str]:
    """Entry date is session 1. Weekends and the verified holiday list are not sessions."""
    holidays = holiday_dates()
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    found = []
    while cursor <= last:
        if cursor.weekday() < 5 and cursor not in holidays:
            found.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return found


def _bars(panels: dict, symbol: str, session: str) -> list[dict]:
    rows = [row for row in panels.get(symbol) or [] if row.get("Date") and row["Date"] <= session]
    return sorted(rows, key=lambda row: row["Date"])


def _between(rows: list[dict], after: str, through: str, *, include_after: bool = False) -> list[dict]:
    out = []
    for row in rows:
        if row["Date"] > through:
            continue
        if include_after and row["Date"] >= after:
            out.append(row)
        elif not include_after and row["Date"] > after:
            out.append(row)
    return out


def advance_events(state: dict, panels: dict, session: str | None) -> list[dict]:
    if not session:
        return []
    current = view(state)
    events = []
    exited = {row.get("order_id") for row in state.get("exits") or []}
    for order in current["pending"]:
        rows = _bars(panels, order["symbol"], session)
        upcoming = _between(rows, order["signal_session"], session)
        if not upcoming:
            continue
        bar = upcoming[0]
        opened = float(bar["Open"])
        limit = float(order["limit"])
        atr = float(order["atr"])
        stop = float(order["stop"])
        reserved = float(order["qty"]) * limit
        if not (limit <= opened <= limit + 0.25 * atr) or opened <= stop:
            events.append({
                "event_id": event_id({"expiry": order["order_id"]}),
                "kind": "paper_expiry",
                "payload": {"order_id": order["order_id"], "symbol": order["symbol"], "status": "unfilled", "session": bar["Date"], "reserved": reserved, "stressed_risk": order.get("stressed_risk") or 0.0, "execution_proxy": EXECUTION_PROXY},
            })
            continue
        debit = opened * int(order["qty"]) + buy_fees(opened, int(order["qty"]), day=bar["Date"])
        account = (state.get("account") or {})
        available = None if account.get("cash") is None else float(account["cash"]) - float(account.get("reserved") or 0.0) + reserved
        if available is not None and debit - available > 1e-6:
            events.append({
                "event_id": event_id({"expiry": order["order_id"], "reason": "insufficient_cash_at_fill"}),
                "kind": "paper_expiry",
                "payload": {"order_id": order["order_id"], "symbol": order["symbol"], "status": "unfilled", "reason": "insufficient_cash_at_fill", "session": bar["Date"], "reserved": reserved, "stressed_risk": order.get("stressed_risk") or 0.0, "execution_proxy": EXECUTION_PROXY},
            })
            continue
        fill = {
            "order_id": order["order_id"], "symbol": order["symbol"], "qty": order["qty"], "fill": opened,
            "stop": stop, "target": order["target"], "horizon": order["horizon"], "family": order["family"],
            "session": bar["Date"], "debit": debit, "reserved": reserved,
            "stressed_risk": order.get("stressed_risk") or 0.0,
            "vertical_sessions": VERTICAL.get(order.get("horizon"), 40),
            "execution_proxy": EXECUTION_PROXY,
        }
        events.append({"event_id": event_id({"fill": order["order_id"]}), "kind": "paper_fill", "payload": fill})
        exit_event = _exit_after(fill, rows, session)
        if exit_event:
            events.append(exit_event)
            exited.add(order["order_id"])
    for position in current["open_positions"]:
        if position["order_id"] in exited:
            continue
        rows = _bars(panels, position["symbol"], session)
        exit_event = _exit_after(position, rows, session)
        if exit_event:
            events.append(exit_event)
    return events


def _exit_after(position: dict, bars: list[dict], through: str) -> dict | None:
    """Session 1 is the fill date. A missing exchange session stops the walk."""
    by_date = {row["Date"]: row for row in bars}
    sessions = exchange_sessions(position["session"], through)
    vertical = int(position.get("vertical_sessions") or VERTICAL.get(position.get("horizon"), 40))
    for held, day in enumerate(sessions, start=1):
        bar = by_date.get(day)
        if bar is None:
            return None
        opened = float(bar["Open"])
        low = float(bar["Low"])
        high = float(bar["High"])
        stop = float(position["stop"])
        target = float(position["target"])
        if low <= stop and high >= target:
            exit_px, reason = min(opened, stop), "stop"
        elif low <= stop:
            exit_px, reason = min(opened, stop), "stop"
        elif high >= target:
            exit_px, reason = (opened if opened >= target else target), "target"
        elif held >= vertical:
            exit_px, reason = float(bar["Close"]), "vertical"
        else:
            continue
        proceeds = exit_px * int(position["qty"]) - sell_fees(exit_px, int(position["qty"]), day=bar["Date"])
        return {
            "event_id": event_id({"exit": position["order_id"]}),
            "kind": "paper_exit",
            "payload": {
                "order_id": position["order_id"], "symbol": position["symbol"], "qty": position["qty"],
                "exit": exit_px, "reason": reason, "session": bar["Date"], "proceeds": proceeds,
                "debit": position.get("debit"), "stressed_risk": position.get("stressed_risk") or 0.0,
                "horizon": position.get("horizon"), "execution_proxy": EXECUTION_PROXY,
                "signal_id": position["order_id"],
            },
        }
    return None


def session_dates(panels: dict, through: str) -> list[str]:
    found = set()
    for rows in panels.values():
        for row in rows:
            if row.get("Date") and row["Date"] <= through:
                found.add(row["Date"])
    return sorted(found)
