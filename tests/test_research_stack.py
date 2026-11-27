"""Research-only HMM / GBDT smoke tests. Never touches live risk."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.engine import placeholder_weekly_signal_ranker, run_walk_forward
from src.config import PortfolioLimits
from src.models.gbdt_ranker import RankerConfig, engineer_cross_sectional_features, run_walk_forward_ranker
from src.models.hmm_regime import (
    RegimeConfig,
    build_macro_features,
    compute_rolling_hmm_regimes,
    entries_blocked_by_regime,
    label_regime_from_probs,
)
from src.universe.pit_universe import UniverseConfig, attach_pit_sectors, build_quarterly_universes


def _macro_series(n: int = 650, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-02", periods=n)
    rets = np.concatenate(
        [
            rng.normal(0.0008, 0.006, n // 3),
            rng.normal(0.0000, 0.008, n // 3),
            rng.normal(-0.0010, 0.018, n - 2 * (n // 3)),
        ]
    )
    close = 10_000 * np.exp(np.cumsum(rets))
    vix = 12 + 40 * (rets.std() and np.abs(rets) / (np.abs(rets).mean() + 1e-8))
    return pd.DataFrame({"date": dates, "nifty_close": close, "india_vix": vix})


def _panel(n_days: int = 80) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    rows = []
    for day in dates:
        for index, (symbol, sector, macro, turnover) in enumerate(
            (
                ("B1", "Banks", "Financial Services", 1000),
                ("B2", "Banks", "Financial Services", 900),
                ("B3", "Banks", "Financial Services", 800),
                ("I1", "IT", "Information Technology", 100),
                ("I2", "IT", "Information Technology", 90),
            )
        ):
            close = 100 + index + (day - dates[0]).days * (0.2 if symbol.startswith("B") else 0.01)
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "open": close,
                    "close": close,
                    "traded_value_inr": turnover,
                }
            )
    ohlcv = pd.DataFrame(rows)
    sectors = pd.DataFrame(
        [
            {
                "effective_date": dates[0],
                "symbol": symbol,
                "isin": symbol,
                "sector": sector,
                "macro_sector": macro,
            }
            for symbol, sector, macro, _turnover in (
                ("B1", "Banks", "Financial Services", 0),
                ("B2", "Banks", "Financial Services", 0),
                ("B3", "Banks", "Financial Services", 0),
                ("I1", "IT", "Information Technology", 0),
                ("I2", "IT", "Information Technology", 0),
            )
        ]
    )
    return ohlcv, sectors


def test_macro_features_fall_back_to_realized_vol_without_vix():
    macro = _macro_series(80).drop(columns=["india_vix"])
    feats = build_macro_features(macro, vol_lookback_days=20)
    assert {"ret_1d", "vol_feat", "mom_20d"} <= set(feats.columns)
    assert feats["vol_feat"].notna().all()


def test_rolling_hmm_emits_semantic_labels_and_probs():
    regimes = compute_rolling_hmm_regimes(
        _macro_series(620),
        RegimeConfig(train_window_days=120, refit_every_n_days=40, n_iter=50),
    )
    assert not regimes.empty
    assert set(regimes["regime"]).issubset({"BULL", "SIDEWAYS", "BEAR_HIGH_VOL"})
    probs = regimes[["prob_bull", "prob_sideways", "prob_bear"]].sum(axis=1)
    assert ((probs > 0.99) & (probs < 1.01)).all()


def test_label_regime_from_probs_is_argmax():
    assert label_regime_from_probs(0.0, 0.0, 1.0) == "BEAR_HIGH_VOL"
    assert label_regime_from_probs(0.6, 0.3, 0.1) == "BULL"


def test_hmm_gate_blocks_bear_entries_in_backtest():
    ohlcv, sectors = _panel()
    merged = attach_pit_sectors(ohlcv, sectors)
    universes = build_quarterly_universes(
        merged,
        UniverseConfig(lookback_days=20, min_trading_days=10, pool_size_c=5, target_size=4, max_per_sector=2),
    )
    dates = pd.to_datetime(sorted(merged["date"].unique()))
    regimes = pd.DataFrame(
        {
            "date": dates,
            "regime": ["BEAR_HIGH_VOL"] * len(dates),
            "prob_bull": 0.1,
            "prob_sideways": 0.1,
            "prob_bear": 0.8,
        }
    )
    port = PortfolioLimits(hold_weeks=1, max_open_positions=4)
    blocked = run_walk_forward(
        merged,
        universes,
        "A",
        placeholder_weekly_signal_ranker,
        portfolio=port,
        regime_df=regimes,
        regime_cfg=RegimeConfig(block_bear_entries=True, max_bear_prob_for_entry=0.65),
    )
    open_run = run_walk_forward(merged, universes, "A", placeholder_weekly_signal_ranker, portfolio=port)
    assert blocked["metrics"]["Regime_Blocked_Weeks"] > 0
    assert open_run["metrics"]["Regime_Blocked_Weeks"] == 0
    assert entries_blocked_by_regime({"regime": "BEAR_HIGH_VOL", "prob_bear": 0.2})


def test_gbdt_feature_engineering_and_tiny_walk_forward():
    dates = pd.bdate_range("2022-01-03", periods=320)
    rows = []
    rng = np.random.default_rng(0)
    for day_i, day in enumerate(dates):
        for sym_i, (symbol, sector) in enumerate(
            (("AAA", "Banks"), ("BBB", "Banks"), ("CCC", "IT"), ("DDD", "IT"))
        ):
            close = 100 + sym_i * 5 + day_i * (0.05 + 0.02 * sym_i) + rng.normal(0, 0.3)
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "open": close,
                    "close": close,
                    "traded_value_inr": 1_000_000 * (1 + sym_i) + day_i,
                }
            )
    ohlcv = pd.DataFrame(rows)
    sectors = pd.DataFrame(
        [
            {"effective_date": dates[0], "symbol": s, "isin": s, "sector": sec, "macro_sector": "X"}
            for s, sec in (("AAA", "Banks"), ("BBB", "Banks"), ("CCC", "IT"), ("DDD", "IT"))
        ]
    )
    feats = engineer_cross_sectional_features(ohlcv, sectors, horizon_days=5)
    assert "cs_rank_target" in feats.columns
    assert "ret_20d_cs_z" in feats.columns

    oof = run_walk_forward_ranker(
        feats,
        RankerConfig(
            forward_horizon_days=5,
            train_min_days=180,
            step_days=40,
            embargo_days=5,
            n_estimators=20,
            min_child_samples=5,
            num_leaves=8,
        ),
    )
    assert not oof.empty
    assert {"date", "symbol", "gbdt_score", "gbdt_rank_pct"} <= set(oof.columns)
