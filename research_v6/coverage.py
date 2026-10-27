"""Breadth coverage with a fixed denominator of 150. Missing names stay in the denominator."""

from __future__ import annotations

import numpy as np

from research_v6.data import _rolling_mean

DENOMINATOR = 150
COVERAGE = 0.90
INVALID_BLOCK = 0.05
SUFFIX_MINIMUM = 252


def _first_valid(values: np.ndarray) -> int | None:
    finite = np.flatnonzero(np.isfinite(values))
    if len(finite) == 0:
        return None
    return int(finite[0])


def audit_coverage(study: dict) -> dict:
    """Classify each failure. Do not shrink the denominator and do not read returns for the window choice."""
    dates = study["dates"]
    symbols = list(study["panels"])
    first = {symbol: _first_valid(panel["close"]) for symbol, panel in study["panels"].items()}
    sma = {symbol: _rolling_mean(panel["close"], 50) for symbol, panel in study["panels"].items()}
    missing_names = list(study.get("missing") or [])
    reasons = {"pre_listing": 0, "warmup": 0, "missing_vendor_bar": 0, "not_in_cache": 0}
    invalid_dates = []
    valid = []
    for index, day in enumerate(dates):
        usable = 0
        for symbol in symbols:
            start = first[symbol]
            close = study["panels"][symbol]["close"][index]
            ret = study["panels"][symbol]["return"][index]
            if start is None or index < start:
                reasons["pre_listing"] += 1
            elif not np.isfinite(close):
                reasons["missing_vendor_bar"] += 1
            elif not np.isfinite(sma[symbol][index]):
                reasons["warmup"] += 1
            elif np.isfinite(ret):
                usable += 1
            else:
                reasons["missing_vendor_bar"] += 1
        reasons["not_in_cache"] += len(missing_names)
        covered = usable / DENOMINATOR
        if covered >= COVERAGE:
            valid.append(day)
        else:
            invalid_dates.append(day)
    checked = len(dates)
    invalid_fraction = len(invalid_dates) / checked if checked else 1.0
    suffix = _suffix(dates, set(valid))
    return {
        "denominator": DENOMINATOR,
        "checked_sessions": checked,
        "invalid_sessions": len(invalid_dates),
        "invalid_fraction": invalid_fraction,
        "failure_counts": reasons,
        "full_window_status": "BLOCKED" if invalid_fraction > INVALID_BLOCK else "ok",
        "full_window_reason": "breadth_coverage_below_gate" if invalid_fraction > INVALID_BLOCK else None,
        "suffix_rule": "longest_terminal_suffix_with_no_invalid_session_and_at_least_252_sessions",
        "suffix_start": None if not suffix else suffix[0],
        "suffix_end": None if not suffix else suffix[-1],
        "suffix_sessions": 0 if not suffix else len(suffix),
        "suffix_status": "ok" if suffix else "BLOCKED",
    }


def _suffix(dates: list[str], valid: set[str]) -> list[str]:
    suffix = []
    for day in reversed(dates):
        if day not in valid:
            break
        suffix.append(day)
    suffix.reverse()
    if len(suffix) < SUFFIX_MINIMUM:
        return []
    return suffix
