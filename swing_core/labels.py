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
    label: int | None  # None = censored/pending
    reason: str
    net_r: float
    gross_r: float
    exit_price: float | None
    bars_held: int


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
    if atr <= 0 or signal_close <= 0:
        return BarrierResult(0, "bad_atr", 0.0, 0.0, None, 0)

    nxt_open = float(df["Open"].iloc[i + 1]) * fill_mult
    if nxt_open <= 0:
        return BarrierResult(None, "unfilled", 0.0, 0.0, None, 0)

    planned_stop = signal_close - SL_MULT * atr
    # Gap through the signal-time stop: fill at the obtainable open (can be worse than -1R).
    if nxt_open <= planned_stop:
        entry = signal_close
        stop = planned_stop
        target = signal_close + TP_MULT * atr
        def _gap_econ():
            gross = qty * (nxt_open - entry)
            charges = round_trip_charges(entry, nxt_open, qty, sleeve=sleeve)
            net = gross - charges
            risk = qty * SL_MULT * atr
            return BarrierResult(
                0,
                "stop_gap",
                float(net / risk) if risk else 0.0,
                float((nxt_open - entry) / (SL_MULT * atr)),
                nxt_open,
                1,
            )
        return _gap_econ()

    entry = nxt_open
    # Skip if the open chased more than 0.5 ATR from the signal close.
    if abs(entry - signal_close) > 0.5 * atr:
        return BarrierResult(None, "unfilled_gap_chase", 0.0, 0.0, None, 0)

    stop = entry - SL_MULT * atr
    target = entry + TP_MULT * atr
    end = min(len(df) - 1, i + horizon)
    mature = (i + horizon) <= (len(df) - 1)

    def _economics(exit_px: float, bars: int, reason: str, label: int | None) -> BarrierResult:
        gross = qty * (exit_px - entry)
        charges = round_trip_charges(entry, exit_px, qty, sleeve=sleeve)
        net = gross - charges
        risk = qty * SL_MULT * atr
        net_r = net / risk if risk else 0.0
        gross_r = (exit_px - entry) / (SL_MULT * atr)
        y = None if label is None else int(net_r > 0)
        return BarrierResult(y, reason, float(net_r), float(gross_r), exit_px, bars)

    for j in range(i + 1, end + 1):
        op = float(df["Open"].iloc[j])
        lo = float(df["Low"].iloc[j])
        hi = float(df["High"].iloc[j])
        bars = j - i
        # Gap through stop at the open: fill at obtainable open.
        if op <= stop:
            return _economics(op, bars, "stop_gap", 0)
        if op >= target:
            return _economics(min(op, target), bars, "target", 1)
        hit_sl = lo <= stop
        hit_tp = hi >= target
        if hit_sl and hit_tp:
            return _economics(stop, bars, "stop", 0)  # ambiguous bar: SL wins
        if hit_sl:
            return _economics(stop, bars, "stop", 0)
        if hit_tp:
            return _economics(target, bars, "target", 1)

    last = float(df["Close"].iloc[end])
    bars = end - i
    if not mature:
        return _economics(last, bars, "censored", None)
    # Vertical / timeout: keep mark-to-market R; label from net profit.
    return _economics(last, bars, "vertical", int(last > entry))
