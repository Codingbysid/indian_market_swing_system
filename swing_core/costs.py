"""Dated broker-cost helpers. Legacy BE floor is preserved separately."""

from __future__ import annotations

BE_COST_MULT = 1.0025
BE_FLAT_FEE = 18.0  # conservative legacy floor; not a substitute for dated DP

# Zerodha CNC (delivery) research defaults as of the 7 Oct 2026 spec.
STT_BUY = 0.001  # 0.1%
STT_SELL = 0.001
EXCHANGE_TXN = 0.0000297  # approx NSE equity delivery
GST_ON_EXCHANGE = 0.18
SEBI = 0.000001
STAMP_BUY = 0.00015
DP_SELL = 15.34  # generally once per scrip per day
SLIPPAGE_CORE = 0.001  # 10 bps/side
SLIPPAGE_SAT = 0.002


def entry_charges(price: float, qty: int, *, sleeve: str = "core") -> float:
    """Buy-side fees and slippage. DP is a sell-side charge and is not included."""
    qty = max(int(qty), 0)
    notional = max(float(price), 0.0) * qty
    if notional <= 0:
        return 0.0
    exch = EXCHANGE_TXN * notional
    slip = (SLIPPAGE_CORE if sleeve != "satellite" else SLIPPAGE_SAT) * notional
    return STT_BUY * notional + exch + GST_ON_EXCHANGE * exch + SEBI * notional + STAMP_BUY * notional + slip


def true_breakeven(avg_cost: float, qty: int) -> float:
    return float(avg_cost) * BE_COST_MULT + BE_FLAT_FEE / max(int(qty), 1)


def round_trip_charges(
    entry: float,
    exit_px: float,
    qty: int,
    *,
    sleeve: str = "core",
    include_slippage: bool = True,
) -> float:
    qty = max(int(qty), 0)
    buy_notional = entry * qty
    sell_notional = exit_px * qty
    stt = STT_BUY * buy_notional + STT_SELL * sell_notional
    exch = EXCHANGE_TXN * (buy_notional + sell_notional)
    gst = GST_ON_EXCHANGE * exch
    sebi = SEBI * (buy_notional + sell_notional)
    stamp = STAMP_BUY * buy_notional
    dp = DP_SELL if qty > 0 else 0.0
    slip = 0.0
    if include_slippage:
        bps = SLIPPAGE_CORE if sleeve != "satellite" else SLIPPAGE_SAT
        slip = bps * buy_notional + bps * sell_notional
    return stt + exch + gst + sebi + stamp + dp + slip


def pretrade_cost_r(
    price: float,
    atr: float,
    qty: int,
    *,
    stop_atr_mult: float = 1.5,
    sleeve: str = "core",
) -> float:
    """Round-trip fee estimate at an unchanged price, / planned stop risk."""
    if qty <= 0 or atr <= 0 or price <= 0:
        return float("nan")
    charges = round_trip_charges(price, price, qty, sleeve=sleeve, include_slippage=True)
    risk = qty * stop_atr_mult * atr
    if risk <= 0:
        return float("nan")
    return charges / risk
