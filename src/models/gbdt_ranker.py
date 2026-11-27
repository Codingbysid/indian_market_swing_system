"""Walk-forward LightGBM cross-sectional ranker for the ~150-stock universe.

Research-only. Features are causal and cross-sectionally standardized per date.
Targets are forward excess returns with purge/embargo gaps so out-of-fold
scores can feed the weekly backtest and the RL sizing env without lookahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import lightgbm as lgb
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RankerConfig:
    forward_horizon_days: int = 20
    train_min_days: int = 756  # ~3 years
    step_days: int = 63  # quarterly walk-forward step
    embargo_days: int = 20
    objective: str = "regression"  # "regression" or "lambdarank"
    num_leaves: int = 31
    learning_rate: float = 0.03
    n_estimators: int = 400
    subsample_freq: int = 1
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_samples: int = 50
    random_state: int = 42


FEATURE_COLS: list[str] = [
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "mom_6m_skip1m",
    "mom_12m_skip1m",
    "price_to_ema20",
    "price_to_ema50",
    "ema20_to_ema50",
    "dist_52w_high",
    "vol_20d",
    "vol_60d",
    "vol_adj_mom_126d",
    "atr_pct_14d",
    "turnover_ratio_5d_126d",
    "amihud_20d",
    "rel_ret_20d_sector",
    "ret_20d_cs_z",
    "mom_12m_skip1m_cs_z",
    "vol_20d_cs_z",
    "turnover_ratio_5d_126d_cs_z",
    "prob_bull",
    "prob_sideways",
    "prob_bear",
    # V3 regime-resilient features (orthogonal quality / low-beta momentum)
    "rs_nifty_60d",
    "downside_vol_60d",
    "trend_consistency_60d",
    "sector",
]


def engineer_cross_sectional_features(
    ohlcv_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    regime_df: pd.DataFrame | None = None,
    sector_col: str = "sector",
    horizon_days: int = 20,
    macro_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Causal stock features, CS z-scores, and forward excess-return targets."""
    df = ohlcv_df.sort_values(["symbol", "date"]).copy()
    df["date"] = pd.to_datetime(df["date"])

    sec = sector_df.sort_values("effective_date").copy()
    sec["effective_date"] = pd.to_datetime(sec["effective_date"])
    df = pd.merge_asof(
        df.sort_values("date"),
        sec,
        left_on="date",
        right_on="effective_date",
        by="symbol",
        direction="backward",
    )
    df["sector"] = df[sector_col].fillna("UNKNOWN").astype("category")

    g = df.groupby("symbol", group_keys=False)
    log_close = np.log(df["close"].astype(float))

    df["ret_1d"] = log_close.groupby(df["symbol"]).diff(1)
    df["ret_5d"] = log_close.groupby(df["symbol"]).diff(5)
    df["ret_10d"] = log_close.groupby(df["symbol"]).diff(10)
    df["ret_20d"] = log_close.groupby(df["symbol"]).diff(20)

    c_lag21 = g["close"].shift(21)
    c_lag126 = g["close"].shift(126)
    c_lag252 = g["close"].shift(252)
    df["mom_6m_skip1m"] = np.log(c_lag21 / c_lag126)
    df["mom_12m_skip1m"] = np.log(c_lag21 / c_lag252)

    ema20 = g["close"].transform(lambda s: s.ewm(span=20, adjust=False).mean())
    ema50 = g["close"].transform(lambda s: s.ewm(span=50, adjust=False).mean())
    df["price_to_ema20"] = df["close"] / ema20 - 1.0
    df["price_to_ema50"] = df["close"] / ema50 - 1.0
    df["ema20_to_ema50"] = ema20 / ema50 - 1.0

    roll_max_252 = g["close"].transform(lambda s: s.rolling(252, min_periods=126).max())
    df["dist_52w_high"] = df["close"] / roll_max_252 - 1.0

    df["vol_20d"] = g["ret_1d"].transform(lambda s: s.rolling(20).std()) * np.sqrt(252)
    df["vol_60d"] = g["ret_1d"].transform(lambda s: s.rolling(60).std()) * np.sqrt(252)
    mom_126 = np.log(df["close"] / g["close"].shift(126))
    vol_126 = g["ret_1d"].transform(lambda s: s.rolling(126).std()) * np.sqrt(252)
    df["vol_adj_mom_126d"] = mom_126 / vol_126.replace(0, np.nan)

    prev_close = g["close"].shift(1)
    tr_proxy = pd.concat(
        [
            (df["open"] - df["close"]).abs(),
            (df["open"] - prev_close).abs(),
            (df["close"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr_pct_14d"] = (
        tr_proxy.groupby(df["symbol"]).transform(lambda s: s.rolling(14).mean()) / df["close"]
    )

    # --- V3: regime-resilient quality / relative-strength features ---
    stock_log_60 = log_close.groupby(df["symbol"]).diff(60)
    if macro_df is not None and not macro_df.empty and "nifty_close" in macro_df.columns:
        macro = macro_df[["date", "nifty_close"]].copy()
        macro["date"] = pd.to_datetime(macro["date"])
        macro = macro.drop_duplicates("date").sort_values("date")
        macro["nifty_log_60"] = np.log(macro["nifty_close"]).diff(60)
        df = df.merge(macro[["date", "nifty_log_60"]], on="date", how="left")
        df["rs_nifty_60d"] = stock_log_60 - df["nifty_log_60"]
        df = df.drop(columns=["nifty_log_60"])
    else:
        # Fall back to cross-sectional demeaning when Nifty series is absent
        df["rs_nifty_60d"] = stock_log_60 - stock_log_60.groupby(df["date"]).transform("median")

    neg_ret = df["ret_1d"].clip(upper=0.0)
    df["downside_vol_60d"] = (
        neg_ret.groupby(df["symbol"]).transform(lambda s: s.rolling(60, min_periods=30).std())
        * np.sqrt(252)
    )
    pos_5d = (df["ret_5d"] > 0).astype(float)
    df["trend_consistency_60d"] = pos_5d.groupby(df["symbol"]).transform(
        lambda s: s.rolling(12, min_periods=6).mean()  # ~60 trading days / 5
    )

    med_val_5 = g["traded_value_inr"].transform(lambda s: s.rolling(5).median())
    med_val_126 = g["traded_value_inr"].transform(
        lambda s: s.rolling(126, min_periods=60).median()
    )
    df["turnover_ratio_5d_126d"] = med_val_5 / med_val_126.replace(0, np.nan)
    df["amihud_20d"] = (
        (df["ret_1d"].abs() / np.log1p(df["traded_value_inr"].clip(lower=0)))
        .groupby(df["symbol"])
        .transform(lambda s: s.rolling(20).median())
    )

    sec_med_20d = df.groupby(["date", "sector"], observed=False)["ret_20d"].transform("median")
    df["rel_ret_20d_sector"] = df["ret_20d"] - sec_med_20d

    for col in ["ret_20d", "mom_12m_skip1m", "vol_20d", "turnover_ratio_5d_126d"]:
        dt_grp = df.groupby("date")[col]
        mean_d = dt_grp.transform("mean")
        std_d = dt_grp.transform("std").replace(0, np.nan)
        df[f"{col}_cs_z"] = (df[col] - mean_d) / std_d

    if regime_df is not None and not regime_df.empty:
        reg = regime_df.copy()
        reg["date"] = pd.to_datetime(reg["date"])
        df = df.merge(
            reg[["date", "prob_bull", "prob_sideways", "prob_bear"]],
            on="date",
            how="left",
        )
    for r_col, default_v in [("prob_bull", 0.34), ("prob_sideways", 0.33), ("prob_bear", 0.33)]:
        if r_col not in df.columns:
            df[r_col] = default_v
        else:
            df[r_col] = df[r_col].fillna(default_v)

    fwd_ret = g["close"].transform(lambda s: np.log(s.shift(-horizon_days) / s))
    bench_fwd = fwd_ret.groupby(df["date"]).transform("median")
    df["fwd_excess_ret"] = fwd_ret - bench_fwd
    df["cs_rank_target"] = df.groupby("date")["fwd_excess_ret"].rank(pct=True) - 0.5

    return df.sort_values(["date", "symbol"]).reset_index(drop=True)


def _model_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame[FEATURE_COLS].copy()
    if "sector" in x.columns:
        x["sector"] = x["sector"].astype("category")
    return x


def build_daily_top_n_mask(
    ohlcv_df: pd.DataFrame,
    *,
    top_n: int = 300,
    lookback_days: int = 126,
    min_periods: int = 60,
) -> pd.DataFrame:
    """Point-in-time Top-N liquid names by rolling median traded_value (zero lookahead).

    Returns columns: date, symbol, med_adtv, in_top_n (bool).
    """
    df = ohlcv_df[["date", "symbol", "traded_value_inr"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"])
    df["med_adtv"] = (
        df.groupby("symbol", group_keys=False)["traded_value_inr"]
        .transform(lambda s: s.rolling(lookback_days, min_periods=min_periods).median())
    )
    df["rank_liq"] = df.groupby("date")["med_adtv"].rank(ascending=False, method="first")
    df["in_top_n"] = df["rank_liq"] <= float(top_n)
    return df[["date", "symbol", "med_adtv", "in_top_n"]]


def restrict_features_to_liquidity_pool(
    feat_df: pd.DataFrame,
    pool_mask: pd.DataFrame,
    *,
    cs_cols: list[str] | None = None,
) -> pd.DataFrame:
    """Keep only Top-N pool rows and recompute cross-sectional z-scores + rank target inside the pool."""
    mask = pool_mask.copy()
    mask["date"] = pd.to_datetime(mask["date"])
    frame = feat_df.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    merged = frame.merge(mask[["date", "symbol", "in_top_n"]], on=["date", "symbol"], how="inner")
    merged = merged[merged["in_top_n"]].copy()
    cs_cols = cs_cols or ["ret_20d", "mom_12m_skip1m", "vol_20d", "turnover_ratio_5d_126d"]
    for col in cs_cols:
        if col not in merged.columns:
            continue
        dt_grp = merged.groupby("date")[col]
        mean_d = dt_grp.transform("mean")
        std_d = dt_grp.transform("std").replace(0, np.nan)
        merged[f"{col}_cs_z"] = (merged[col] - mean_d) / std_d
    if "fwd_excess_ret" in merged.columns:
        # Re-center excess vs pool median, then rank within pool
        pool_med = merged.groupby("date")["fwd_excess_ret"].transform("median")
        # fwd_excess_ret was vs all-stock median; rebuild from close if available would be ideal,
        # but re-ranking the existing excess within the pool is the matched-universe target.
        merged["cs_rank_target"] = merged.groupby("date")["fwd_excess_ret"].rank(pct=True) - 0.5
        del pool_med
    return merged.drop(columns=["in_top_n"]).sort_values(["date", "symbol"]).reset_index(drop=True)


def run_walk_forward_ranker(
    feat_df: pd.DataFrame,
    cfg: RankerConfig = RankerConfig(),
    *,
    return_importances: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Train LightGBM with embargoed walk-forward folds; return OOF scores.

    When ``return_importances`` is True, also return a DataFrame of mean
    gain importances across folds (top features for diagnostics).
    """
    unique_dates = np.sort(feat_df["date"].unique())
    oof_chunks: list[pd.DataFrame] = []
    importance_rows: list[dict] = []
    start_idx = cfg.train_min_days + cfg.embargo_days
    # Only require columns that exist (allows older feature matrices)
    feat_cols = [c for c in FEATURE_COLS if c in feat_df.columns]

    for test_start_idx in range(start_idx, len(unique_dates), cfg.step_days):
        test_end_idx = min(test_start_idx + cfg.step_days, len(unique_dates))
        train_cutoff_idx = test_start_idx - cfg.embargo_days
        if train_cutoff_idx <= 0:
            continue

        train_dates = set(unique_dates[:train_cutoff_idx])
        test_dates = set(unique_dates[test_start_idx:test_end_idx])

        train_df = feat_df[feat_df["date"].isin(train_dates)].dropna(
            subset=feat_cols + ["fwd_excess_ret", "cs_rank_target"]
        )
        test_df = feat_df[feat_df["date"].isin(test_dates)].dropna(subset=feat_cols)
        if train_df.empty or test_df.empty:
            continue

        x_train = train_df[feat_cols].copy()
        x_test = test_df[feat_cols].copy()
        if "sector" in x_train.columns:
            x_train["sector"] = x_train["sector"].astype("category")
            x_test["sector"] = x_test["sector"].astype("category")
        cat_features = ["sector"] if "sector" in x_train.columns else "auto"

        if cfg.objective == "lambdarank":
            rel_bins = (
                train_df.groupby("date")["fwd_excess_ret"]
                .rank(pct=True)
                .mul(5)
                .clip(0, 4.999)
                .astype(int)
            )
            groups = train_df.groupby("date", sort=False).size().tolist()
            model = lgb.LGBMRanker(
                objective="lambdarank",
                num_leaves=cfg.num_leaves,
                learning_rate=cfg.learning_rate,
                n_estimators=cfg.n_estimators,
                subsample=cfg.subsample,
                subsample_freq=cfg.subsample_freq,
                colsample_bytree=cfg.colsample_bytree,
                min_child_samples=cfg.min_child_samples,
                random_state=cfg.random_state + test_start_idx,
                verbose=-1,
            )
            model.fit(x_train, rel_bins, group=groups, categorical_feature=cat_features)
        else:
            model = lgb.LGBMRegressor(
                objective="regression",
                num_leaves=cfg.num_leaves,
                learning_rate=cfg.learning_rate,
                n_estimators=cfg.n_estimators,
                subsample=cfg.subsample,
                subsample_freq=cfg.subsample_freq,
                colsample_bytree=cfg.colsample_bytree,
                min_child_samples=cfg.min_child_samples,
                random_state=cfg.random_state + test_start_idx,
                verbose=-1,
            )
            model.fit(x_train, train_df["cs_rank_target"], categorical_feature=cat_features)

        scores = model.predict(x_test)
        out = test_df[["date", "symbol"]].copy()
        out["gbdt_score"] = scores
        out["gbdt_rank_pct"] = out.groupby("date")["gbdt_score"].rank(pct=True)
        if "fwd_excess_ret" in test_df.columns:
            out["fwd_excess_ret"] = test_df["fwd_excess_ret"].to_numpy()
        oof_chunks.append(out)

        gain = model.booster_.feature_importance(importance_type="gain")
        for name, val in zip(feat_cols, gain):
            importance_rows.append({"feature": name, "gain": float(val), "fold_start": str(unique_dates[test_start_idx])[:10]})

    if not oof_chunks:
        empty = pd.DataFrame(columns=["date", "symbol", "gbdt_score", "gbdt_rank_pct"])
        return (empty, pd.DataFrame()) if return_importances else empty

    oof = (
        pd.concat(oof_chunks, ignore_index=True)
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )
    if not return_importances:
        return oof
    imp = (
        pd.DataFrame(importance_rows)
        .groupby("feature", as_index=False)["gain"]
        .mean()
        .sort_values("gain", ascending=False)
        .reset_index(drop=True)
    )
    return oof, imp


def make_oof_signal_fn(
    oof_df: pd.DataFrame,
) -> Callable[[pd.Timestamp, pd.DataFrame, list[str]], pd.DataFrame]:
    """Build a weekly signal_fn that ranks the watchlist by OOF GBDT score."""
    scores = oof_df.copy()
    scores["date"] = pd.to_datetime(scores["date"])

    def signal_fn(
        asof_date: pd.Timestamp,
        df_history: pd.DataFrame,
        watchlist: list[str],
    ) -> pd.DataFrame:
        del df_history  # ranking comes from leak-free OOF scores only
        eligible = scores[(scores["date"] <= asof_date) & (scores["symbol"].isin(watchlist))]
        if eligible.empty:
            return pd.DataFrame(columns=["symbol", "score"])
        latest = eligible.sort_values("date").groupby("symbol", as_index=False).tail(1)
        ranked = latest.sort_values(["gbdt_score", "symbol"], ascending=[False, True])
        return ranked.rename(columns={"gbdt_score": "score"})[["symbol", "score"]].reset_index(drop=True)

    signal_fn.__name__ = "oof_gbdt_signal_ranker"
    return signal_fn
