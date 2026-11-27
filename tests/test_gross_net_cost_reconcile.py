"""Gross vs Net CAGR must reconcile with one-way turnover × RT cost drag."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.engine import run_walk_forward
from src.config import IndiaDeliveryCostConfig, PortfolioLimits
from src.universe.pit_universe import UniverseConfig, build_quarterly_universes


def _toy_panel(n_days: int = 400, n_symbols: int = 40) -> tuple[pd.DataFrame, dict]:
    dates = pd.bdate_range("2019-01-02", periods=n_days)
    rng = np.random.default_rng(42)
    rows = []
    for i, sym in enumerate([f"S{i:02d}" for i in range(n_symbols)]):
        px = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, size=n_days)))
        sector = f"SEC{i % 8}"
        for d, p in zip(dates, px):
            rows.append(
                {
                    "date": d,
                    "symbol": sym,
                    "isin": f"INE{i:04d}",
                    "open": p,
                    "close": p,
                    "traded_value_inr": float(1e7 + 1e5 * i),
                    "sector": sector,
                    "macro_sector": sector,
                    "sector_label": sector,
                }
            )
    df = pd.DataFrame(rows)
    # Fake PIT sector columns expected by universe builder
    univ_df = df.rename(columns={"sector": "sector"}).copy()
    # attach sector_label for eligible()
    univ = build_quarterly_universes(
        univ_df,
        UniverseConfig(
            lookback_days=60,
            min_trading_days=40,
            pool_size_c=30,
            target_size=20,
            max_per_sector=5,
            sector_col="sector",
        ),
    )
    return df, univ


def test_gross_net_gap_reconciles_with_cost_drag():
    df, univ = _toy_panel()
    assert univ, "toy universe empty"
    port = PortfolioLimits(
        exit_mode="rank_buffer",
        rank_buffer_top_n=12,
        max_hold_weeks=8,
        rank_exit_confirm_weeks=2,
        max_new_entries_per_week=1,
        max_open_positions=5,
        initial_capital=1_000_000.0,
    )
    costs = IndiaDeliveryCostConfig()
    result = run_walk_forward(df, univ, "A+", portfolio=port, cost_cfg=costs)
    m = result["metrics"]
    assert m["N_Trades"] > 10
    gross = m["Gross_CAGR_%"]
    net = m["Net_CAGR_%"]
    drag_bps = m["Ann_Cost_Drag_BPS_yr"]
    # CAGR gap in bps/yr (approximate; compounding makes it not exact)
    gap_bps = (gross - net) * 100.0
    # Allow generous tolerance for compounding / path effects, but must be same order
    assert drag_bps > 0
    assert gap_bps > 0
    # Previously broken math had gap ~74 vs drag ~582 (ratio ~8x). Require ratio within 2.5x.
    ratio = max(gap_bps, 1e-6) / max(drag_bps, 1e-6)
    assert 0.25 <= ratio <= 2.5, f"gap_bps={gap_bps:.1f} drag={drag_bps:.1f} ratio={ratio:.2f}"
    # One-way turnover should be well below the old two-way ~1800% on this toy
    assert m["Ann_Turnover_%"] < 1500


def test_stress_rt_is_42_3():
    cfg = IndiaDeliveryCostConfig()
    from src.backtest.costs import resolve_rt_cost

    assert abs(resolve_rt_cost(cfg).total_bps - 32.3) < 1e-9
    assert abs(resolve_rt_cost(cfg, stress_slippage=True).total_bps - 42.3) < 1e-9
