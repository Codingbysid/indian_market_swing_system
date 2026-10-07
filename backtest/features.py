"""Feature engineering shared by the labeler and the trainer."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from swing_core.indicators import calculate_atr, calculate_rsi, enrich_ohlcv

FEATURE_NAMES = [
    "rsi",
    "rsi_slope",
    "atr_pct",
    "bb_position",
    "dist_sma50",
    "rel_volume",
    "regime_on",
]


def enrich_indicators(df: pd.DataFrame) -> pd.DataFrame:
    return enrich_ohlcv(df)


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
