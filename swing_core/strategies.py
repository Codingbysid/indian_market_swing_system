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


def relative_strength_signal(
    df: pd.DataFrame,
    i: int,
    prev_rank: float | None,
    curr_rank: float | None,
    residual20: float | None,
) -> bool:
    """Enter the top quintile from below it. A membership that was already there is not a signal."""
    if i < 60 or prev_rank is None or curr_rank is None or residual20 is None:
        return False
    try:
        prev_rank_f, curr_rank_f, residual_f = float(prev_rank), float(curr_rank), float(residual20)
    except (TypeError, ValueError):
        return False
    close = _f(df["Close"], i)
    sma50, sma_prev = _f(df["SMA_50"], i), _f(df["SMA_50"], i - 10)
    ema21, atr = _f(df["EMA_21"], i), _f(df["ATR"], i)
    if not _finite(prev_rank_f, curr_rank_f, residual_f, close, sma50, sma_prev, ema21, atr) or atr <= 0:
        return False
    return (
        prev_rank_f < 0.80 <= curr_rank_f
        and residual_f > 0
        and close > sma50 > sma_prev
        and close <= ema21 + 3.0 * atr
    )


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
    prev_rank: float | None = None,
    curr_rank: float | None = None,
    residual20: float | None = None,
    residual_z: float | None = None,
) -> list[str]:
    found = []
    if ema_cross(df, i):
        found.append("ema_cross")
    if trend_pullback(df, i, nifty_ret20=nifty_ret20):
        found.append("trend_pullback")
    if range_breakout(df, i):
        found.append("range_breakout")
    if relative_strength_signal(df, i, prev_rank, curr_rank, residual20) or rs_enter_top_quintile:
        found.append("relative_strength_momentum")
    if failed_breakdown(df, i):
        found.append("failed_breakdown_reclaim")
    if residual_shock(df, i, residual_z):
        found.append("residual_shock_reversion")
    return found


def _pullback_gates(df: pd.DataFrame, i: int, nifty_ret20: float | None) -> dict[str, bool]:
    history = i >= 20
    close = _f(df["Close"], i) if history else float("nan")
    sma50 = _f(df["SMA_50"], i) if history else float("nan")
    sma_prev = _f(df["SMA_50"], i - 10) if history else float("nan")
    ema9 = _f(df["EMA_9"], i) if history else float("nan")
    ema21 = _f(df["EMA_21"], i) if history else float("nan")
    atr = _f(df["ATR"], i) if history else float("nan")
    prev_high = _f(df["High"], i - 1) if history else float("nan")
    finite = _finite(close, sma50, sma_prev, ema9, ema21, atr, prev_high)
    touched = False
    breakdown = False
    if history:
        for k in range(1, 6):
            low_k, ema_k, atr_k = _f(df["Low"], i - k), _f(df["EMA_21"], i - k), _f(df["ATR"], i - k)
            sma_k, cls_k = _f(df["SMA_50"], i - k), _f(df["Close"], i - k)
            if not _finite(low_k, ema_k, atr_k, sma_k, cls_k):
                finite = False
                break
            if cls_k < sma_k:
                breakdown = True
            if ema_k - 0.75 * atr_k <= low_k <= ema_k + 0.25 * atr_k:
                touched = True
    stock_r = close / _f(df["Close"], i - 20) - 1.0 if history and _f(df["Close"], i - 20) > 0 else float("nan")
    return {
        "history": history and finite,
        "rising_sma50": bool(finite and close > sma50 > sma_prev),
        "ema_stacked": bool(finite and ema9 > ema21),
        "controlled_touch": touched and not breakdown,
        "reclaim": bool(finite and close > prev_high and close > ema9),
        "not_extended": bool(finite and atr > 0 and close <= ema21 + 1.5 * atr),
        "relative_strength": bool(finite and nifty_ret20 is not None and stock_r - float(nifty_ret20) > 0),
    }


def condition_funnel(df: pd.DataFrame, i: int, ctx: dict | None = None) -> dict[str, dict[str, bool]]:
    """Every gate, including the ones that failed. Used for zero-hit diagnosis."""
    ctx = ctx or {}
    close = _f(df["Close"], i)
    atr = _f(df["ATR"], i)
    ema21 = _f(df["EMA_21"], i)
    sma50 = _f(df["SMA_50"], i)
    sma_prev = _f(df["SMA_50"], i - 10) if i >= 10 else float("nan")
    cross_history = i >= 1
    prev9, prev21 = (_f(df["EMA_9"], i - 1), _f(df["EMA_21"], i - 1)) if cross_history else (float("nan"), float("nan"))
    ema9, rsi = _f(df["EMA_9"], i), _f(df["RSI"], i)
    bb = bb_position(close, _f(df["Lower_Band"], i), _f(df["Upper_Band"], i))
    cross_finite = _finite(prev9, prev21, ema9, ema21, rsi, bb, close)
    breakout_history = i >= 21
    vol = _f(df["Volume"], i)
    prior_high = float(df["High"].iloc[i - 20 : i].max()) if breakout_history else float("nan")
    vol_med = float(df["Volume"].iloc[i - 20 : i].median()) if breakout_history else float("nan")
    prev_rank, curr_rank, residual20 = ctx.get("prev_rank"), ctx.get("curr_rank"), ctx.get("residual20")
    rs_history = i >= 60
    rs_ranks = False
    try:
        rs_ranks = prev_rank is not None and curr_rank is not None and _finite(float(prev_rank), float(curr_rank))
    except (TypeError, ValueError):
        rs_ranks = False
    z = ctx.get("residual_z")
    z_ok = z is not None and _finite(float(z)) if z is not None else False
    sma200 = _f(df["SMA_200"], i) if "SMA_200" in df.columns else float("nan")
    support = float(df["Low"].iloc[i - 10 : i].min()) if i >= 12 else float("nan")
    atr_prev = _f(df["ATR"], i - 1) if i >= 1 else float("nan")
    low, prev_close = _f(df["Low"], i), _f(df["Close"], i - 1) if i >= 1 else float("nan")
    vol_med_fb = float(df["Volume"].iloc[i - 20 : i].median()) if i >= 20 else float("nan")
    tr = _f(df["High"], i) - low if _finite(_f(df["High"], i), low) else float("nan")
    return {
        "ema_cross": {
            "history": cross_history and cross_finite,
            "fresh_cross": bool(cross_finite and prev9 <= prev21 and ema9 > ema21),
            "rsi_band": bool(cross_finite and 40 <= rsi <= 65),
            "bb_below_60": bool(cross_finite and bb < 0.60),
        },
        "trend_pullback": _pullback_gates(df, i, ctx.get("nifty_ret20")),
        "range_breakout": {
            "history": breakout_history and _finite(close, sma50, sma_prev, ema21, atr, vol, prior_high, vol_med),
            "prior_high_break": bool(breakout_history and close > prior_high),
            "rising_sma50": bool(_finite(close, sma50, sma_prev) and close > sma50 > sma_prev),
            "volume": bool(vol_med > 0 and vol >= 1.3 * vol_med),
            "not_extended": bool(_finite(close, ema21, atr) and atr > 0 and close <= ema21 + 3.0 * atr),
        },
        "relative_strength_momentum": {
            "history": rs_history and _finite(close, sma50, sma_prev, ema21, atr),
            "ranks": rs_ranks and ctx.get("residual20") is not None,
            "entered_top_quintile": bool(rs_ranks and float(prev_rank) < 0.80 <= float(curr_rank)),
            "positive_residual": bool(residual20 is not None and _finite(float(residual20)) and float(residual20) > 0),
            "rising_sma50": bool(_finite(close, sma50, sma_prev) and close > sma50 > sma_prev),
            "not_extended": bool(_finite(close, ema21, atr) and atr > 0 and close <= ema21 + 3.0 * atr),
        },
        "failed_breakdown_reclaim": {
            "history": i >= 20 and _finite(support, atr_prev, low, close, prev_close, sma50, sma_prev, vol, vol_med_fb, tr),
            "undercut": bool(_finite(support, atr_prev, low) and atr_prev > 0 and support - 0.5 * atr_prev <= low < support),
            "reclaim": bool(_finite(close, support, prev_close) and close > support and close > prev_close),
            "rising_sma50": bool(_finite(close, sma50, sma_prev) and close > sma50 > sma_prev),
            "volume": bool(vol_med_fb > 0 and vol >= vol_med_fb),
            "range_limit": bool(_finite(tr, atr_prev) and atr_prev > 0 and tr <= 2.0 * atr_prev),
        },
        "residual_shock_reversion": {
            "history": i >= 200 and _finite(close, _f(df["High"], i - 1), sma200),
            "z_finite": z_ok,
            "z_below_minus_2": bool(z_ok and float(z) < -2),
            "reclaim_prior_high": bool(_finite(close, _f(df["High"], i - 1)) and close > _f(df["High"], i - 1)),
            "above_sma200": bool(_finite(close, sma200) and close > sma200),
        },
    }


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
