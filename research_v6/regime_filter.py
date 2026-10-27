"""EMA50 regime and a two-session hysteresis comparator. Neither writes a live gate."""

from __future__ import annotations

import numpy as np


def ema(values: np.ndarray, span: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    alpha = 2.0 / (span + 1.0)
    started = False
    current = 0.0
    for index, value in enumerate(np.asarray(values, dtype=float)):
        if not np.isfinite(value):
            started = False
            continue
        current = value if not started else alpha * value + (1.0 - alpha) * current
        started = True
        out[index] = current
    return out


def ema50_state(close: float, average: float) -> str:
    if not np.isfinite(close) or not np.isfinite(average):
        return "DATA_INVALID"
    return "RISK_ON" if close > average else "RISK_OFF"


def hysteresis(states: list[str], confirm: int = 2) -> list[str]:
    """Switch only after `confirm` consecutive raw states that differ from the held state."""
    if confirm < 2:
        raise ValueError("confirmation must be at least two sessions")
    held = "DATA_INVALID"
    pending = None
    count = 0
    out = []
    for state in states:
        if state == "DATA_INVALID":
            held = "DATA_INVALID"
            pending = None
            count = 0
        elif state == held:
            pending = None
            count = 0
        else:
            if pending != state:
                pending = state
                count = 1
            else:
                count += 1
            if count >= confirm:
                held = state
                pending = None
                count = 0
        out.append(held)
    return out
