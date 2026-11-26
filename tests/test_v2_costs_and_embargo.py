"""Cost BPS precision, corporate-action invariance, and GBDT embargo integrity."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.costs import resolve_rt_cost
from src.config import IndiaDeliveryCostConfig
from src.data.corporate_actions import apply_corporate_actions
from src.data.incremental_s3 import append_daily_rows, read_partitioned_ohlcv
from src.models.gbdt_ranker import RankerConfig, run_walk_forward_ranker


def test_default_round_trip_is_about_32_3_bps():
    cfg = IndiaDeliveryCostConfig()
    rt = resolve_rt_cost(cfg)
    assert abs(rt.statutory_bps - 22.3) < 1e-9
    assert abs(rt.slippage_bps - 10.0) < 1e-9
    assert abs(rt.total_bps - 32.3) < 1e-9
    stress = resolve_rt_cost(cfg, stress_slippage=True)
    assert abs(stress.total_bps - 42.3) < 1e-9


def test_corporate_action_leaves_turnover_invariant(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=4)
    ohlcv = pd.DataFrame(
        {
            "date": list(dates),
            "symbol": ["AAA"] * 4,
            "isin": ["INEAAA"] * 4,
            "open": [100, 100, 50, 50],
            "close": [100, 100, 50, 50],
            "traded_value_inr": [10, 11, 12, 13],
        }
    )
    actions = pd.DataFrame([{"symbol": "AAA", "ex_date": dates[2], "factor": 0.5}])
    adj = apply_corporate_actions(ohlcv, actions)
    assert list(adj["traded_value_inr"]) == [10, 11, 12, 13]
    assert list(adj["close"]) == [50, 50, 50, 50]


def test_incremental_s3_idempotent_append(tmp_path):
    day = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-07-08")] * 2,
            "symbol": ["AAA", "BBB"],
            "close": [1.0, 2.0],
            "traded_value_inr": [100, 200],
        }
    )
    root = tmp_path / "ohlcv_raw_partitioned"
    append_daily_rows(day, root)
    append_daily_rows(day, root)  # idempotent
    out = read_partitioned_ohlcv(root)
    assert len(out) == 2


def test_gbdt_embargo_keeps_train_and_test_disjoint():
    # Tiny synthetic feature frame with enough dates
    dates = pd.bdate_range("2022-01-03", periods=400)
    rows = []
    for d in dates:
        for s in ("AAA", "BBB", "CCC"):
            rows.append(
                {
                    "date": d,
                    "symbol": s,
                    "sector": "Banks",
                    "ret_5d": 0.01,
                    "ret_10d": 0.02,
                    "ret_20d": 0.03,
                    "mom_6m_skip1m": 0.05,
                    "mom_12m_skip1m": 0.1,
                    "price_to_ema20": 0.0,
                    "price_to_ema50": 0.0,
                    "ema20_to_ema50": 0.0,
                    "dist_52w_high": -0.1,
                    "vol_20d": 0.2,
                    "vol_60d": 0.2,
                    "vol_adj_mom_126d": 0.5,
                    "atr_pct_14d": 0.02,
                    "turnover_ratio_5d_126d": 1.0,
                    "amihud_20d": 0.001,
                    "rel_ret_20d_sector": 0.0,
                    "ret_20d_cs_z": 0.0,
                    "mom_12m_skip1m_cs_z": 0.0,
                    "vol_20d_cs_z": 0.0,
                    "turnover_ratio_5d_126d_cs_z": 0.0,
                    "prob_bull": 0.4,
                    "prob_sideways": 0.3,
                    "prob_bear": 0.3,
                    "fwd_excess_ret": 0.01,
                    "cs_rank_target": 0.1,
                }
            )
    feats = pd.DataFrame(rows)
    feats["sector"] = feats["sector"].astype("category")
    cfg = RankerConfig(
        train_min_days=200,
        step_days=40,
        embargo_days=20,
        n_estimators=10,
        min_child_samples=5,
        num_leaves=8,
    )
    oof = run_walk_forward_ranker(feats, cfg)
    assert not oof.empty
    # First OOF date must be at least train_min + embargo into the calendar
    unique = np.sort(feats["date"].unique())
    first_oof = pd.Timestamp(oof["date"].min())
    assert first_oof >= pd.Timestamp(unique[cfg.train_min_days + cfg.embargo_days])
