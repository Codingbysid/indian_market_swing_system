"""Date-aligned residual shock and cross-sectional rank helpers.

Beta for a three-session window is estimated on the sixty daily returns that
end before that window. The z-score uses the prior sixty three-session
residuals and excludes the shock being tested. Zero variance is invalid.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def residual_z_series(stock_close: np.ndarray, nifty_close: np.ndarray) -> np.ndarray:
    s = np.asarray(stock_close, dtype=float)
    n = np.asarray(nifty_close, dtype=float)
    out = np.full(len(s), np.nan)
    if len(s) != len(n) or len(s) < 130:
        return out
    sr = np.full(len(s), np.nan)
    nr = np.full(len(s), np.nan)
    sr[1:] = s[1:] / s[:-1] - 1.0
    nr[1:] = n[1:] / n[:-1] - 1.0
    r3s = np.full(len(s), np.nan)
    r3n = np.full(len(s), np.nan)
    r3s[3:] = s[3:] / s[:-3] - 1.0
    r3n[3:] = n[3:] / n[:-3] - 1.0
    ps = pd.Series(sr)
    pn = pd.Series(nr)
    var = pn.rolling(60, min_periods=60).var()
    cov = ps.rolling(60, min_periods=60).cov(pn)
    beta = (cov / var).to_numpy()
    beta_lag = np.full(len(s), np.nan)
    beta_lag[3:] = beta[:-3]
    resid = r3s - beta_lag * r3n
    rs = pd.Series(resid)
    prev = rs.shift(1)
    mu = prev.rolling(60, min_periods=60).mean()
    sd = prev.rolling(60, min_periods=60).std(ddof=1)
    z = (rs - mu) / sd
    bad = ~np.isfinite(sd.to_numpy()) | (sd.to_numpy() <= 1e-12)
    z = z.to_numpy().copy()
    z[bad] = np.nan
    return z


def equal_weight_percentile(panel_a: pd.DataFrame, panel_b: pd.DataFrame, min_names: int = 20) -> pd.DataFrame:
    """Average of two cross-sectional percentile ranks. Dates are the index."""
    both = panel_a.notna() & panel_b.notna()
    enough = both.sum(axis=1) >= min_names
    p_a = panel_a.where(both).rank(axis=1, pct=True, method="average")
    p_b = panel_b.where(both).rank(axis=1, pct=True, method="average")
    score = 0.5 * (p_a + p_b)
    return score.where(enough)
