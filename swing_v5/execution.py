"""Next-session fills. An open outside the band, or through the stop, is unfilled."""

from __future__ import annotations

import math

import pandas as pd

from .costs import round_trip


def stop_distance(entry: float, atr: float, prior_low: float, *, atr_mult: float, max_mult: float) -> float | None:
    if atr <= 0 or not math.isfinite(entry):
        return None
    distance = max(atr_mult * atr, entry - prior_low + 0.1 * atr)
    if distance > max_mult * atr:
        return None
    return distance


def label_trade(
    frame: pd.DataFrame,
    signal_i: int,
    *,
    horizon: str,
    qty: int = 1,
    slippage_bps: float = 10.0,
) -> dict:
    atr = float(frame["ATR"].iloc[signal_i])
    close = float(frame["Close"].iloc[signal_i])
    if atr <= 0 or close <= 0 or signal_i + 1 >= len(frame):
        return {"status": "invalid", "label": None}
    entry_open = float(frame["Open"].iloc[signal_i + 1])
    band_high = close + 0.25 * atr
    planned_stop = close - (1.5 if horizon == "short" else 2.0) * atr
    if entry_open < close or entry_open > band_high or entry_open <= planned_stop:
        return {"status": "unfilled", "label": None, "reason": "outside_band_or_through_stop"}
    lookback = 5 if horizon == "short" else 10
    prior_low = float(frame["Low"].iloc[signal_i - lookback + 1 : signal_i + 1].min())
    if horizon == "short":
        distance = stop_distance(entry_open, atr, prior_low, atr_mult=1.5, max_mult=2.5)
        vertical = 15
        target_r = 2.0
    else:
        distance = stop_distance(entry_open, atr, prior_low, atr_mult=2.0, max_mult=3.0)
        vertical = 40
        target_r = 3.0
    if distance is None:
        return {"status": "rejected_wide_stop", "label": None}
    stop = entry_open - distance
    target = entry_open + target_r * distance
    last = min(len(frame) - 1, signal_i + vertical)
    mature = signal_i + vertical < len(frame)
    for j in range(signal_i + 1, last + 1):
        session = j - signal_i
        op = float(frame["Open"].iloc[j])
        lo = float(frame["Low"].iloc[j])
        hi = float(frame["High"].iloc[j])
        if frame["Close"].iloc[j] != frame["Close"].iloc[j]:
            return {"status": "pending_untradable", "label": None}
        if op <= stop:
            return _economics(entry_open, op, qty, distance, session, "stop_gap", slippage_bps, frame, signal_i)
        hit_stop = lo <= stop
        hit_target = hi >= target
        if hit_stop and hit_target:
            return _economics(entry_open, stop, qty, distance, session, "stop_same_bar", slippage_bps, frame, signal_i)
        if hit_stop:
            return _economics(entry_open, stop, qty, distance, session, "stop", slippage_bps, frame, signal_i)
        if hit_target:
            return _economics(entry_open, target, qty, distance, session, "target", slippage_bps, frame, signal_i)
        if session >= vertical:
            return _economics(entry_open, float(frame["Close"].iloc[j]), qty, distance, session, "vertical" if mature else "censored", slippage_bps, frame, signal_i, censored=not mature)
    return {"status": "censored", "label": None}


def _economics(entry, exit_px, qty, distance, session, reason, slippage_bps, frame, signal_i, censored=False) -> dict:
    day = str(pd.Timestamp(frame["Date"].iloc[signal_i]).date()) if "Date" in frame.columns else "2026-10-01"
    fees = round_trip(entry, exit_px, qty, day=day, slippage_bps=slippage_bps)
    net = qty * (exit_px - entry) - fees
    risk = qty * distance
    net_r = net / risk if risk else 0.0
    return {
        "status": "censored" if censored else "filled",
        "reason": reason,
        "label": None if censored else int(net > 0),
        "net_pnl": None if censored else net,
        "net_r": None if censored else net_r,
        "fees": fees,
        "entry": entry,
        "exit": exit_px,
        "sessions": session,
    }
