"""Feature engineering shared by the labeler and the trainer."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "rsi",
    "rsi_slope",
    "atr_pct",
    "bb_position",
    "dist_sma50",
    "rel_volume",
    "regime_on",
]


def calculate_rsi(close: pd.Series, periods: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / periods, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / periods, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, periods: int = 14) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.ewm(alpha=1 / periods, adjust=False).mean()


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["EMA_9"] = out["Close"].ewm(span=9, adjust=False).mean()
    out["EMA_21"] = out["Close"].ewm(span=21, adjust=False).mean()
    out["RSI"] = calculate_rsi(out["Close"])
    out["ATR"] = calculate_atr(out)
    out["SMA_20"] = out["Close"].rolling(20).mean()
    out["SMA_50"] = out["Close"].rolling(50).mean()
    out["Std_Dev"] = out["Close"].rolling(20).std()
    out["Lower_Band"] = out["SMA_20"] - 2 * out["Std_Dev"]
    out["Upper_Band"] = out["SMA_20"] + 2 * out["Std_Dev"]
    out["Vol_MA20"] = out["Volume"].rolling(20).mean()
    return out


def bb_position(close: float, lower: float, upper: float) -> float:
    width = upper - lower
    if width <= 0 or math.isnan(width):
        return 1.0
    return (close - lower) / width


def features_at_index(df: pd.DataFrame, i: int, regime_on: float) -> dict:
    close = float(df["Close"].iloc[i])
    rsi = float(df["RSI"].iloc[i])
    rsi_prev = float(df["RSI"].iloc[i - 14]) if i >= 14 else float(df["RSI"].iloc[0])
    atr = float(df["ATR"].iloc[i])
    lower = float(df["Lower_Band"].iloc[i])
    upper = float(df["Upper_Band"].iloc[i])
    sma50 = float(df["SMA_50"].iloc[i])
    vol = float(df["Volume"].iloc[i])
    vol_ma = float(df["Vol_MA20"].iloc[i])

    return {
        "rsi": rsi,
        "rsi_slope": rsi - rsi_prev,
        "atr_pct": atr / close if close else 0.0,
        "bb_position": bb_position(close, lower, upper),
        "dist_sma50": ((close - sma50) / close) if close and not math.isnan(sma50) else 0.0,
        "rel_volume": (vol / vol_ma) if vol_ma and not math.isnan(vol_ma) else 1.0,
        "regime_on": float(regime_on),
    }


def build_nifty_regime_series(nifty: pd.DataFrame) -> pd.Series:
    """Daily RISK_ON flag (1/0) from Nifty close vs 50-EMA, indexed by date."""
    n = nifty.copy()
    if "Date" in n.columns:
        n["Date"] = pd.to_datetime(n["Date"]).dt.tz_localize(None).dt.normalize()
        n = n.set_index("Date").sort_index()
    close = n["Close"].astype(float)
    ema50 = close.ewm(span=50, adjust=False).mean()
    regime = (close > ema50).astype(float)
    regime.name = "regime_on"
    return regime
