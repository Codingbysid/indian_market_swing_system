"""Single RSI/ATR/Bollinger implementation for live and research."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def calculate_rsi(close: pd.Series, periods: int = 14) -> pd.Series:
    """Wilder RSI. First delta is NaN (offline contract). Do not seed with zeros."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / periods, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / periods, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, periods: int = 14) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / periods, adjust=False).mean()


def enrich_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["Close"].astype(float)
    out["EMA_9"] = close.ewm(span=9, adjust=False).mean()
    out["EMA_21"] = close.ewm(span=21, adjust=False).mean()
    out["RSI"] = calculate_rsi(close)
    out["ATR"] = calculate_atr(out)
    out["SMA_20"] = close.rolling(20).mean()
    out["SMA_50"] = close.rolling(50).mean()
    out["SMA_200"] = close.rolling(200).mean()
    out["Std_Dev"] = close.rolling(20).std()
    out["Lower_Band"] = out["SMA_20"] - 2 * out["Std_Dev"]
    out["Upper_Band"] = out["SMA_20"] + 2 * out["Std_Dev"]
    out["Vol_MA20"] = out["Volume"].rolling(20).mean()
    return out


def bb_position(close: float, lower: float, upper: float) -> float:
    width = upper - lower
    if width <= 0 or math.isnan(width):
        return 1.0
    return (close - lower) / width
