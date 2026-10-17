"""Shared new-sleeve limits. Legacy marks are not the new-trade loss counter."""

from __future__ import annotations

import math


def money(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def sleeve_pnl(nav: float, launch_nav: float, external_inflows: float) -> float:
    """P = N - N0 - net external inflows. Deposits are flows, not recovered losses."""
    return float(nav) - float(launch_nav) - float(external_inflows)


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
) -> int:
    """Search integers. Fees are recomputed at each quantity, not added after a division."""
    from .costs import buy_fees, round_trip

    if entry <= 0 or not math.isfinite(entry) or gap_buffer < 0 or not math.isfinite(gap_buffer):
        return 0
    if gap_buffer == 0 and stop >= entry:
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
        if price_risk <= 0 or fees > max_cost_fraction * price_risk:
            qty -= 1
            continue
        if loss <= trade_risk + 1e-6 and loss <= aggregate_left + 1e-6:
            return qty
        qty -= 1
    return 0
