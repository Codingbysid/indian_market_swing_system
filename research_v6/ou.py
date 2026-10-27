"""Discrete OU screen. Invalid phi is rejected. This does not prove cointegration."""

from __future__ import annotations

import math

import numpy as np

HALF_LIFE_MIN = 5.0
HALF_LIFE_MAX = 60.0
DF_CRITICAL = -2.86
ENTRY_Z = -1.5


def fit_ou(level: np.ndarray) -> dict:
    series = np.asarray(level, dtype=float)
    if len(series) < 120 or not np.isfinite(series).all():
        return {"valid": False, "reason": "insufficient_or_nonfinite"}
    lag = series[:-1]
    change = np.diff(series)
    design = np.column_stack([np.ones(len(lag)), lag])
    coef, _, _, _ = np.linalg.lstsq(design, change, rcond=None)
    residual = change - design @ coef
    degrees = len(change) - 2
    variance = float(residual @ residual) / degrees
    xtx_inv = np.linalg.inv(design.T @ design)
    slope_se = math.sqrt(variance * float(xtx_inv[1, 1]))
    slope = float(coef[1])
    intercept = float(coef[0])
    t_stat = slope / slope_se if slope_se > 0 else float("nan")
    phi = 1.0 + slope
    if not (0.0 < phi < 1.0) or not math.isfinite(t_stat):
        return {"valid": False, "reason": "phi_outside_unit_interval", "phi": phi, "t_stat": t_stat}
    if t_stat > DF_CRITICAL:
        return {"valid": False, "reason": "stationarity_screen_failed", "phi": phi, "t_stat": t_stat}
    theta = -math.log(phi)
    half_life = math.log(2.0) / theta
    if not (HALF_LIFE_MIN <= half_life <= HALF_LIFE_MAX):
        return {"valid": False, "reason": "half_life_outside_predeclared_band", "phi": phi, "half_life": half_life}
    if abs(slope) < 1e-12:
        return {"valid": False, "reason": "slope_degenerate"}
    equilibrium = -intercept / slope
    equilibrium_variance = variance / (1.0 - phi ** 2)
    if equilibrium_variance <= 0 or not math.isfinite(equilibrium_variance):
        return {"valid": False, "reason": "variance_invalid"}
    return {
        "valid": True,
        "phi": phi,
        "half_life": half_life,
        "mean": equilibrium,
        "equilibrium_variance": equilibrium_variance,
        "sigma": math.sqrt(equilibrium_variance),
        "t_stat": t_stat,
        "vertical_sessions": int(math.ceil(2.0 * half_life)),
    }


def entry_distance(level: float, fit: dict) -> float | None:
    if not fit.get("valid"):
        return None
    return (float(level) - fit["mean"]) / fit["sigma"]


def entry_allowed(level: float, fit: dict) -> bool:
    distance = entry_distance(level, fit)
    return distance is not None and distance <= ENTRY_Z
