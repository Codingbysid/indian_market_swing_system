"""Advance admitted paper orders. An empty book is a no-op."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from .costs import buy_fees, sell_fees
from .research_ledger import ResearchLedger, logical_id
from .sessions import IST, SessionClock


def _bars(bundle: dict, symbol: str) -> pd.DataFrame | None:
    items = bundle.get("symbols") or []
    if not items and bundle.get("prices") is not None:
        items = [bundle]
    for item in items:
        identity = item.get("identity") or {}
        if identity.get("nse_symbol") == symbol and item.get("prices") is not None:
            return pd.DataFrame(item["prices"])
    return None


def advance_book(clock: datetime, bundle: dict, ledger: ResearchLedger) -> dict:
    holidays = {datetime.fromisoformat(day).date() for day in bundle.get("holidays") or []}
    session = SessionClock(clock, holidays, calendar_verified=bool(bundle.get("calendar_verified")))
    price_session = session.latest_completed_session()
    events = ledger.events()
    orders = [row for row in events if row.get("kind") == "paper_order"]
    closed = {row["order_id"] for row in events if row.get("kind") in {"paper_fill", "paper_expiry"}}
    exited = {row["order_id"] for row in events if row.get("kind") == "paper_exit"}
    pending = [row for row in orders if row["order_id"] not in closed]
    positions = [row for row in events if row.get("kind") == "paper_fill" and row["order_id"] not in exited]
    if not pending and not positions:
        return {"status": "noop", "reason": "no pending orders or open positions", "processed": 0, "execution_proxy": "next_session_open_not_verified_0925"}
    if price_session is None:
        return {"status": "noop", "reason": "calendar_unverified", "processed": 0}
    processed = 0
    for order in pending:
        frame = _bars(bundle, order["symbol"])
        if frame is None or "Date" not in frame:
            continue
        dates = pd.to_datetime(frame["Date"]).dt.date
        eligible = frame.loc[(dates > datetime.fromisoformat(order["signal_session"]).date()) & (dates <= price_session)]
        if eligible.empty:
            continue
        bar = eligible.iloc[0]
        opened = float(bar["Open"])
        limit = float(order["limit"])
        atr = float(order["atr"])
        stop = float(order["stop"])
        in_band = limit <= opened <= limit + 0.25 * atr
        if not in_band or opened <= stop:
            expiry = {"kind": "paper_expiry", "order_id": order["order_id"], "symbol": order["symbol"], "status": "unfilled", "session": str(bar["Date"])[:10]}
            ledger.append(logical_id({"expiry": order["order_id"]}), expiry)
            processed += 1
            continue
        debit = opened * int(order["qty"]) + buy_fees(opened, int(order["qty"]), day=str(bar["Date"])[:10])
        fill = {
            "kind": "paper_fill",
            "order_id": order["order_id"],
            "symbol": order["symbol"],
            "qty": order["qty"],
            "fill": opened,
            "stop": stop,
            "target": order["target"],
            "horizon": order["horizon"],
            "family": order["family"],
            "session": str(bar["Date"])[:10],
            "debit": debit,
            "execution_proxy": "next_session_open_not_verified_0925",
        }
        ledger.append(logical_id({"fill": order["order_id"]}), fill)
        processed += 1
        positions.append(fill)
    for position in list(positions):
        if position["order_id"] in exited:
            continue
        frame = _bars(bundle, position["symbol"])
        if frame is None:
            continue
        dates = pd.to_datetime(frame["Date"]).dt.date
        later = frame.loc[(dates > datetime.fromisoformat(position["session"]).date()) & (dates <= price_session)]
        for _, bar in later.iterrows():
            low = float(bar["Low"])
            high = float(bar["High"])
            opened = float(bar["Open"])
            if low <= float(position["stop"]):
                exit_px = min(opened, float(position["stop"]))
                reason = "stop"
            elif high >= float(position["target"]):
                exit_px = max(opened, float(position["target"])) if opened < float(position["target"]) else opened
                reason = "target"
            else:
                continue
            proceeds = exit_px * int(position["qty"]) - sell_fees(exit_px, int(position["qty"]), day=str(bar["Date"])[:10])
            ledger.append(
                logical_id({"exit": position["order_id"]}),
                {
                    "kind": "paper_exit",
                    "order_id": position["order_id"],
                    "symbol": position["symbol"],
                    "qty": position["qty"],
                    "exit": exit_px,
                    "reason": reason,
                    "session": str(bar["Date"])[:10],
                    "proceeds": proceeds,
                    "horizon": position["horizon"],
                    "execution_proxy": "next_session_open_not_verified_0925",
                },
            )
            exited.add(position["order_id"])
            processed += 1
            break
    if processed == 0:
        return {"status": "noop", "reason": "no newly completed session", "processed": 0, "execution_proxy": "next_session_open_not_verified_0925"}
    return {"status": "advanced", "processed": processed, "price_session": price_session.isoformat(), "execution_proxy": "next_session_open_not_verified_0925"}
