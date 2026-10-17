"""Q, T and V hypotheses. Fundamental families do not fire without a known snapshot."""

from __future__ import annotations

import math

import pandas as pd

from .types import DEFAULT_REGISTRY


def _finite(value: float) -> bool:
    return value == value and math.isfinite(value)


def quality_continuation(frame: pd.DataFrame, i: int, *, member: bool, rel63: float) -> bool:
    if not member or i < 200:
        return False
    close = float(frame["Close"].iloc[i])
    sma50 = float(frame["SMA50"].iloc[i])
    sma200 = float(frame["SMA200"].iloc[i])
    sma_prev = float(frame["SMA50"].iloc[i - 5])
    ema = float(frame["EMA20"].iloc[i])
    atr = float(frame["ATR"].iloc[i])
    if not all(_finite(x) for x in (close, sma50, sma200, sma_prev, ema, atr, rel63)):
        return False
    if not (close > sma50 > sma200 and sma50 > sma_prev and rel63 > 0):
        return False
    touched = False
    for k in range(1, 4):
        if float(frame["Close"].iloc[i - k]) < float(frame["SMA50"].iloc[i - k]):
            return False
        if float(frame["Low"].iloc[i - k]) <= float(frame["EMA20"].iloc[i - k]):
            touched = True
    if not touched:
        return False
    prior_high = float(frame["High"].iloc[i - 1])
    vol_med = float(frame["Volume"].iloc[i - 20 : i].median())
    if not (close > prior_high and close > ema and vol_med > 0 and float(frame["Volume"].iloc[i]) >= vol_med):
        return False
    return close <= ema + atr


def trend_transition(frame: pd.DataFrame, i: int, *, rel63: float) -> bool:
    if i < 220:
        return False
    close = float(frame["Close"].iloc[i])
    sma200 = float(frame["SMA200"].iloc[i])
    sma200_prev = float(frame["SMA200"].iloc[i - 20])
    atr = float(frame["ATR"].iloc[i])
    if not all(_finite(x) for x in (close, sma200, sma200_prev, rel63, atr)) or rel63 <= 0:
        return False
    if sma200 < sma200_prev:
        return False
    window = frame.iloc[i - 9 : i + 1]
    prev = window["SMA50"].shift(1)
    if not bool(((window["SMA50"] > window["SMA200"]) & (prev <= window["SMA200"])).fillna(False).any()):
        return False
    prior_high = float(frame["High"].iloc[i - 20 : i].max())
    vol_med = float(frame["Volume"].iloc[i - 20 : i].median())
    return close > prior_high and vol_med > 0 and float(frame["Volume"].iloc[i]) >= 1.2 * vol_med


def improving_value(frame: pd.DataFrame, i: int, *, member: bool, financial: bool, rel63: float, piotroski: float | None) -> bool:
    if not member or financial or i < 50:
        return False
    if piotroski is not None and piotroski < 6:
        return False
    close = float(frame["Close"].iloc[i])
    sma50 = float(frame["SMA50"].iloc[i])
    atr = float(frame["ATR"].iloc[i])
    if not all(_finite(x) for x in (close, sma50, atr, rel63)) or rel63 <= 0 or not close > sma50:
        return False
    prior_high = float(frame["High"].iloc[i - 20 : i].max())
    return close > prior_high and close <= sma50 + 1.5 * atr


def owner_family(flags: list[str], expectations: dict[str, float] | None = None) -> str | None:
    if not flags:
        return None
    if expectations:
        return max(flags, key=lambda name: expectations.get(name, -1e9))
    order = ("quality_continuation", "trend_transition", "improving_value", "trend_transition_price_control")
    for name in order:
        if name in flags:
            return name
    return flags[0]


def promoted(family: str, registry: dict | None = None) -> bool:
    registry = registry or DEFAULT_REGISTRY
    entry = registry.get(family)
    return bool(entry and entry.status == "promoted")
