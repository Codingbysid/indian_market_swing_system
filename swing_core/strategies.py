"""Six named setup families. Thresholds are research hypotheses in config."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .indicators import bb_position


def _f(s: pd.Series, i: int) -> float:
    if i < 0 or i >= len(s):
        return float("nan")
    return float(s.iloc[i])


def _finite(*vals: float) -> bool:
    return all(math.isfinite(v) for v in vals)


@dataclass(frozen=True)
class Family:
    family_id: str
    version: str
    status: str  # research | shadow | promoted
    hypothesis: str


REGISTRY: dict[str, Family] = {
    "ema_cross": Family("ema_cross", "1", "promoted", "Fresh EMA cross inside RSI/BB band."),
    "trend_pullback": Family("trend_pullback", "1", "shadow", "Pullback resume in a rising SMA50 trend."),
    "range_breakout": Family("range_breakout", "1", "shadow", "Close through the prior 20-session high."),
    "relative_strength_momentum": Family(
        "relative_strength_momentum", "1", "shadow", "Enters top residual-return quintile."
    ),
    "failed_breakdown_reclaim": Family(
        "failed_breakdown_reclaim", "1", "shadow", "Undercut of 10-session support that reclaims."
    ),
    "residual_shock_reversion": Family(
        "residual_shock_reversion", "1", "research", "Oversold residual shock then reclaim. Research only."
    ),
}

PROMOTED = {k for k, v in REGISTRY.items() if v.status == "promoted"}


def ema_cross(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    prev9, prev21 = _f(df["EMA_9"], i - 1), _f(df["EMA_21"], i - 1)
    ema9, ema21 = _f(df["EMA_9"], i), _f(df["EMA_21"], i)
    rsi = _f(df["RSI"], i)
    close = _f(df["Close"], i)
    bb = bb_position(close, _f(df["Lower_Band"], i), _f(df["Upper_Band"], i))
    if not _finite(prev9, prev21, ema9, ema21, rsi, bb, close):
        return False
    return prev9 <= prev21 and ema9 > ema21 and 40 <= rsi <= 65 and bb < 0.60


def trend_pullback(df: pd.DataFrame, i: int, nifty_ret20: float | None = None) -> bool:
    if i < 20:
        return False
    close = _f(df["Close"], i)
    sma50, sma50_prev = _f(df["SMA_50"], i), _f(df["SMA_50"], i - 10)
    ema9, ema21, atr = _f(df["EMA_9"], i), _f(df["EMA_21"], i), _f(df["ATR"], i)
    prev_high = _f(df["High"], i - 1)
    if not _finite(close, sma50, sma50_prev, ema9, ema21, atr, prev_high):
        return False
    if not (close > sma50 > sma50_prev and ema9 > ema21):
        return False
    touched = False
    for k in range(1, 6):
        low_k = _f(df["Low"], i - k)
        ema_k = _f(df["EMA_21"], i - k)
        atr_k = _f(df["ATR"], i - k)
        sma_k = _f(df["SMA_50"], i - k)
        cls_k = _f(df["Close"], i - k)
        if not _finite(low_k, ema_k, atr_k, sma_k, cls_k):
            return False
        if cls_k < sma_k:
            return False
        if ema_k - 0.75 * atr_k <= low_k <= ema_k + 0.25 * atr_k:
            touched = True
    if not touched:
        return False
    if not (close > prev_high and close > ema9):
        return False
    if close > ema21 + 1.5 * atr:
        return False
    stock_r = close / _f(df["Close"], i - 20) - 1.0
    if nifty_ret20 is None:
        return False
    return stock_r - nifty_ret20 > 0


def range_breakout(df: pd.DataFrame, i: int) -> bool:
    if i < 21:
        return False
    close = _f(df["Close"], i)
    sma50, sma50_prev = _f(df["SMA_50"], i), _f(df["SMA_50"], i - 10)
    ema21, atr, vol = _f(df["EMA_21"], i), _f(df["ATR"], i), _f(df["Volume"], i)
    if not _finite(close, sma50, sma50_prev, ema21, atr, vol):
        return False
    prior_high = float(df["High"].iloc[i - 20 : i].max())
    vol_med = float(df["Volume"].iloc[i - 20 : i].median())
    if not (close > prior_high and close > sma50 > sma50_prev):
        return False
    if vol_med <= 0 or vol < 1.3 * vol_med:
        return False
    if close > ema21 + 3.0 * atr:
        return False
    return True


def failed_breakdown(df: pd.DataFrame, i: int) -> bool:
    if i < 12:
        return False
    support = float(df["Low"].iloc[i - 10 : i].min())
    atr_prev = _f(df["ATR"], i - 1)
    low, close, prev_close = _f(df["Low"], i), _f(df["Close"], i), _f(df["Close"], i - 1)
    sma50, sma50_prev = _f(df["SMA_50"], i), _f(df["SMA_50"], i - 10)
    vol = _f(df["Volume"], i)
    vol_med = float(df["Volume"].iloc[i - 20 : i].median()) if i >= 20 else float("nan")
    tr = _f(df["High"], i) - low
    if not _finite(support, atr_prev, low, close, prev_close, sma50, sma50_prev, vol, vol_med, tr):
        return False
    if atr_prev <= 0 or vol_med <= 0:
        return False
    if not (support - 0.5 * atr_prev <= low < support):
        return False
    if not (close > support and close > prev_close):
        return False
    if not (close > sma50 > sma50_prev):
        return False
    if vol < vol_med or tr > 2.0 * atr_prev:
        return False
    return True


def residual_shock(df: pd.DataFrame, i: int, z: float | None) -> bool:
    if i < 200 or z is None or not math.isfinite(z):
        return False
    if z >= -2:
        return False
    close = _f(df["Close"], i)
    prev_high = _f(df["High"], i - 1)
    sma200 = _f(df["Close"], i)  # placeholder replaced below
    if "SMA_200" in df.columns:
        sma200 = _f(df["SMA_200"], i)
    else:
        sma200 = float(df["Close"].iloc[i - 199 : i + 1].mean())
    if not _finite(close, prev_high, sma200):
        return False
    return close > prev_high and close > sma200


def detect(
    df: pd.DataFrame,
    i: int,
    *,
    nifty_ret20: float | None = None,
    rs_enter_top_quintile: bool = False,
    residual_z: float | None = None,
) -> list[str]:
    found = []
    if ema_cross(df, i):
        found.append("ema_cross")
    if trend_pullback(df, i, nifty_ret20=nifty_ret20):
        found.append("trend_pullback")
    if range_breakout(df, i):
        found.append("range_breakout")
    if rs_enter_top_quintile:
        found.append("relative_strength_momentum")
    if failed_breakdown(df, i):
        found.append("failed_breakdown_reclaim")
    if residual_shock(df, i, residual_z):
        found.append("residual_shock_reversion")
    return found


def primary_family(flags: list[str]) -> str | None:
    order = (
        "ema_cross",
        "trend_pullback",
        "range_breakout",
        "relative_strength_momentum",
        "failed_breakdown_reclaim",
        "residual_shock_reversion",
    )
    for name in order:
        if name in flags:
            return name
    return None
