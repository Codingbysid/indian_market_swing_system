"""Completed-bar setup families. Same functions for Lambda and research."""

from __future__ import annotations

import math

import pandas as pd

from .indicators import bb_position


def _f(series: pd.Series, i: int) -> float:
    return float(series.iloc[i])


def setup_cross(df: pd.DataFrame, i: int) -> bool:
    """Family A: fresh EMA9/21 cross + RSI 40-65 + BB% < 0.60."""
    if i < 1:
        return False
    prev_ema9 = _f(df["EMA_9"], i - 1)
    prev_ema21 = _f(df["EMA_21"], i - 1)
    ema9 = _f(df["EMA_9"], i)
    ema21 = _f(df["EMA_21"], i)
    rsi = _f(df["RSI"], i)
    close = _f(df["Close"], i)
    bb = bb_position(close, _f(df["Lower_Band"], i), _f(df["Upper_Band"], i))
    if any(map(math.isnan, (prev_ema9, prev_ema21, ema9, ema21, rsi, bb))):
        return False
    return (
        prev_ema9 <= prev_ema21
        and ema9 > ema21
        and 40 <= rsi <= 65
        and bb < 0.60
    )


def setup_pullback(df: pd.DataFrame, i: int, nifty: pd.DataFrame | None = None) -> bool:
    """Family B: trend-pullback resumption. No fresh cross. No BB<0.60 gate."""
    if i < 10:
        return False
    close = _f(df["Close"], i)
    sma50 = _f(df["SMA_50"], i)
    sma50_prev = _f(df["SMA_50"], i - 10)
    ema9 = _f(df["EMA_9"], i)
    ema21 = _f(df["EMA_21"], i)
    atr = _f(df["ATR"], i)
    prev_high = _f(df["High"], i - 1)
    if any(map(math.isnan, (close, sma50, sma50_prev, ema9, ema21, atr, prev_high))):
        return False
    if not (close > sma50 and sma50 > sma50_prev):
        return False
    if not (ema9 > ema21):
        return False
    touched = False
    lookback = min(5, i)
    for k in range(1, lookback + 1):
        low_k = _f(df["Low"], i - k)
        ema21_k = _f(df["EMA_21"], i - k)
        atr_k = _f(df["ATR"], i - k)
        if math.isnan(low_k) or math.isnan(ema21_k) or math.isnan(atr_k):
            continue
        if low_k <= ema21_k + 0.25 * atr_k:
            touched = True
            break
    if not touched:
        return False
    if not (close > prev_high and close > ema9):
        return False
    if close > ema21 + 1.5 * atr:
        return False
    if nifty is not None and len(nifty) > i:
        stock_r = close / _f(df["Close"], i - 20) - 1.0 if i >= 20 else 0.0
        n_close = _f(nifty["Close"], min(i, len(nifty) - 1))
        n_prev = _f(nifty["Close"], max(0, min(i, len(nifty) - 1) - 20))
        nifty_r = n_close / n_prev - 1.0 if n_prev else 0.0
        if stock_r - nifty_r <= 0:
            return False
    elif i >= 20:
        # Without nifty, still require a positive 20-session stock return.
        if close / _f(df["Close"], i - 20) - 1.0 <= 0:
            return False
    return True


def setup_breakout(df: pd.DataFrame, i: int) -> bool:
    """Family C: 20-session range breakout. BB may exceed 0.60. Current high excluded."""
    if i < 21:
        return False
    close = _f(df["Close"], i)
    sma50 = _f(df["SMA_50"], i)
    sma50_prev = _f(df["SMA_50"], i - 10)
    vol = _f(df["Volume"], i)
    if any(map(math.isnan, (close, sma50, sma50_prev, vol))):
        return False
    prior_high = float(df["High"].iloc[i - 20 : i].max())  # d-20 through d-1
    if close <= prior_high:
        return False
    if not (close > sma50 and sma50 > sma50_prev):
        return False
    prior_vol = df["Volume"].iloc[i - 20 : i]
    vol_med = float(prior_vol.median())
    if vol_med <= 0 or vol < 1.3 * vol_med:
        return False
    if i >= 20:
        if close / _f(df["Close"], i - 20) - 1.0 <= 0:
            return False
    return True


def detect_setups(
    df: pd.DataFrame, i: int, nifty: pd.DataFrame | None = None
) -> list[str]:
    """Return qualifying family ids for a completed-bar index."""
    found: list[str] = []
    if setup_cross(df, i):
        found.append("cross")
    if setup_pullback(df, i, nifty=nifty):
        found.append("pullback")
    if setup_breakout(df, i):
        found.append("breakout")
    return found
