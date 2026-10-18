"""Shared new-sleeve limits. Legacy marks are not the new-trade loss counter."""

from __future__ import annotations

import math


def money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def sleeve_pnl(nav: float, launch_nav: float, external_inflows: float) -> float:
    """P = N - N0 - net external inflows. Deposits are flows, not recovered losses."""
    return float(nav) - float(launch_nav) - float(external_inflows)


def loss_still_breached(book: dict, policy: dict) -> bool:
    pause = float(policy["risk"]["cumulative_net_loss_pause"])
    pnl = sleeve_pnl(
        book.get("sleeve_nav", book.get("cash", 0.0)),
        book.get("launch_nav", book.get("cash", 0.0)),
        book.get("external_inflows", 0.0),
    )
    return not math.isfinite(pnl) or pnl <= -pause


def is_latched(book: dict, policy: dict) -> bool:
    """A stored resume flag does not clear the latch. A rebound or deposit does not either."""
    if book.get("circuit_latched") is True:
        return True
    return loss_still_breached(book, policy)


def request_resume(book: dict, policy: dict, *, actor: str, reason: str, now: str) -> dict:
    """One audited transition. It cannot reopen buys while the loss threshold is still breached."""
    audit = {"actor": actor, "reason": reason, "at": now}
    book.setdefault("resume_audit", []).append(audit)
    book.pop("explicit_resume", None)
    if not book.get("circuit_latched"):
        audit["accepted"] = False
        audit["detail"] = "not_latched"
        return {"accepted": False, "reason": "not_latched"}
    if loss_still_breached(book, policy):
        audit["accepted"] = False
        audit["detail"] = "loss_threshold_still_breached"
        return {"accepted": False, "reason": "loss_threshold_still_breached"}
    book["circuit_latched"] = False
    audit["accepted"] = True
    return {"accepted": True}


def open_stressed_risk(book: dict) -> float:
    total = 0.0
    for holding in book.get("holdings") or []:
        if holding.get("classification") != "system_atr":
            continue
        if "stressed_risk" not in holding:
            return float("nan")
        value = float(holding.get("stressed_risk"))
        if not math.isfinite(value) or value < 0:
            return float("nan")
        total += value
    for reservation in book.get("reservations") or []:
        if reservation.get("status") != "open":
            continue
        if "stressed_risk" not in reservation:
            return float("nan")
        value = float(reservation.get("stressed_risk"))
        if not math.isfinite(value) or value < 0:
            return float("nan")
        total += value
    return total


def limits_for(
    nav: float,
    policy: dict,
    *,
    sleeve_p: float | None = None,
) -> dict:
    risk = policy["risk"]
    pause = float(risk["cumulative_net_loss_pause"])
    p_value = 0.0 if sleeve_p is None else float(sleeve_p)
    headroom = max(0.0, pause + p_value)
    reserve = money(nav * float(risk["cash_reserve_fraction"]))
    debit = money(nav * float(risk["position_debit_fraction"]))
    trade = min(
        float(risk["trade_risk_rupee_cap"]),
        nav * float(risk["trade_risk_fraction"]),
        headroom / float(risk["loss_headroom_trade_divisor"]),
    )
    aggregate = min(
        float(risk["aggregate_risk_rupee_cap"]),
        nav * float(risk["aggregate_risk_fraction"]),
        headroom * float(risk["loss_headroom_aggregate_fraction"]),
    )
    return {
        "reserve": reserve,
        "position_debit_cap": debit,
        "trade_risk": money(trade),
        "aggregate_risk": money(aggregate),
        "headroom": money(headroom),
        "max_positions": int(risk["max_new_and_pending_positions"]),
        "latched": p_value <= -pause,
    }


def stressed_loss(qty: int, entry: float, stop: float, gap_buffer: float, fees: float) -> float:
    return qty * max(0.0, (entry - stop) + gap_buffer) + fees


def largest_quantity(
    *,
    entry: float,
    stop: float,
    gap_buffer: float,
    cash_left: float,
    debit_cap: float,
    trade_risk: float,
    aggregate_left: float,
    day: str,
    max_cost_fraction: float = 0.25,
    apply_cost_filter: bool = True,
) -> int:
    """Search integers. Fees are recomputed at each quantity, not added after a division."""
    from .costs import buy_fees, round_trip

    values = (entry, stop, gap_buffer, cash_left, debit_cap, trade_risk, aggregate_left, max_cost_fraction)
    if not all(math.isfinite(value) for value in values):
        return 0
    if apply_cost_filter and not 0 < max_cost_fraction <= 1:
        return 0
    if entry <= 0 or stop <= 0 or gap_buffer < 0 or cash_left < 0 or debit_cap < 0 or trade_risk < 0 or aggregate_left < 0:
        return 0
    if stop >= entry:
        return 0
    room = min(cash_left, debit_cap)
    if room < entry:
        return 0
    qty = int(room // entry)
    while qty > 0:
        debit = qty * entry + buy_fees(entry, qty, day=day, slippage_bps=10)
        if debit > room + 1e-6:
            qty -= 1
            continue
        fees = round_trip(entry, entry, qty, day=day, slippage_bps=10)
        loss = stressed_loss(qty, entry, stop, gap_buffer, fees)
        price_risk = qty * max(entry - stop, 0.0)
        if price_risk <= 0 or (apply_cost_filter and fees > max_cost_fraction * price_risk):
            qty -= 1
            continue
        if loss <= trade_risk + 1e-6 and loss <= aggregate_left + 1e-6:
            return qty
        qty -= 1
    return 0
