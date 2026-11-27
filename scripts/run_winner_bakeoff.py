#!/usr/bin/env python3
"""Streamlined 5-config bake-off on official-CA 10y panel (no Option C / PPO)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import nifty_buy_hold_metrics, run_walk_forward
from src.backtest.report import build_summary_table, metrics_to_row, write_summary
from src.config import IndiaDeliveryCostConfig, PortfolioLimits, SystemConfig
from src.models.gbdt_ranker import (
    RankerConfig,
    engineer_cross_sectional_features,
    make_oof_signal_fn,
    run_walk_forward_ranker,
)
from src.models.hmm_regime import RegimeConfig, compute_rolling_hmm_regimes
from src.universe.pit_universe import UniverseConfig, attach_pit_sectors, build_quarterly_universes

CLEAN = ROOT / "data" / "clean"
REPORTS = ROOT / "reports"


def _overlay_capm(metrics: dict, equity_curve: pd.DataFrame, nifty: pd.Series) -> dict:
    out = dict(metrics)
    if equity_curve.empty:
        return out
    eq = equity_curve.set_index("date")["equity"]
    joined = pd.concat(
        [
            eq.pct_change(fill_method=None).rename("strat"),
            nifty.reindex(eq.index).pct_change(fill_method=None).rename("bench"),
        ],
        axis=1,
    ).dropna()
    if len(joined) > 5 and joined["bench"].std() > 0:
        beta = np.cov(joined["strat"], joined["bench"])[0, 1] / np.var(joined["bench"])
        alpha_w = joined["strat"].mean() - beta * joined["bench"].mean()
        out["Ann_CAPM_Alpha_%"] = round(float(alpha_w * 52 * 100), 2)
        out["Ann_CAPM_Alpha_BPS_yr"] = round(float(alpha_w * 52 * 10_000), 1)
        out["Ann_Excess_vs_Nifty_BPS_yr"] = round(
            float((joined["strat"].mean() - joined["bench"].mean()) * 52 * 10_000), 1
        )
    return out


def _unknown_sector_audit(trades: pd.DataFrame, name: str) -> dict:
    if trades is None or trades.empty:
        return {"variant": name, "pct_trades_unknown": None, "pct_notional_unknown": None}
    unknown = trades["sector"].astype(str).eq("UNKNOWN")
    notional = (trades["entry_price"] * trades["shares"]).astype(float)
    return {
        "variant": name,
        "n_trades": int(len(trades)),
        "pct_trades_unknown": round(float(unknown.mean() * 100), 2),
        "pct_notional_unknown": round(float(notional[unknown].sum() / max(notional.sum(), 1e-8) * 100), 2),
    }


def main() -> None:
    ohlcv = pd.read_parquet(CLEAN / "nse_ohlcv_10y_adj.parquet")
    sectors = pd.read_parquet(CLEAN / "nse_pit_sectors.parquet")
    macro = pd.read_parquet(CLEAN / "nifty_vix_10y.parquet")
    meta = json.loads((CLEAN / "panel_meta.json").read_text())
    oof_path = ROOT / "artifacts" / "models" / "gbdt_oof_real10y.parquet"
    regime_path = ROOT / "artifacts" / "models" / "hmm_regimes_real10y.parquet"
    cfg = SystemConfig()
    if regime_path.exists():
        regimes = pd.read_parquet(regime_path)
        print(f"[hmm] cached regimes={len(regimes)}", flush=True)
    else:
        print("[hmm] fitting…", flush=True)
        regimes = compute_rolling_hmm_regimes(macro, RegimeConfig())
        regime_path.parent.mkdir(parents=True, exist_ok=True)
        regimes.to_parquet(regime_path, index=False)
    if oof_path.exists():
        oof = pd.read_parquet(oof_path)
        print(f"[gbdt] cached oof_rows={len(oof)}", flush=True)
    else:
        print("[gbdt] features + OOF on official-CA panel…", flush=True)
        feats = engineer_cross_sectional_features(
            ohlcv, sectors, regimes, horizon_days=cfg.gbdt.forward_horizon_days
        )
        oof = run_walk_forward_ranker(
            feats,
            RankerConfig(
                forward_horizon_days=cfg.gbdt.forward_horizon_days,
                train_min_days=cfg.gbdt.train_min_days,
                step_days=cfg.gbdt.step_days,
                embargo_days=cfg.gbdt.embargo_days,
                n_estimators=cfg.gbdt.n_estimators,
                learning_rate=cfg.gbdt.learning_rate,
                num_leaves=cfg.gbdt.num_leaves,
                min_child_samples=cfg.gbdt.min_child_samples,
            ),
        )
        oof_path.parent.mkdir(parents=True, exist_ok=True)
        oof.to_parquet(oof_path, index=False)
        print(f"[gbdt] oof_rows={len(oof)}", flush=True)
    signal_fn = make_oof_signal_fn(oof)
    merged = attach_pit_sectors(ohlcv, sectors)
    univ = build_quarterly_universes(
        merged,
        UniverseConfig(
            lookback_days=126,
            min_trading_days=90,
            pool_size_c=300,
            target_size=150,
            max_per_sector=15,
            sector_col="sector",
        ),
    )
    costs = IndiaDeliveryCostConfig()
    nifty = macro.set_index(pd.to_datetime(macro["date"]))["nifty_close"].sort_index()

    buffer = PortfolioLimits(
        exit_mode="rank_buffer",
        rank_buffer_top_n=60,
        trailing_stop_pct=0.12,
        max_hold_weeks=12,
        rank_exit_confirm_weeks=2,
        max_new_entries_per_week=1,
    )
    soft_hmm = RegimeConfig(
        block_bear_entries=False,
        soft_bear_scale=0.5,
        soft_bear_prob_threshold=0.65,
    )

    rows = []
    audits = []
    results = {}

    # 1. Benchmark
    rows.append(metrics_to_row("Benchmark_Nifty50", nifty_buy_hold_metrics(macro, buffer.initial_capital, costs)))

    specs = [
        ("Baseline_A_Top150_rankBuffer", "A", univ, buffer, None, False),
        ("Winner_APlus_HardCap_rankBuffer", "A+", univ, buffer, None, False),
        ("Winner_APlus_HardCap_rankBuffer_stress42p3", "A+", univ, buffer, None, True),
        ("APlus_RankBuffer_SoftHMM", "A+", univ, buffer, soft_hmm, False),
    ]

    for name, variant, universes, port, regime_cfg, stress in specs:
        print(f"[bt] {name}", flush=True)
        res = run_walk_forward(
            merged,
            universes,
            variant,
            signal_fn=signal_fn,
            portfolio=port,
            cost_cfg=costs,
            stress_slippage=stress,
            regime_df=regimes if regime_cfg is not None else None,
            regime_cfg=regime_cfg or RegimeConfig(block_bear_entries=False),
        )
        m = _overlay_capm(res["metrics"], res["equity_curve"], nifty)
        m["ExitMode"] = port.exit_mode
        m["StressSlippage"] = stress
        rows.append(metrics_to_row(name, m))
        results[name] = res
        print(
            f"  Net={m.get('Net_CAGR_%')} Gross={m.get('Gross_CAGR_%')} "
            f"TO={m.get('Ann_Turnover_%')} Drag={m.get('Ann_Cost_Drag_BPS_yr')} "
            f"N={m.get('N_Trades')} Blocked={m.get('Regime_Blocked_Weeks')}",
            flush=True,
        )
        if name == "Winner_APlus_HardCap_rankBuffer":
            audits.append(_unknown_sector_audit(res["trades"], name))
            # Map Top-150 UNKNOWN symbols for the report
            unknown_syms = sorted(
                {
                    str(s)
                    for payload in univ.values()
                    for s in payload["A"]["symbol"].tolist()
                    if str(
                        sectors.loc[sectors["symbol"] == s, "sector"].iloc[0]
                        if (sectors["symbol"] == s).any()
                        else "UNKNOWN"
                    )
                    == "UNKNOWN"
                }
            )
            audits.append({"top150_unknown_symbols_sample": unknown_syms[:50], "n_unknown": len(unknown_syms)})

    winner = "Winner_APlus_HardCap_rankBuffer"
    assert results[winner]["metrics"]["Regime_Blocked_Weeks"] == 0, "Winner must be unblocked"

    # Subperiods on TRUE winner (no HMM)
    sub_rows = []
    for y0, y1 in [(2018, 2019), (2020, 2021), (2022, 2023), (2024, 2026)]:
        mask = (pd.to_datetime(merged["date"]).dt.year >= y0) & (pd.to_datetime(merged["date"]).dt.year <= y1)
        sub = merged.loc[mask]
        sub_univ = build_quarterly_universes(
            sub,
            UniverseConfig(lookback_days=126, min_trading_days=90, pool_size_c=300, target_size=150, max_per_sector=15),
        )
        if not sub_univ:
            continue
        res = run_walk_forward(sub, sub_univ, "A+", signal_fn, buffer, costs)
        m = res["metrics"]
        print(f"[sub] {y0}-{y1} Net={m.get('Net_CAGR_%')} Blocked={m.get('Regime_Blocked_Weeks')} N={m.get('N_Trades')}", flush=True)
        assert m.get("Regime_Blocked_Weeks", 0) == 0
        sub_rows.append({"period": f"{y0}-{y1}", "variant": winner, **m})
    pd.DataFrame(sub_rows).to_csv(REPORTS / "subperiod_stability.csv", index=False)

    summary = build_summary_table(rows)
    note = (
        f"**Data source:** `REAL_NSE_10Y` | ca_method=`{meta.get('ca_method', 'OFFICIAL')}` | "
        f"sector_mode=`{meta.get('sector_mode')}` | range `2016-01-01`→`2026-11-26` | "
        f"rows={len(ohlcv)} symbols={ohlcv['symbol'].nunique()} | "
        f"official_splits_applied={meta.get('splits_applied')} remaining_top150_suspects={meta.get('remaining_split_suspects')} | "
        f"winner=`{winner}` | cost RT=32.3 / stress 42.3 | "
        f"Ann_Turnover is ONE-WAY; Gross=0bps book, Net=delivery schedule."
    )
    write_summary(summary, REPORTS, data_note=note)
    (REPORTS / "unknown_sector_audit.json").write_text(json.dumps(audits, indent=2), encoding="utf-8")
    (REPORTS / "data_audit.json").write_text(
        json.dumps({"audit": meta, "unknown": audits}, indent=2, default=str), encoding="utf-8"
    )
    print(summary.to_string(index=False))
    print("UNKNOWN_AUDIT", json.dumps(audits, indent=2))
    print("DONE_BAKEOFF")


if __name__ == "__main__":
    main()
