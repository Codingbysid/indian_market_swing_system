"""Economic triple-barrier labels with gap fills and right-censoring."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .costs import round_trip_charges

SL_MULT = 1.5
TP_MULT = 3.0
HORIZON = 10


@dataclass
class BarrierResult:
    label: int | None  # None = censored, unfilled, or invalid
    reason: str
    net_r: float
    gross_r: float
    exit_price: float | None
    bars_held: int
    entry_price: float | None = None
    net_inr: float | None = None
    fees: float | None = None
    mae_r: float | None = None
    mfe_r: float | None = None


def label_triple_barrier(
    df: pd.DataFrame,
    i: int,
    *,
    qty: int = 1,
    fill_mult: float = 1.0,
    sleeve: str = "core",
    horizon: int = HORIZON,
) -> BarrierResult:
    """Label from completed signal bar i. Entry is next bar's Open * fill_mult.

    Incomplete windows (not enough future bars for the vertical exit) are
    censored (label=None) unless a real stop/target already occurred.
    Opening gaps through the stop fill at the obtainable Open, so R can be < -1.
    Same-bar SL and TP → SL wins.
    """
    if i < 0 or i + 1 >= len(df):
        return BarrierResult(None, "censored_no_entry_bar", 0.0, 0.0, None, 0)

    atr = float(df["ATR"].iloc[i])
    signal_close = float(df["Close"].iloc[i])
    if atr <= 0 or signal_close <= 0 or not (atr == atr and signal_close == signal_close):
        return BarrierResult(None, "invalid_price", 0.0, 0.0, None, 0)

    nxt_open = float(df["Open"].iloc[i + 1]) * fill_mult
    if nxt_open <= 0:
        return BarrierResult(None, "unfilled", 0.0, 0.0, None, 0)

    # No fill, and no loss, if the next open is outside the entry band.
    # A gap through the planned stop before entry is UNFILLED, not a phantom trade.
    if abs(nxt_open - signal_close) > 0.5 * atr:
        return BarrierResult(None, "unfilled", 0.0, 0.0, None, 0)

    entry = nxt_open

    stop = entry - SL_MULT * atr
    target = entry + TP_MULT * atr
    end = min(len(df) - 1, i + horizon)
    mature = (i + horizon) <= (len(df) - 1)

    risk_unit = SL_MULT * atr

    def _economics(exit_px: float, bars: int, reason: str, label: int | None, mae: float, mfe: float) -> BarrierResult:
        gross = qty * (exit_px - entry)
        charges = round_trip_charges(entry, exit_px, qty, sleeve=sleeve)
        net = gross - charges
        risk = qty * risk_unit
        net_r = net / risk if risk else 0.0
        gross_r = (exit_px - entry) / risk_unit
        y = None if label is None else int(net_r > 0)
        return BarrierResult(
            y, reason, float(net_r), float(gross_r), exit_px, bars,
            entry, float(net), float(charges), float(mae), float(mfe),
        )

    mae = 0.0
    mfe = 0.0
    for j in range(i + 1, end + 1):
        op = float(df["Open"].iloc[j])
        lo = float(df["Low"].iloc[j])
        hi = float(df["High"].iloc[j])
        bars = j - i
        mae = min(mae, (min(op, lo) - entry) / risk_unit)
        mfe = max(mfe, (max(op, hi) - entry) / risk_unit)
        # Gap through stop at the open: fill at obtainable open.
        if op <= stop:
            return _economics(op, bars, "stop_gap", 0, mae, mfe)
        if op >= target:
            return _economics(min(op, target), bars, "target", 1, mae, mfe)
        hit_sl = lo <= stop
        hit_tp = hi >= target
        if hit_sl and hit_tp:
            return _economics(stop, bars, "stop", 0, mae, mfe)  # ambiguous bar: SL wins
        if hit_sl:
            return _economics(stop, bars, "stop", 0, mae, mfe)
        if hit_tp:
            return _economics(target, bars, "target", 1, mae, mfe)

    last = float(df["Close"].iloc[end])
    bars = end - i
    if not mature:
        return _economics(last, bars, "censored", None, mae, mfe)
    # Vertical exit keeps actual net economics. label = 1 only if net profit > 0.
    return _economics(last, bars, "vertical", 1, mae, mfe)
