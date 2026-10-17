"""Versioned feature schemas. Membership flags are omitted from the price-only contract."""

from __future__ import annotations

import hashlib
import math

import numpy as np
import pandas as pd

FEATURES_18 = [
    "atr_pct",
    "dist_ema20_atr",
    "sma50_slope5_atr",
    "drawdown_63_high",
    "return_20",
    "rel_return_63",
    "rel_return_126",
    "beta60",
    "rel_volume20",
    "log_turnover60",
    "downside_gap95_63_atr",
    "volatility_ratio20_63",
    "nifty_distance_ema50_atr",
    "breadth50",
    "screen_piotroski",
    "screen_magic",
    "screen_coffee",
    "recent_golden_cross",
]
FEATURES_PRICE = [name for name in FEATURES_18 if not name.startswith("screen_")]
OPTIONAL_20 = FEATURES_18 + ["delivery_surprise", "sector_relative_return"]


def schema_hash(names: list[str]) -> str:
    return hashlib.sha256("|".join(names).encode()).hexdigest()


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    close = out["Close"].astype(float)
    out["EMA20"] = close.ewm(span=20, adjust=False).mean()
    out["SMA50"] = close.rolling(50).mean()
    out["SMA200"] = close.rolling(200).mean()
    prev_close = close.shift()
    true_range = pd.concat(
        [out["High"] - out["Low"], (out["High"] - prev_close).abs(), (out["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    out["ATR"] = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    out["Ret"] = np.log(close).diff()
    return out


def row_features(
    frame: pd.DataFrame,
    i: int,
    *,
    nifty: pd.DataFrame | None,
    breadth: float,
    flags: dict | None = None,
    price_only: bool = False,
) -> dict | None:
    if i < 126:
        return None
    close = float(frame["Close"].iloc[i])
    atr = float(frame["ATR"].iloc[i])
    if close <= 0 or atr <= 0 or not math.isfinite(atr):
        return None
    ema20 = float(frame["EMA20"].iloc[i])
    sma50 = float(frame["SMA50"].iloc[i])
    sma_prev = float(frame["SMA50"].iloc[i - 5])
    high63 = float(frame["High"].iloc[i - 62 : i + 1].max())
    vol_med = float(frame["Volume"].iloc[i - 20 : i].median())
    turn = float((frame["Close"].iloc[i - 59 : i + 1] * frame["Volume"].iloc[i - 59 : i + 1]).median())
    gaps = []
    for k in range(i - 62, i + 1):
        if k < 1:
            continue
        prev_atr = float(frame["ATR"].iloc[k - 1])
        if prev_atr <= 0:
            continue
        gaps.append(max(0.0, (float(frame["Close"].iloc[k - 1]) - float(frame["Open"].iloc[k])) / prev_atr))
    down = float(np.percentile(gaps, 95)) if len(gaps) >= 30 else float("nan")
    vol20 = float(frame["Ret"].iloc[i - 19 : i + 1].std())
    vol63 = float(frame["Ret"].iloc[i - 62 : i + 1].std())
    beta = float("nan")
    rel63 = float("nan")
    rel126 = float("nan")
    nifty_dist = float("nan")
    if nifty is not None and len(nifty) > i:
        stock = frame["Ret"].iloc[i - 59 : i + 1]
        index = nifty["Ret"].iloc[i - 59 : i + 1] if "Ret" in nifty.columns else np.log(nifty["Close"].astype(float)).diff().iloc[i - 59 : i + 1]
        if float(np.nanvar(index.to_numpy())) > 0 and stock.notna().all() and index.notna().all():
            beta = float(np.cov(stock.to_numpy(), index.to_numpy())[0, 1] / np.var(index.to_numpy()))
        if float(frame["Close"].iloc[i - 63]) > 0 and float(nifty["Close"].iloc[i - 63]) > 0:
            rel63 = close / float(frame["Close"].iloc[i - 63]) - float(nifty["Close"].iloc[i]) / float(nifty["Close"].iloc[i - 63])
        if float(frame["Close"].iloc[i - 126]) > 0 and float(nifty["Close"].iloc[i - 126]) > 0:
            rel126 = close / float(frame["Close"].iloc[i - 126]) - float(nifty["Close"].iloc[i]) / float(nifty["Close"].iloc[i - 126])
        ema = float(nifty["EMA50"].iloc[i]) if "EMA50" in nifty.columns else float(nifty["Close"].astype(float).ewm(span=50, adjust=False).mean().iloc[i])
        natr = float(nifty["ATR"].iloc[i]) if "ATR" in nifty.columns else atr
        if natr > 0 and math.isfinite(ema):
            nifty_dist = (float(nifty["Close"].iloc[i]) - ema) / natr
    flags = flags or {}
    cross = recent_cross(frame, i)
    raw = {
        "atr_pct": atr / close,
        "dist_ema20_atr": (close - ema20) / atr,
        "sma50_slope5_atr": (sma50 - sma_prev) / atr,
        "drawdown_63_high": close / high63 - 1.0,
        "return_20": close / float(frame["Close"].iloc[i - 20]) - 1.0,
        "rel_return_63": rel63,
        "rel_return_126": rel126,
        "beta60": beta,
        "rel_volume20": float(frame["Volume"].iloc[i]) / vol_med if vol_med else float("nan"),
        "log_turnover60": math.log1p(turn) if turn == turn else float("nan"),
        "downside_gap95_63_atr": down,
        "volatility_ratio20_63": vol20 / vol63 if vol63 else float("nan"),
        "nifty_distance_ema50_atr": nifty_dist,
        "breadth50": float(breadth),
        "screen_piotroski": 1.0 if flags.get("piotroski") else 0.0,
        "screen_magic": 1.0 if flags.get("magic") else 0.0,
        "screen_coffee": 1.0 if flags.get("coffee") else 0.0,
        "recent_golden_cross": 1.0 if cross else 0.0,
    }
    names = FEATURES_PRICE if price_only else FEATURES_18
    out = {}
    for name in names:
        value = float(raw[name])
        if not math.isfinite(value):
            return None
        out[name] = value
    return out


def recent_cross(frame: pd.DataFrame, i: int) -> bool:
    if i < 10 or "SMA50" not in frame.columns:
        return False
    window = frame.iloc[i - 9 : i + 1]
    prev = window["SMA50"].shift(1)
    crossed = (window["SMA50"] > window["SMA200"]) & (prev <= window["SMA200"])
    return bool(crossed.fillna(False).any())
