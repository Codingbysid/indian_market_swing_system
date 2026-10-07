"""Versioned 20-column feature schema. regime_on is metadata, not a model column."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

FEATURE_NAMES_V3 = [
    "rsi14",
    "rsi_change3",
    "ema21_slope5_atr",
    "dist_ema21_atr",
    "atr_pct",
    "volatility_ratio63",
    "rel_volume20",
    "log_turnover20",
    "last_gap_atr",
    "downside_gap90_60",
    "beta60",
    "residual_return20",
    "nifty_distance50_atr",
    "breadth50",
    "cost_R",
    "setup_pullback",
    "setup_breakout",
    "setup_relative_strength",
    "setup_failed_breakdown",
    "setup_residual_reversion",
]

SCHEMA_ID = "features_v3_20"


def _finite_map(raw: dict) -> dict | None:
    out = {}
    for name in FEATURE_NAMES_V3:
        if name not in raw:
            return None
        val = float(raw[name])
        if not math.isfinite(val):
            return None
        out[name] = val
    return out


def features_at(
    df: pd.DataFrame,
    i: int,
    *,
    nifty: pd.DataFrame | None,
    breadth50: float,
    cost_r: float,
    flags: list[str],
) -> dict | None:
    if i < 63:
        return None
    close = float(df["Close"].iloc[i])
    atr = float(df["ATR"].iloc[i])
    if close <= 0 or atr <= 0:
        return None
    rsi = float(df["RSI"].iloc[i])
    rsi_prev = float(df["RSI"].iloc[i - 3])
    ema21 = float(df["EMA_21"].iloc[i])
    ema21_5 = float(df["EMA_21"].iloc[i - 5])
    rets = np.log(df["Close"].astype(float)).diff()
    vol_s = float(rets.iloc[i - 10 : i + 1].std())
    vol_l = float(rets.iloc[i - 63 : i + 1].std())
    vol_med = float(df["Volume"].iloc[i - 20 : i].median())
    turn = (df["Close"].iloc[i - 19 : i + 1] * df["Volume"].iloc[i - 19 : i + 1]).median()
    gap = (float(df["Open"].iloc[i]) - float(df["Close"].iloc[i - 1])) / float(df["ATR"].iloc[i - 1])
    adverse = []
    for k in range(i - 59, i + 1):
        if k < 1:
            continue
        g = (float(df["Close"].iloc[k - 1]) - float(df["Open"].iloc[k])) / max(float(df["ATR"].iloc[k - 1]), 1e-9)
        adverse.append(max(0.0, g))
    down = float(np.percentile(adverse, 90)) if len(adverse) >= 30 else float("nan")
    beta = 1.0
    resid = 0.0
    nifty_dist = 0.0
    if nifty is not None and len(nifty) > i:
        # Caller must pass a date-aligned frame of equal length.
        s = np.log(df["Close"].astype(float)).diff().iloc[i - 59 : i + 1]
        n = np.log(nifty["Close"].astype(float)).diff().iloc[i - 59 : i + 1]
        if len(s) == len(n) and float(n.var()) > 0:
            beta = float(np.cov(s.fillna(0), n.fillna(0))[0, 1] / n.var())
            beta = 0.5 * beta + 0.5 * 1.0
        stock_r = close / float(df["Close"].iloc[i - 20]) - 1.0
        nifty_r = float(nifty["Close"].iloc[i]) / float(nifty["Close"].iloc[i - 20]) - 1.0
        resid = stock_r - beta * nifty_r
        if "EMA_50" in nifty.columns or "SMA_50" in nifty.columns:
            ema = float((nifty["EMA_50"] if "EMA_50" in nifty.columns else nifty["SMA_50"]).iloc[i])
            natr = float(nifty["ATR"].iloc[i]) if "ATR" in nifty.columns else atr
            nifty_dist = (float(nifty["Close"].iloc[i]) - ema) / natr if natr else float("nan")
    raw = {
        "rsi14": rsi,
        "rsi_change3": rsi - rsi_prev,
        "ema21_slope5_atr": (ema21 - ema21_5) / atr,
        "dist_ema21_atr": (close - ema21) / atr,
        "atr_pct": atr / close,
        "volatility_ratio63": vol_s / vol_l if vol_l else float("nan"),
        "rel_volume20": float(df["Volume"].iloc[i]) / vol_med if vol_med else float("nan"),
        "log_turnover20": math.log1p(float(turn)) if turn == turn else float("nan"),
        "last_gap_atr": gap,
        "downside_gap90_60": down,
        "beta60": beta,
        "residual_return20": resid,
        "nifty_distance50_atr": nifty_dist,
        "breadth50": float(breadth50),
        "cost_R": float(cost_r),
        "setup_pullback": 1.0 if "trend_pullback" in flags else 0.0,
        "setup_breakout": 1.0 if "range_breakout" in flags else 0.0,
        "setup_relative_strength": 1.0 if "relative_strength_momentum" in flags else 0.0,
        "setup_failed_breakdown": 1.0 if "failed_breakdown_reclaim" in flags else 0.0,
        "setup_residual_reversion": 1.0 if "residual_shock_reversion" in flags else 0.0,
    }
    return _finite_map(raw)
