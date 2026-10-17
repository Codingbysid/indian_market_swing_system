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


def _day(frame: pd.DataFrame, index: int) -> str | None:
    if "Date" not in frame.columns:
        return None
    value = frame["Date"].iloc[index]
    if pd.isna(value):
        return None
    return str(pd.Timestamp(value).date())


def _bar_ok(frame: pd.DataFrame, index: int) -> bool:
    try:
        opened = float(frame["Open"].iloc[index])
        high = float(frame["High"].iloc[index])
        low = float(frame["Low"].iloc[index])
        close = float(frame["Close"].iloc[index])
    except (TypeError, ValueError, KeyError):
        return False
    if not all(math.isfinite(value) and value > 0 for value in (opened, high, low, close)):
        return False
    return low <= min(opened, close) and high >= max(opened, close) and low <= high


def label_trade(
    frame: pd.DataFrame,
    signal_i: int,
    *,
    horizon: str,
    qty: int = 1,
    slippage_bps: float = 10.0,
) -> dict:
    if horizon not in ("short", "long"):
        raise ValueError(f"unknown horizon: {horizon}")
    if int(qty) <= 0:
        raise ValueError("quantity must be positive")
    signal_date = _day(frame, signal_i)
    if signal_date is None:
        return {"status": "invalid", "label": None, "reason": "missing_transaction_date"}
    if not _bar_ok(frame, signal_i) or signal_i + 1 >= len(frame) or not _bar_ok(frame, signal_i + 1):
        return {"status": "invalid", "label": None, "reason": "malformed_ohlc"}
    atr = float(frame["ATR"].iloc[signal_i])
    close = float(frame["Close"].iloc[signal_i])
    if not math.isfinite(atr) or atr <= 0 or close <= 0:
        return {"status": "invalid", "label": None, "reason": "invalid_atr"}
    fill_i = signal_i + 1
    if "Tradable" in frame.columns and frame["Tradable"].iloc[fill_i] == False:
        return {
            "status": "pending_untradable",
            "label": None,
            "reason": "circuit_or_halt_no_fabricated_fill",
            "signal_date": signal_date,
            "fill_date": _day(frame, fill_i),
        }
    entry_open = float(frame["Open"].iloc[fill_i])
    band_high = close + 0.25 * atr
    planned_stop = close - (1.5 if horizon == "short" else 2.0) * atr
    if entry_open < close or entry_open > band_high or entry_open <= planned_stop:
        return {
            "status": "unfilled",
            "label": None,
            "reason": "outside_band_or_through_stop",
            "signal_date": signal_date,
            "fill_date": _day(frame, fill_i),
        }
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
    if distance is None or not math.isfinite(distance):
        return {"status": "rejected_wide_stop", "label": None, "signal_date": signal_date}
    stop = entry_open - distance
    target = entry_open + target_r * distance
    last = min(len(frame) - 1, signal_i + vertical)
    mature = signal_i + vertical < len(frame)
    for j in range(fill_i, last + 1):
        session = j - signal_i
        if "Tradable" in frame.columns and frame["Tradable"].iloc[j] == False:
            return {
                "status": "pending_untradable",
                "label": None,
                "reason": "circuit_or_halt_no_fabricated_fill",
                "signal_date": signal_date,
                "stop": stop,
                "target": target,
                "qty": int(qty),
            }
        op = float(frame["Open"].iloc[j])
        lo = float(frame["Low"].iloc[j])
        hi = float(frame["High"].iloc[j])
        if op <= stop:
            return _economics(entry_open, op, qty, distance, session, "stop_gap", slippage_bps, frame, fill_i, j, stop, target)
        hit_stop = lo <= stop
        hit_target = hi >= target
        if hit_stop and hit_target:
            return _economics(entry_open, stop, qty, distance, session, "stop_same_bar", slippage_bps, frame, fill_i, j, stop, target)
        if hit_stop:
            return _economics(entry_open, stop, qty, distance, session, "stop", slippage_bps, frame, fill_i, j, stop, target)
        if hit_target:
            return _economics(entry_open, target, qty, distance, session, "target", slippage_bps, frame, fill_i, j, stop, target)
        if session >= vertical:
            censored = not mature
            return _economics(
                entry_open,
                float(frame["Close"].iloc[j]),
                qty,
                distance,
                session,
                "censored" if censored else "vertical",
                slippage_bps,
                frame,
                fill_i,
                j,
                stop,
                target,
                censored=censored,
            )
    return {"status": "censored", "label": None, "signal_date": signal_date, "stop": stop, "target": target, "qty": int(qty)}


def reprice(entry: float, exit_px: float, qty: int, distance: float, *, entry_day: str, exit_day: str, slippage_bps: float = 10.0) -> dict:
    """Costs are recomputed at the admitted quantity. A one-share net R is not scaled."""
    from .costs import buy_fees, sell_fees

    if qty <= 0 or distance <= 0 or not all(math.isfinite(value) for value in (entry, exit_px, distance)):
        raise ValueError("invalid reprice inputs")
    fees = buy_fees(entry, qty, day=entry_day, slippage_bps=slippage_bps) + sell_fees(
        exit_px, qty, day=exit_day, slippage_bps=slippage_bps
    )
    net = qty * (exit_px - entry) - fees
    return {"fees": fees, "net_pnl": net, "net_r": net / (qty * distance), "qty": qty}


def _economics(entry, exit_px, qty, distance, session, reason, slippage_bps, frame, fill_i, exit_i, stop, target, censored=False) -> dict:
    entry_day = _day(frame, fill_i)
    exit_day = _day(frame, exit_i)
    signal_day = _day(frame, fill_i - 1)
    if entry_day is None or exit_day is None:
        return {"status": "invalid", "label": None, "reason": "missing_transaction_date"}
    priced = reprice(entry, exit_px, int(qty), distance, entry_day=entry_day, exit_day=exit_day, slippage_bps=slippage_bps)
    return {
        "status": "censored" if censored else "filled",
        "reason": reason,
        "label": None if censored else int(priced["net_pnl"] > 0),
        "net_pnl": None if censored else priced["net_pnl"],
        "net_r": None if censored else priced["net_r"],
        "fees": priced["fees"],
        "entry": entry,
        "exit": None if censored else exit_px,
        "sessions": session,
        "signal_date": signal_day,
        "fill_date": entry_day,
        "exit_date": None if censored else exit_day,
        "stop": stop,
        "target": target,
        "qty": int(qty),
    }
