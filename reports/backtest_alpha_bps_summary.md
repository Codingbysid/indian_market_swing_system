# Backtest Alpha & Basis-Points Summary

**Data source:** `SYNTHETIC_RESEARCH_PANEL`. Cost model default RT ≈ 32.3 bps (statutory 22.3 + slippage 10.0). Best pre-HMM universe: `Baseline_APlus_HardCap`. Synthetic panel used because `data/clean/nse_ohlcv_10y_adj.parquet` was absent — replace with real Bhavcopy parquet for production Alpha/BPS numbers.

| Variant | Gross_CAGR_% | Net_CAGR_% | Ann_CAPM_Alpha_% | Ann_CAPM_Alpha_BPS_yr | Ann_Excess_vs_Nifty_BPS_yr | Gross_Return_per_Trade_BPS | Statutory_Cost_BPS | Slippage_Cost_BPS | Total_Execution_Cost_per_Trade_BPS | Net_Return_per_Trade_BPS | Ann_Turnover_% | Ann_Cost_Drag_BPS_yr | Sharpe | Sortino | Max_Drawdown_% | Calmar | Win_Rate_% | Profit_Factor | Same_Sector_Double_Buy_Rate_% | P95_Max_Sector_Share_% | N_Trades | Regime_Blocked_Weeks | Cap_Skips_Count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Benchmark_Nifty50 | 4.11 | 4.08 | 0.0 | 0.0 | 0.0 | 3652.1 | 22.3 | 10.0 | 32.3 | 3613.9 | 25.9 | 8.4 | 1.0 | 1.53 | -10.18 | 0.4 | 100.0 |  |  |  | 1 | 0 | 0 |
| Baseline_A_Top150 | 4.14 | 1.57 | -1.05 | -105.1 | -255.7 | 53.7 | 22.3 | 10.0 | 32.3 | 21.3 | 1919.3 | 619.9 | 0.27 | 0.45 | -11.63 | 0.14 | 49.5 | 1.07 | 11.28 | 39.02 | 666 | 0 | 0 |
| Baseline_APlus_HardCap | 4.99 | 2.56 | 0.06 | 5.8 | -162.1 | 64.2 | 22.3 | 10.0 | 32.3 | 31.8 | 1926.1 | 622.1 | 0.44 | 0.69 | -9.55 | 0.27 | 49.1 | 1.12 | 0.0 | 26.41 | 666 | 0 | 178 |
| Universe_C_22Sector | 4.14 | 1.57 | -1.05 | -105.1 | -255.7 | 53.7 | 22.3 | 10.0 | 32.3 | 21.3 | 1919.3 | 619.9 | 0.27 | 0.45 | -11.63 | 0.14 | 49.5 | 1.07 | 11.28 | 39.02 | 666 | 0 | 0 |
| Universe_C_12Macro | 4.14 | 1.57 | -1.05 | -105.1 | -255.7 | 53.7 | 22.3 | 10.0 | 32.3 | 21.3 | 1919.3 | 619.9 | 0.27 | 0.45 | -11.63 | 0.14 | 49.5 | 1.07 | 11.28 | 39.02 | 666 | 0 | 0 |
| Stack_GBDT_HMM(Baseline_APlus_HardCap) | 4.77 | 2.35 | -0.23 | -23.3 | -182.4 | 62.3 | 22.3 | 10.0 | 32.3 | 29.9 | 1890.4 | 610.6 | 0.41 | 0.65 | -9.55 | 0.25 | 50.0 | 1.11 | 0.0 | 26.71 | 658 | 4 | 178 |
| Full_Stack_GBDT_HMM_RL | 0.0 | 0.0 | 0.0 | 0.0 | -28.7 |  | 22.3 | 10.0 | 32.3 |  | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |  |  |  |  |  | 0 | 0 | 0 |

## Reading Notes

- **Net After-Cost CAGR** is the promotion metric; Gross CAGR ignores delivery costs.
- **Ann_CAPM_Alpha_BPS_yr** is weekly OLS alpha annualized vs Nifty when a benchmark series is supplied.
- **Total_Execution_Cost_per_Trade_BPS** = statutory (~22.3) + slippage RT (default 10.0 → ~32.3 RT).
- **Ann_Cost_Drag_BPS_yr** ≈ annualized turnover × RT cost (bps).
- Promote RL only if `Full_Stack_GBDT_HMM_RL` improves OOS Sharpe or MaxDD vs equal-weight HMM stack.

## Exact Findings (this run)

| Question | Answer on this panel |
| --- | --- |
| Best Net CAGR / Sharpe | **Baseline_APlus_HardCap** (Net CAGR 2.56%, Sharpe 0.44, MaxDD −9.55%) |
| Does HMM help A+? | Slightly worse Net CAGR (2.35% vs 2.56%); 4 weeks blocked |
| Option C vs A+ concentration | **A+ wins**: P95 sector share 26.4% vs 39.0% on A/C; same-sector double-buy 0% vs 11.3% |
| Full RL stack | **Do not promote** on this run (flat equity / no trade blotter after 3k PPO steps) |
| Cost model | Statutory 22.3 bps + 10.0 bps slippage RT = **32.3 bps**/trade; A+ ann. cost drag ≈ **622 bps/yr** at ~1926% turnover |

**Caveat:** Panel is `SYNTHETIC_RESEARCH_PANEL`. Drop real files at `data/clean/nse_ohlcv_10y_adj.parquet`, `nse_pit_sectors.parquet`, `nifty_vix_10y.parquet` and re-run `python scripts/run_full_ablation.py --full` for production Alpha/BPS.
