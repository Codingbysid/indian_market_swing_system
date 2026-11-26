"""Research-only HMM / GBDT / RL sizing smoke tests. Never touches live risk."""

from __future__ import annotations

import numpy as np
import pandas as pd

from gbdt_ranker import RankerConfig, engineer_cross_sectional_features, run_walk_forward_ranker
from hmm_regime import RegimeConfig, build_macro_features, compute_rolling_hmm_regimes, entries_blocked_by_regime
from rl_sizing_env import DecisionStep, NseSwingSizingEnv, SizingEnvConfig
from universe_ab_test import (
    BacktestConfig,
    UniverseConfig,
    attach_pit_sectors,
    build_quarterly_universes,
    placeholder_weekly_signal_ranker,
    run_single_backtest,
)


def _macro_series(n: int = 650, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-02", periods=n)
    # Three rough regimes: calm up, sideways, volatile down
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
    bt = BacktestConfig(hold_weeks=1, max_open_positions=4)
    blocked = run_single_backtest(
        merged,
        universes,
        "A",
        placeholder_weekly_signal_ranker,
        bt,
        regime_df=regimes,
        regime_cfg=RegimeConfig(block_bear_entries=True, max_bear_prob_for_entry=0.65),
    )
    open_run = run_single_backtest(merged, universes, "A", placeholder_weekly_signal_ranker, bt)
    assert blocked["Regime_Blocked_Weeks"] > 0
    assert open_run["Regime_Blocked_Weeks"] == 0
    assert entries_blocked_by_regime({"regime": "BEAR_HIGH_VOL", "prob_bear": 0.2})


def test_rl_env_equal_weight_smoke_and_bear_block():
    steps = [
        DecisionStep(
            date=pd.Timestamp("2025-01-03") + pd.Timedelta(days=7 * i),
            prob_bull=0.1 if i == 0 else 0.7,
            prob_sideways=0.1,
            prob_bear=0.8 if i == 0 else 0.2,
            nifty_mom_20d=0.01,
            vix_zscore=0.0,
            candidates=[
                {
                    "symbol": f"S{j}",
                    "sector": "Banks" if j % 2 == 0 else "IT",
                    "gbdt_score": 1.0 - 0.1 * j,
                    "gbdt_rank_pct": 0.9 - 0.1 * j,
                    "mom_20d": 0.02,
                    "atr_pct": 0.02,
                    "price": 100.0 + j,
                }
                for j in range(5)
            ],
            next_prices={f"S{j}": 101.0 + j for j in range(5)},
        )
        for i in range(6)
    ]
    env = NseSwingSizingEnv(steps, SizingEnvConfig(hold_steps=2, max_new_entries_per_step=2))
    obs, _ = env.reset()
    assert obs.shape == env.observation_space.shape
    action = np.ones(env.action_space.shape, dtype=np.float32)
    obs, reward, term, trunc, info = env.step(action)
    assert info["regime_blocked"] is True
    assert info["open_positions"] == 0
    assert isinstance(reward, float)
    assert not term and not trunc


def test_gbdt_feature_engineering_and_tiny_walk_forward():
    # Longer panel so momentum / vol windows populate.
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
