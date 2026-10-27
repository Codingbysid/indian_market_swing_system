"""Residual momentum from out-of-sample prediction errors.

Simple returns. The fit window ends before the ranking window, so the
intercept's in-sample zero-sum property does not apply to the score.
"""

from __future__ import annotations

import numpy as np

FIT = 120
RANK = 60
SKIP = 5
VOL_FLOOR = 1e-4


def window_bounds(index: int) -> tuple[int, int, int, int] | None:
    """Fit and rank slices. Returns None until the first legal decision index."""
    rank_end = index - SKIP
    rank_start = rank_end - RANK + 1
    fit_end = rank_start - 1
    fit_start = fit_end - FIT + 1
    if fit_start < 1 or rank_start < 1:
        return None
    return fit_start, fit_end + 1, rank_start, rank_end + 1


def _ridge(y: np.ndarray, factors: np.ndarray, lam: float) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if factors.ndim == 1:
        factors = factors.reshape(-1, 1)
    if len(y) != len(factors) or not np.isfinite(y).all() or not np.isfinite(factors).all():
        return None
    center = factors.mean(axis=0)
    scale = factors.std(axis=0)
    if np.any(scale < 1e-12):
        return None
    scaled = (factors - center) / scale
    design = np.column_stack([np.ones(len(y)), scaled])
    penalty = np.diag([0.0, *([float(lam)] * scaled.shape[1])])
    try:
        beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    except np.linalg.LinAlgError:
        return None
    if not np.isfinite(beta).all():
        return None
    return beta, center, scale


def _predict(beta: np.ndarray, center: np.ndarray, scale: np.ndarray, factors: np.ndarray) -> np.ndarray:
    if factors.ndim == 1:
        factors = factors.reshape(-1, 1)
    scaled = (factors - center) / scale
    return beta[0] + scaled @ beta[1:]


def orthogonalize(sector: np.ndarray, market: np.ndarray) -> tuple[float, float] | None:
    """sector = a + b * market + residual, fit by OLS with an intercept."""
    if len(sector) != len(market) or not np.isfinite(sector).all() or not np.isfinite(market).all():
        return None
    design = np.column_stack([np.ones(len(market)), market])
    try:
        coef, _, _, _ = np.linalg.lstsq(design, sector, rcond=None)
    except np.linalg.LinAlgError:
        return None
    if not np.isfinite(coef).all():
        return None
    return float(coef[0]), float(coef[1])


def prediction_errors(
    stock: np.ndarray,
    market: np.ndarray,
    sector: np.ndarray | None,
    index: int,
    lam: float,
) -> np.ndarray | None:
    """Ranking-window prediction errors. None means the name is excluded."""
    bounds = window_bounds(index)
    if bounds is None:
        return None
    fit_start, fit_stop, rank_start, rank_stop = bounds
    stock_fit = np.asarray(stock[fit_start:fit_stop], dtype=float)
    market_fit = np.asarray(market[fit_start:fit_stop], dtype=float)
    stock_rank = np.asarray(stock[rank_start:rank_stop], dtype=float)
    market_rank = np.asarray(market[rank_start:rank_stop], dtype=float)
    if not np.isfinite(stock_rank).all() or not np.isfinite(market_rank).all():
        return None
    if sector is None:
        fitted = _ridge(stock_fit, market_fit, lam)
        if fitted is None:
            return None
        beta, center, scale = fitted
        return stock_rank - _predict(beta, center, scale, market_rank)
    sector_fit = np.asarray(sector[fit_start:fit_stop], dtype=float)
    sector_rank = np.asarray(sector[rank_start:rank_stop], dtype=float)
    if not np.isfinite(sector_fit).all() or not np.isfinite(sector_rank).all():
        return None
    mapping = orthogonalize(sector_fit, market_fit)
    if mapping is None:
        return None
    intercept, slope = mapping
    orth_fit = sector_fit - intercept - slope * market_fit
    orth_rank = sector_rank - intercept - slope * market_rank
    fitted = _ridge(stock_fit, np.column_stack([market_fit, orth_fit]), lam)
    if fitted is None:
        return None
    beta, center, scale = fitted
    return stock_rank - _predict(beta, center, scale, np.column_stack([market_rank, orth_rank]))


def residual_score(stock: np.ndarray, market: np.ndarray, sector: np.ndarray | None, index: int, lam: float) -> float | None:
    pair = residual_pair(stock, market, sector, index, lam)
    if pair is None:
        return None
    return pair["sum"]


def residual_pair(stock: np.ndarray, market: np.ndarray, sector: np.ndarray | None, index: int, lam: float) -> dict | None:
    """Summed residual return, and that sum divided by a floored residual volatility."""
    errors = prediction_errors(stock, market, sector, index, lam)
    if errors is None or not np.isfinite(errors).all() or len(errors) < 2:
        return None
    total = float(errors.sum())
    scale = float(np.std(errors, ddof=1))
    return {"sum": total, "vol_normalized": total / max(scale, VOL_FLOOR), "volatility": scale}


def raw_momentum(stock: np.ndarray, index: int) -> float | None:
    bounds = window_bounds(index)
    if bounds is None:
        return None
    _, _, rank_start, rank_stop = bounds
    window = np.asarray(stock[rank_start:rank_stop], dtype=float)
    if not np.isfinite(window).all():
        return None
    return float(window.sum())


def top_decile(scores: dict[str, float], minimum: int = 20) -> list[str]:
    """Names at or above the 90th percentile. Ties stay in. Order is alphabetical."""
    clean = {symbol: float(value) for symbol, value in scores.items() if value is not None and np.isfinite(value)}
    if len(clean) < minimum:
        return []
    threshold = float(np.quantile(np.fromiter(clean.values(), dtype=float), 0.9))
    return sorted(symbol for symbol, value in clean.items() if value >= threshold)
