"""Baseline and challenger use the same aligned index. Neither is tuned after results."""

from __future__ import annotations

import math


def baseline(close: float | None, ema50: float | None) -> str:
    if close is None or ema50 is None or not math.isfinite(close) or not math.isfinite(ema50):
        return "DATA_INVALID"
    return "RISK_ON" if close > ema50 else "RISK_OFF"


def challenger(
    close: float | None,
    ema50: float | None,
    *,
    breadth: float | None,
    vol_ratio: float | None,
    coverage: float | None,
) -> str:
    base = baseline(close, ema50)
    if base == "DATA_INVALID":
        return base
    if coverage is None or coverage < 0.90 or breadth is None or vol_ratio is None:
        return "DATA_INVALID"
    if not math.isfinite(breadth) or not math.isfinite(vol_ratio):
        return "DATA_INVALID"
    if base == "RISK_ON" and breadth >= 0.50 and vol_ratio <= 1.5:
        return "RISK_ON"
    return "RISK_OFF"
