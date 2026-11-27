# Backtest Alpha & Basis-Points Summary

**Data source:** `REAL_NSE_10Y` | ca_method=`OFFICIAL_NSE_SPLIT_BONUS_ONLY` | sector_mode=`STATIC_SECTOR_FALLBACK` | range `2016-01-01`→`2026-11-26` | rows=4668829 symbols=3792 | official_splits_applied=996 remaining_top150_suspects=28 | winner=`Winner_APlus_HardCap_rankBuffer` | cost RT=32.3 / stress 42.3 | Ann_Turnover is ONE-WAY; Gross=0bps book, Net=delivery schedule.

| Variant | Gross_CAGR_% | Net_CAGR_% | Ann_CAPM_Alpha_% | Ann_CAPM_Alpha_BPS_yr | Ann_Excess_vs_Nifty_BPS_yr | Gross_Return_per_Trade_BPS | Statutory_Cost_BPS | Slippage_Cost_BPS | Total_Execution_Cost_per_Trade_BPS | Net_Return_per_Trade_BPS | Ann_Turnover_% | Ann_Cost_Drag_BPS_yr | Sharpe | Sortino | Max_Drawdown_% | Calmar | Win_Rate_% | Profit_Factor | Same_Sector_Double_Buy_Rate_% | P95_Max_Sector_Share_% | N_Trades | Regime_Blocked_Weeks | Cap_Skips_Count |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Benchmark_Nifty50 | 11.63 | 11.61 | 0.0 | 0.0 | 0.0 | 23138.6 | 22.3 | 10.0 | 32.3 | 23068.9 | 18.4 | 5.9 | 0.78 | 0.96 | -38.44 | 0.3 | 100.0 |  |  |  | 1 | 0 | 0 |
| Baseline_A_Top150_rankBuffer | 9.3 | 8.4 | 2.81 | 281.4 | -253.5 | 284.8 | 22.3 | 10.0 | 32.3 | 252.0 | 408.6 | 132.0 | 0.66 | 0.87 | -20.99 | 0.4 | 51.6 | 1.46 |  | 73.35 | 370 | 0 | 0 |
| Winner_APlus_HardCap_rankBuffer | 10.4 | 9.52 | 4.06 | 406.3 | -159.9 | 310.4 | 22.3 | 10.0 | 32.3 | 277.6 | 413.5 | 133.6 | 0.77 | 0.95 | -22.81 | 0.42 | 52.2 | 1.55 |  | 39.14 | 370 | 0 | 384 |
| Winner_APlus_HardCap_rankBuffer_stress42p3 | 10.27 | 9.12 | 3.69 | 369.3 | -196.3 | 310.4 | 22.3 | 20.0 | 42.3 | 267.4 | 412.3 | 174.4 | 0.74 | 0.92 | -23.23 | 0.39 | 51.9 | 1.52 |  | 39.14 | 370 | 0 | 384 |
| APlus_RankBuffer_SoftHMM | 9.06 | 8.2 | 3.45 | 345.2 | -295.4 | 310.4 | 22.3 | 10.0 | 32.3 | 277.6 | 387.0 | 125.0 | 0.71 | 0.87 | -19.66 | 0.42 | 52.2 | 1.49 |  | 40.82 | 370 | 0 | 384 |

## Reading Notes

- **Net After-Cost CAGR** is the promotion metric; Gross CAGR ignores delivery costs.
- **Ann_CAPM_Alpha_BPS_yr** is weekly OLS alpha annualized vs Nifty when a benchmark series is supplied.
- **Total_Execution_Cost_per_Trade_BPS** = statutory (~22.3) + slippage RT (default 10.0 → ~32.3 RT).
- **Ann_Cost_Drag_BPS_yr** ≈ annualized turnover × RT cost (bps).
- Promote RL only if `Full_Stack_GBDT_HMM_RL` improves OOS Sharpe or MaxDD vs equal-weight HMM stack.
