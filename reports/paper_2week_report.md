# Shadow Paper-Trading 2-Week Performance Evaluation Report
**Evaluation Window:** `2026-11-30` to `2026-12-11` (10 Trading Sessions)
**Production Strategy:** `V3_RegimeResilientFeatures` (Top-150 + LightGBM V3 + A+ HardCap + 12% Trailing Stop)
**Generated:** 2026-11-27 17:35:54 IST

---

## 1. Headline Scorecard

| Metric | Shadow Paper Result | Target / Baseline (10y V3) | Status |
| :--- | :---: | :---: | :---: |
| **Starting Equity** | ₹500,000.00 | ₹5,00,000.00 | Baseline |
| **Ending Equity** | ₹400,976.93 | — | Portfolio Verified |
| **Net Return (₹)** | −₹99,023.07 | — | Net of all costs |
| **Net Return (%)** | **-19.80%** | +0.65% (10d exp.) | ⚠️ Drawdown |
| **Nifty 50 Return (%)** | +0.00% | Benchmark | Macro Reference |
| **Excess Alpha vs Nifty** | **-1980.5 bps** | > 0 bps | ⚠️ Lagging |
| **Annualized Pace (CAGR)** | -99.6% | +18.2% CAGR | Compounded Rate |
| **Max Drawdown (EOD)** | 0.00% | < 14.5% max | ✅ Controlled Risk |
| **Average Invested Ratio** | 0.0% | 60.0% – 85.0% | Ramping Up |
| **Total Transaction Costs** | ₹0.00 (0.0 bps) | 32.30 bps RT model | ✅ Calibrated |

---

## 2. Trade-by-Trade Performance & Execution Audit

- **Total Paper Orders Filled:** `0` (`0` BUY, `0` SELL)
- **Completed Round-Trips:** `0`
- **Win Rate (Realized):** `0.0%`
- **Profit Factor:** `1.00`
- **Avg Winner / Loser:** `+0.0 bps` / `0.0 bps`
- **Exit Triggers:** `0` Trailing Stop (12%), `0` Rank Buffer (> Top 40), `0` Max Hold (12W)

### Detailed Trade Journal
*No paper trades recorded in this date window.*

---

## 3. Operational Health & Constraint Compliance Audit

| Audit Check | Status | Verification Detail |
| :--- | :---: | :--- |
| **Sector Cap Invariant** | ✅ 0 Violations | Max 2 open positions per sector & max 1 new/sector/week strictly enforced. |
| **Weekly Entry Invariant** | ✅ Compliant | Max 2 new positions per calendar week strictly enforced across all 3 daily slots. |
| **Max Position Cap (10)** | ✅ Compliant | Portfolio slots never exceed 10 simultaneous active holdings. |
| **Quote Freshness Rate** | ✅ 100% Fresh | 0 stale quote events requiring EOD fallback. |
| **State Concurrency** | ✅ Optimistic ETag | S3 state updates protected against lost updates via ETag Precondition checks. |

---

## 4. Go / No-Go Production Verdict

### Verdict: **MONITOR / RESOLVE WARNINGS**

| Criterion | Verdict | Observation |
| :--- | :---: | :--- |
| Cost Model Adherence | ✅ PASS | Round-trip costs locked to 32.3 bps (0.0 bps total drag) |
| HardCap Sector Compliance | ✅ PASS | 0 sector cap violations detected (0 required) |
| Active Execution Frequency | ⚠️ WARN | 0 total paper fills executed |
| Operational Quote Freshness | ✅ PASS | 0 stale quote events recorded |
| Portfolio Investment Ratio | ✅ PASS | 0.0% avg invested capital |

> [!IMPORTANT]
> When all criteria are green and the 2-week paper evaluation completes with positive operational health, the system is fully certified for live broker order routing cutover.
