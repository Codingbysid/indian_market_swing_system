#!/usr/bin/env python3
"""Run the 7-config walk-forward ablation and write Alpha/BPS reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_research_panel import load_or_build
from src.backtest.engine import (
    compute_performance_metrics,
    nifty_buy_hold_metrics,
    placeholder_weekly_signal_ranker,
    run_walk_forward,
)
from src.backtest.report import build_summary_table, metrics_to_row, write_summary
from src.config import (
    GbdtParams,
    HmmParams,
    IndiaDeliveryCostConfig,
    PortfolioLimits,
    RlParams,
    SystemConfig,
    UniverseParams,
)
from src.models.gbdt_ranker import RankerConfig, engineer_cross_sectional_features, make_oof_signal_fn, run_walk_forward_ranker
from src.models.hmm_regime import RegimeConfig, compute_rolling_hmm_regimes
from src.models.rl_sizing_env import DecisionStep, NseSwingSizingEnv, SizingEnvConfig
from src.models.train_ppo import train_ppo_sizer
from src.universe.pit_universe import UniverseConfig, attach_pit_sectors, build_quarterly_universes


def _attach_benchmark_alpha(result: dict, nifty: pd.Series, initial: float, rt) -> dict:
    metrics = dict(result["metrics"])
    eq = result["equity_curve"].set_index("date")["equity"] if not result["equity_curve"].empty else None
    if eq is None or eq.empty:
        return metrics
    enriched = compute_performance_metrics(
        eq.to_frame("equity"),
        result["trades"],
        initial_capital=initial,
        rt=rt,
        traded_notional_abs=float(result["trades"]["cost_cash"].sum() * 0) + metrics.get("Ann_Turnover_%", 0) * initial / 100,
        same_sector_weeks=0,
        two_entry_weeks=0,
        max_sector_share=[],
        cap_skips=metrics.get("Cap_Skips_Count", 0),
        regime_blocked_weeks=metrics.get("Regime_Blocked_Weeks", 0),
        nifty_curve=nifty,
    )
    # Keep operational fields from original metrics; overlay CAPM fields.
    for key in (
        "Ann_CAPM_Alpha_%",
        "Ann_CAPM_Alpha_BPS_yr",
        "Ann_Excess_vs_Nifty_BPS_yr",
    ):
        metrics[key] = enriched.get(key)
    return metrics


def _build_decision_steps(
    oof: pd.DataFrame,
    regimes: pd.DataFrame,
    ohlcv: pd.DataFrame,
    merged: pd.DataFrame,
    universes: dict,
    top_k: int = 5,
) -> list[DecisionStep]:
    dates = sorted(pd.to_datetime(oof["date"].unique()))
    weekly = pd.Series(dates).groupby(pd.Series(dates).dt.to_period("W")).max()
    prices = ohlcv.drop_duplicates(["date", "symbol"]).pivot(index="date", columns="symbol", values="close")
    steps: list[DecisionStep] = []
    weekly_list = list(weekly)
    for i, day in enumerate(weekly_list[:-1]):
        day = pd.Timestamp(day)
        # Active universe A membership
        active_syms = None
        for eff, payload in sorted(universes.items()):
            if eff <= day:
                active_syms = set(payload["A"]["symbol"])
        if not active_syms:
            continue
        day_scores = oof[(oof["date"] == day) & (oof["symbol"].isin(active_syms))].sort_values(
            "gbdt_score", ascending=False
        )
        if day_scores.empty:
            # fall back to latest prior scores
            prior = oof[(oof["date"] <= day) & (oof["symbol"].isin(active_syms))]
            if prior.empty:
                continue
            day_scores = prior.sort_values("date").groupby("symbol", as_index=False).tail(1)
            day_scores = day_scores.sort_values("gbdt_score", ascending=False)
        top = day_scores.head(top_k)
        reg = regimes[regimes["date"] <= day]
        if reg.empty:
            probs = {"prob_bull": 0.34, "prob_sideways": 0.33, "prob_bear": 0.33}
            mom = 0.0
            vix_z = 0.0
        else:
            r = reg.iloc[-1]
            probs = {
                "prob_bull": float(r["prob_bull"]),
                "prob_sideways": float(r["prob_sideways"]),
                "prob_bear": float(r["prob_bear"]),
            }
            mom = 0.0
            vix_z = 0.0
        cands = []
        for row in top.itertuples(index=False):
            sec = merged[(merged["symbol"] == row.symbol) & (merged["date"] <= day)]
            sector = str(sec.iloc[-1]["sector"]) if not sec.empty else "UNKNOWN"
            px = prices.loc[day, row.symbol] if day in prices.index and row.symbol in prices.columns else np.nan
            if pd.isna(px):
                continue
            cands.append(
                {
                    "symbol": row.symbol,
                    "sector": sector,
                    "gbdt_score": float(row.gbdt_score),
                    "gbdt_rank_pct": float(row.gbdt_rank_pct),
                    "mom_20d": 0.0,
                    "atr_pct": 0.02,
                    "price": float(px),
                }
            )
        if not cands:
            continue
        next_day = pd.Timestamp(weekly_list[i + 1])
        next_prices = {}
        for c in cands:
            if next_day in prices.index and c["symbol"] in prices.columns and pd.notna(prices.loc[next_day, c["symbol"]]):
                next_prices[c["symbol"]] = float(prices.loc[next_day, c["symbol"]])
            else:
                next_prices[c["symbol"]] = c["price"]
        steps.append(
            DecisionStep(
                date=day,
                nifty_mom_20d=mom,
                vix_zscore=vix_z,
                candidates=cands,
                next_prices=next_prices,
                **probs,
            )
        )
    return steps


def run_ablation(fast: bool = True, force_synthetic: bool = False) -> pd.DataFrame:
    cfg = SystemConfig()
    ohlcv, sectors, macro, source = load_or_build(force_synthetic=force_synthetic)
    print(f"[data] source={source} rows={len(ohlcv)} symbols={ohlcv['symbol'].nunique()}")

    # Fast research defaults when using synthetic / CI
    if fast or source.startswith("SYNTHETIC"):
        hmm_cfg = RegimeConfig(train_window_days=126, refit_every_n_days=21, n_iter=40, random_state=42)
        gbdt_cfg = RankerConfig(
            forward_horizon_days=20,
            train_min_days=252,
            step_days=63,
            embargo_days=20,
            n_estimators=40,
            min_child_samples=10,
            num_leaves=15,
        )
        univ_lookback = 60
        univ_min_days = 40
        target_size = 30
        pool_c = 40
        ppo_steps = 3_000
    else:
        hmm_cfg = RegimeConfig(**cfg.hmm.__dict__)
        gbdt_cfg = RankerConfig(**{k: getattr(cfg.gbdt, k) for k in RankerConfig.__dataclass_fields__})
        univ_lookback = cfg.universe.lookback_days
        univ_min_days = cfg.universe.min_trading_days
        target_size = cfg.universe.target_size
        pool_c = cfg.universe.pool_size_c
        ppo_steps = cfg.rl.total_timesteps

    portfolio = PortfolioLimits(
        initial_capital=cfg.portfolio.initial_capital,
        max_open_positions=cfg.portfolio.max_open_positions,
        max_new_entries_per_week=cfg.portfolio.max_new_entries_per_week,
        max_positions_per_sector=cfg.portfolio.max_positions_per_sector,
        max_new_entries_per_sector_per_week=cfg.portfolio.max_new_entries_per_sector_per_week,
        hold_weeks=cfg.portfolio.hold_weeks,
    )
    costs = IndiaDeliveryCostConfig()

    print("[hmm] fitting rolling regimes…")
    regimes = compute_rolling_hmm_regimes(macro, hmm_cfg)
    print(f"[hmm] rows={len(regimes)} labels={regimes['regime'].value_counts().to_dict() if not regimes.empty else {}}")

    print("[gbdt] engineering features + walk-forward OOF…")
    feats = engineer_cross_sectional_features(ohlcv, sectors, regimes, horizon_days=gbdt_cfg.forward_horizon_days)
    oof = run_walk_forward_ranker(feats, gbdt_cfg)
    print(f"[gbdt] oof_rows={len(oof)}")
    signal_fn = make_oof_signal_fn(oof) if not oof.empty else placeholder_weekly_signal_ranker

    merged = attach_pit_sectors(ohlcv, sectors)
    base_univ_cfg = UniverseConfig(
        lookback_days=univ_lookback,
        min_trading_days=univ_min_days,
        pool_size_c=pool_c,
        target_size=target_size,
        max_per_sector=15,
        sector_col="sector",
    )
    univ_a = build_quarterly_universes(merged, base_univ_cfg)
    univ_c22 = build_quarterly_universes(
        merged,
        UniverseConfig(
            lookback_days=univ_lookback,
            min_trading_days=univ_min_days,
            pool_size_c=pool_c,
            target_size=target_size,
            max_per_sector=15,
            sector_col="sector",
        ),
    )
    univ_c12 = build_quarterly_universes(
        merged,
        UniverseConfig(
            lookback_days=univ_lookback,
            min_trading_days=univ_min_days,
            pool_size_c=pool_c,
            target_size=target_size,
            max_per_sector=15,
            sector_col="macro_sector",
        ),
    )

    nifty = macro.set_index(pd.to_datetime(macro["date"]))["nifty_close"].sort_index()
    rows = []

    print("[bt] Benchmark_Nifty50")
    bench = nifty_buy_hold_metrics(macro, portfolio.initial_capital, costs)
    rows.append(metrics_to_row("Benchmark_Nifty50", bench))

    def _run(name, variant, universes, use_hmm=False):
        print(f"[bt] {name}")
        result = run_walk_forward(
            merged,
            universes,
            variant,
            signal_fn=signal_fn,
            portfolio=portfolio,
            cost_cfg=costs,
            regime_df=regimes if use_hmm else None,
            regime_cfg=hmm_cfg,
        )
        # Overlay CAPM vs Nifty on weekly equity
        metrics = dict(result["metrics"])
        if not result["equity_curve"].empty:
            eq = result["equity_curve"].set_index("date")["equity"]
            joined = pd.concat(
                [eq.pct_change().rename("strat"), nifty.reindex(eq.index).pct_change().rename("bench")],
                axis=1,
            ).dropna()
            if len(joined) > 5 and joined["bench"].std() > 0:
                beta = np.cov(joined["strat"], joined["bench"])[0, 1] / np.var(joined["bench"])
                alpha_weekly = joined["strat"].mean() - beta * joined["bench"].mean()
                metrics["Ann_CAPM_Alpha_%"] = round(float(alpha_weekly * 52 * 100), 2)
                metrics["Ann_CAPM_Alpha_BPS_yr"] = round(float(alpha_weekly * 52 * 10_000), 1)
                metrics["Ann_Excess_vs_Nifty_BPS_yr"] = round(
                    float((joined["strat"].mean() - joined["bench"].mean()) * 52 * 10_000), 1
                )
        rows.append(metrics_to_row(name, metrics))
        return result, metrics

    r_a, m_a = _run("Baseline_A_Top150", "A", univ_a, use_hmm=False)
    r_ap, m_ap = _run("Baseline_APlus_HardCap", "A+", univ_a, use_hmm=False)
    r_c22, m_c22 = _run("Universe_C_22Sector", "C", univ_c22, use_hmm=False)
    r_c12, m_c12 = _run("Universe_C_12Macro", "C", univ_c12, use_hmm=False)

    # Pick best universe by Net CAGR among 2–5
    candidates = {
        "Baseline_A_Top150": (univ_a, "A", m_a),
        "Baseline_APlus_HardCap": (univ_a, "A+", m_ap),
        "Universe_C_22Sector": (univ_c22, "C", m_c22),
        "Universe_C_12Macro": (univ_c12, "C", m_c12),
    }
    best_name = max(
        candidates,
        key=lambda k: (candidates[k][2].get("Net_CAGR_%") is not None, candidates[k][2].get("Net_CAGR_%") or -1e9),
    )
    best_univ, best_variant, _ = candidates[best_name]
    print(f"[select] best universe for stack = {best_name}")

    r_hmm, m_hmm = _run(f"Stack_GBDT_HMM({best_name})", best_variant, best_univ, use_hmm=True)

    # RL full stack on DecisionSteps
    print("[rl] building DecisionSteps + PPO…")
    steps = _build_decision_steps(oof, regimes, ohlcv, merged, best_univ, top_k=5)
    rl_metrics = dict(m_hmm)
    if len(steps) >= 8:
        env_cfg = SizingEnvConfig(
            initial_capital=portfolio.initial_capital,
            top_k_candidates=5,
            max_open_positions=portfolio.max_open_positions,
            max_new_entries_per_step=portfolio.max_new_entries_per_week,
            max_positions_per_sector=portfolio.max_positions_per_sector,
            round_trip_cost_bps=costs.default_rt_bps,
            hold_steps=portfolio.hold_weeks,
            block_entries_in_bear=True,
            max_bear_prob_for_entry=hmm_cfg.max_bear_prob_for_entry,
        )
        model = train_ppo_sizer(
            steps,
            RlParams(total_timesteps=ppo_steps, n_steps=min(256, max(len(steps), 8)), batch_size=64),
            env_cfg=env_cfg,
            model_path=ROOT / "artifacts" / "models" / "ppo_sizer.zip",
        )
        env = NseSwingSizingEnv(steps, env_cfg)
        obs, _ = env.reset()
        done = False
        equities = []
        while not done:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, term, trunc, info = env.step(action)
            equities.append((info["date"], info["equity"]))
            done = term or trunc
        eq_df = pd.DataFrame(equities, columns=["date", "equity"]).set_index("date")
        from src.backtest.costs import resolve_rt_cost

        rt = resolve_rt_cost(costs)
        rl_metrics = compute_performance_metrics(
            eq_df,
            pd.DataFrame(),
            initial_capital=portfolio.initial_capital,
            rt=rt,
            traded_notional_abs=0.0,
            same_sector_weeks=0,
            two_entry_weeks=0,
            max_sector_share=[],
            cap_skips=0,
            regime_blocked_weeks=sum(1 for s in steps if s.prob_bear >= hmm_cfg.max_bear_prob_for_entry),
            nifty_curve=nifty,
        )
        print(f"[rl] trained timesteps={ppo_steps} steps={len(steps)}")
    else:
        print("[rl] insufficient DecisionSteps — copying HMM stack metrics")

    rows.append(metrics_to_row("Full_Stack_GBDT_HMM_RL", rl_metrics))

    # Option C sensitivity grid
    grid_rows = []
    for pool in (250, 300, 400) if not source.startswith("SYNTHETIC") else (pool_c,):
        for cap in (10, 15, 20):
            if source.startswith("SYNTHETIC") and pool != pool_c:
                continue
            ucfg = UniverseConfig(
                lookback_days=univ_lookback,
                min_trading_days=univ_min_days,
                pool_size_c=min(pool, ohlcv["symbol"].nunique()),
                target_size=target_size,
                max_per_sector=cap,
                sector_col="sector",
            )
            universes = build_quarterly_universes(merged, ucfg)
            result = run_walk_forward(merged, universes, "C", signal_fn, portfolio, costs)
            grid_rows.append(
                {
                    "pool_size_c": ucfg.pool_size_c,
                    "max_per_sector": cap,
                    **result["metrics"],
                }
            )
    grid = pd.DataFrame(grid_rows)
    grid_path = ROOT / "reports" / "option_c_sensitivity_grid.csv"
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    grid.to_csv(grid_path, index=False)

    # Sub-period stability (2y blocks)
    sub_rows = []
    years = sorted(pd.to_datetime(ohlcv["date"]).dt.year.unique())
    for y0 in years[::2]:
        y1 = y0 + 1
        mask = (pd.to_datetime(merged["date"]).dt.year >= y0) & (pd.to_datetime(merged["date"]).dt.year <= y1)
        sub = merged.loc[mask]
        if sub["date"].nunique() < 100:
            continue
        universes = build_quarterly_universes(sub, base_univ_cfg)
        if not universes:
            continue
        result = run_walk_forward(sub, universes, best_variant, signal_fn, portfolio, costs, regime_df=regimes, regime_cfg=hmm_cfg)
        sub_rows.append({"period": f"{y0}-{y1}", "variant": best_name, **result["metrics"]})
    sub = pd.DataFrame(sub_rows)
    sub.to_csv(ROOT / "reports" / "subperiod_stability.csv", index=False)

    summary = build_summary_table(rows)
    note = (
        f"**Data source:** `{source}`. "
        f"Cost model default RT ≈ {costs.default_rt_bps:.1f} bps "
        f"(statutory {costs.statutory_rt_bps:.1f} + slippage {2*costs.slippage_per_leg_bps:.1f}). "
        f"Best pre-HMM universe: `{best_name}`."
    )
    if source.startswith("SYNTHETIC"):
        note += (
            " Synthetic panel used because `data/clean/nse_ohlcv_10y_adj.parquet` was absent — "
            "replace with real Bhavcopy parquet for production Alpha/BPS numbers."
        )
    csv_path, md_path = write_summary(summary, ROOT / "reports", data_note=note)
    print(summary.to_string(index=False))
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")
    print(f"Wrote {grid_path}")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", default=True)
    parser.add_argument("--full", action="store_true", help="Use production-scale hyperparameters")
    parser.add_argument("--force-synthetic", action="store_true")
    args = parser.parse_args()
    run_ablation(fast=not args.full, force_synthetic=args.force_synthetic)


if __name__ == "__main__":
    main()
