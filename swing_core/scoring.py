"""Fail-closed probability helpers. None/NaN must never admit a BUY."""

from __future__ import annotations

import math


def finite_probability(value) -> float | None:
    if value is None:
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p < 0.0 or p > 1.0:
        return None
    return p


def admit_buy(p, min_prob: float) -> tuple[bool, str]:
    scored = finite_probability(p)
    if scored is None:
        return False, "fail_closed_invalid_probability"
    if scored < float(min_prob):
        return False, f"p={scored:.3f}<min={float(min_prob):.2f}"
    return True, "ok"
