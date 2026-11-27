"""Hard gate test: assert strict causality and zero lookahead in V3 feature engineering."""

import numpy as np
import pandas as pd
import pytest

from src.models.gbdt_ranker import FEATURE_COLS, engineer_cross_sectional_features


def test_v3_features_strict_causality_zero_lookahead():
    """Assert that modifying prices strictly after date t produces ZERO change to any V3 feature on date <= t."""
    dates = pd.date_range("2020-01-01", periods=320, freq="B")
    rows = []
    np.random.seed(42)
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]

    for sym in symbols:
        price = 500.0 + np.random.uniform(10, 100)
        for d in dates:
            price *= 1 + np.random.normal(0, 0.02)
            rows.append(
                {
                    "date": d,
                    "symbol": sym,
                    "open": price * 0.99,
                    "high": price * 1.015,
                    "low": price * 0.985,
                    "close": price,
                    "traded_value_inr": np.random.uniform(5e7, 5e8),
                }
            )

    df_clean = pd.DataFrame(rows)
    sector_df = pd.DataFrame(
        [
            {"symbol": s, "effective_date": "2020-01-01", "sector": "SEC_" + s}
            for s in symbols
        ]
    )
    macro_clean = pd.DataFrame(
        {
            "date": dates,
            "nifty_close": np.cumprod(1 + np.random.normal(0, 0.01, len(dates))) * 15000,
        }
    )

    # 1. Compute baseline features
    feat_baseline = engineer_cross_sectional_features(
        df_clean, sector_df, macro_df=macro_clean
    )

    # 2. Modify prices strictly after cutoff date (index 280, leaving >252 days before)
    cutoff_date = dates[280]
    df_modified = df_clean.copy()
    mask_future = df_modified["date"] > cutoff_date
    df_modified.loc[mask_future, "close"] = df_modified.loc[mask_future, "close"] * 2.5
    df_modified.loc[mask_future, "open"] = df_modified.loc[mask_future, "open"] * 2.5
    df_modified.loc[mask_future, "high"] = df_modified.loc[mask_future, "high"] * 2.5
    df_modified.loc[mask_future, "low"] = df_modified.loc[mask_future, "low"] * 2.5
    df_modified.loc[mask_future, "traded_value_inr"] = (
        df_modified.loc[mask_future, "traded_value_inr"] * 10.0
    )

    macro_modified = macro_clean.copy()
    mask_m_future = macro_modified["date"] > cutoff_date
    macro_modified.loc[mask_m_future, "nifty_close"] = (
        macro_modified.loc[mask_m_future, "nifty_close"] * 0.5
    )

    feat_modified = engineer_cross_sectional_features(
        df_modified, sector_df, macro_df=macro_modified
    )

    # 3. Filter both to <= cutoff_date and sort identically
    f1_hist = (
        feat_baseline[feat_baseline["date"] <= cutoff_date]
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )
    f2_hist = (
        feat_modified[feat_modified["date"] <= cutoff_date]
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )

    # Assert identical NaN pattern and zero numeric difference for all features
    numeric_features = [c for c in FEATURE_COLS if c != "sector"]
    for col in numeric_features:
        s1 = f1_hist[col].values
        s2 = f2_hist[col].values
        assert np.array_equal(pd.isna(s1), pd.isna(s2)), f"NaN pattern mismatch in {col}"
        valid = ~pd.isna(s1)
        if np.any(valid):
            diff = np.max(np.abs(s1[valid] - s2[valid]))
            assert diff == 0.0, f"Leakage detected in feature {col}: max diff = {diff}"
