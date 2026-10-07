Weekly Signal Upgrade and Updated Portfolio Plan
7 October 2026 — decision and implementation addendum

Purpose

Increase the frequency of independent, economically viable swing opportunities while protecting the current cash pool and preserving the profit-only rules for legacy holdings. This is a design and research handoff, not an activated trading change or a set of current buy recommendations.

This addendum replaces the earlier portfolio quantities and provisional allocation settings. It also changes the proposed strategy architecture: the original fresh EMA cross becomes one candidate family to benchmark, rather than the only possible entry. The earlier audit's requirements for honest labels, execution-aware validation, JSON inference, S3 state, and protected legacy exits remain.

The repository was rechecked on 7 October: main is still 4b634f2ba46d00f00bac98d10178b0d59e74ebf9 (17 August). The newer deployed portfolio monitor/state implementation remains absent from that branch. Recover deployed source before integrating changes; do not overwrite a newer live Lambda with this older branch.

1. Verified screenshot snapshot

Source: the user-supplied screenshot dated 7 October 2026, approximately 11:25. Prices below are screenshot observations, not refreshed market quotations.

| Holding | Quantity | Average cost | Screenshot LTP | Invested | Current value | Unrealized P&L | Weight in stock holdings |
|---|---:|---:|---:|---:|---:|---:|---:|
| BPCL | 6 | ₹318.65 | ₹297.35 | ₹1,911.90 | ₹1,784.10 | -₹127.80 | 1.68% |
| CDSL | 8 | ₹1,363.75 | ₹1,277.80 | ₹10,910.00 | ₹10,222.40 | -₹687.60 | 9.60% |
| HAL | 16 | ₹5,040.63 | ₹4,790.90 | ₹80,650.10 | ₹76,654.40 | -₹3,995.70 | 71.99% |
| INFY | 5 | ₹1,030.00 | ₹1,002.60 | ₹5,150.00 | ₹5,013.00 | -₹137.00 | 4.71% |
| ZENTEC | 8 | ₹1,711.25 | ₹1,600.50 | ₹13,690.00 | ₹12,804.00 | -₹886.00 | 12.03% |
| Total | | | | ₹1,12,312.00 | ₹1,06,477.90 | -₹5,834.10 | 100% |

HAL plus ZENTEC = 84.02% of holdings value. HAL plus BPCL = 73.67% in the PSU overlay. These categories overlap and must not be added together.

The broker's displayed HAL invested total differs by two paise from multiplying the rounded displayed average cost by quantity. Use the broker's displayed totals for portfolio reconciliation, and the actual fill ledger for precise accounting.

Compared with the earlier supplied book, HAL increased 14→16 shares, CDSL 6→8 and ZENTEC 6→8. Invested cost increased by ₹14,889.98. The reduced averages are consistent with additions below the earlier average costs, but transaction dates, fees and cash flows must be verified from fills.

Current available cash is NOT in this screenshot. Only if there were no other transactions, charges or deposits would the earlier ₹27,560 imply approximately ₹12,670.02 remaining. Do not write that estimate into the S3 live state or use it for live orders. Obtain the actual available cash, reservations and unsettled amounts.

2. What the updated book implies

The portfolio currently has two separate problems:
- Concentrated legacy capital that cannot be sold below the protected breakeven.
- A new-trade engine whose single, rare entry predicate limits opportunities.

Adding more shares of legacy names worsens the first problem. Buying unrelated stocks with existing cash prevents additional concentration in new trades, but does not reduce HAL's rupee exposure or HAL's fraction of total account NAV at the instant of purchase. It moves cash into other equity risk. Actual concentration repair requires permitted partial sales, relative asset performance, retained profits and/or contributions.

Immediate policy recommendation:
- No additions to any current holding.
- No additional defence or PSU theme exposure while current breaches persist.
- Keep all five screenshot holdings classified as legacy_profit_only.
- Park the high-beta Satellite allocation in cash during the repair period. Keep its reporting envelope if useful, but do not spend it to generate extra alerts.
- After validation, admit at most two new, liquid, unrelated Core positions initially.
- Reassess position capacity only after cash and independent strategy evidence improve.

These are proposed admission rules; they are not instructions to sell current holdings at a loss.

3. Legacy sell eligibility

Preserve the user rule:

    BE = avg_cost * 1.0025 + 18 / current_quantity

Indicative floors from the displayed averages:

| Holding | Formula BE, approximately | Rise from screenshot LTP to BE |
|---|---:|---:|
| BPCL | ₹322.45 | 8.44% |
| CDSL | ₹1,369.41 | 7.17% |
| HAL | ₹5,054.36 | 5.50% |
| INFY | ₹1,036.18 | 3.35% |
| ZENTEC | ₹1,717.78 | 7.33% |

Round executable prices upward to the instrument's valid tick. These are BE floors, not T1 targets or recommendations to sell immediately.

All five prices are below BE in the screenshot. Therefore there is no eligible profit-only sale from this snapshot alone. A weekly sell quota for these holdings is incompatible with the protected rule when they remain below BE.

Preserve:
- T1=max(BE+ATR, nearest causally known resistance above BE).
- T2=average cost+3ATR, bumped above T1 where required.
- Approximately one-third per stage, remainder runner.
- Trail active only after observed LTP>T1.
- Trail=max(BE,HWM-1.5ATR).
- Weakness sale only when the daily EMA weakness condition is valid and executable price exceeds BE.
- Net-positive tranche check using actual sell quantity and applicable costs.

With the updated quantities, a deterministic full-position stage schedule could be HAL 5/5/6, CDSL 3/3/2, ZENTEC 3/3/2, BPCL 2/2/2 and INFY 2/1/2. Apply this only after confirming that no previous stage was already executed; do not reset stage flags or HWM from the screenshot.

No ATR/resistance history was supplied, so do not invent numerical T1/T2/trail levels.

A GTT trigger is not a fill. An executable legacy sell limit must remain at or above the stricter BE/net-profit floor. If price gaps below it, retain the position and record the unfilled instruction. No below-BE liquidation or claim of a guaranteed protective exit.

4. Broaden the universe before broadening risk

Start with the dated official Nifty 200 membership, which covers large and mid-cap companies. Use the official index constituent download; archive the membership, retrieval timestamp and source hash. Verify symbols against TradingView and broker instrument/lot metadata.

The current Screener screens become optional tags or ranking context, not mandatory eligibility gates for every trade. Do not use a current RSI-oversold screen as the historical universe. BSE-only and thinly traded supplemental names can remain research candidates, but exclude them from the initial new live strategies.

Initial admission filter to test:
- Mainboard cash equity with verified exchange-qualified symbol and tradable lot.
- At least 300 valid completed daily sessions for shared indicator calculations.
- Median 20-session rupee turnover >=₹10 crore.
- Proposed order <=0.05% of median daily turnover.
- Valid ATR, no unresolved corporate-action discontinuity, no stale price series.
- No current holding; no prohibited defence/PSU or strongly correlated exposure.
- Positive integer quantity within cash, cost and risk limits.
- Known trading suspension/circuit/settlement restrictions handled conservatively.

These thresholds are starting research hypotheses, not empirically optimized values. Do not mistake daily turnover for a measured bid/ask spread.

If opportunity coverage remains poor AFTER evaluating two additional setups, test a dated subset of Nifty 500: the most liquid names satisfying the same rules, across multiple sectors, within measured Lambda runtime. Do not immediately fetch 500 full histories sequentially at 09:25.

Historical Nifty membership must be point-in-time. Current constituents replayed over old prices are survivor-selected and are development evidence only until corrected or confirmed prospectively.

5. Replace the single mandatory entry with independently tested setup families

This is an intentional proposed change from the old requirement that every trade pass a fresh EMA9/21 cross AND RSI40–65 AND BB position<0.60. Keep that exact rule as the baseline family. Do not apply its Bollinger/RSI gates to every new family.

All formulas use completed sessions. New entries remain manual cash-delivery swing trades at the existing morning schedule.

Family A — original cross / baseline

Retain the exact EMA cross, RSI and Bollinger predicate for comparison. It may contribute opportunities if it passes corrected after-cost validation; it is not automatically the champion because it already exists.

Family B — trend pullback resumption / primary new candidate

Initial fixed specification:
1. Close_d > SMA50_d, and SMA50_d > SMA50_(d-10).
2. EMA9_d > EMA21_d; no fresh crossover required.
3. At least one of the preceding five sessions had Low <= EMA21 + 0.25ATR, using that session's own completed indicators.
4. Close_d > High_(d-1), and Close_d > EMA9_d.
5. Close_d <= EMA21_d + 1.5ATR_d, to avoid an overextended recovery.
6. Stock 20-session return minus Nifty 20-session return >0.

Hypothesis: capture a renewed advance after a pullback within an established uptrend. The old fresh-cross condition misses such moves because EMA9 may stay above EMA21 throughout the pullback.

Family C — range breakout / second new candidate

Initial fixed specification:
1. Close_d > maximum High over d-20 through d-1.
2. Close_d > SMA50_d, and SMA50_d > SMA50_(d-10).
3. Volume_d >=1.3*median(Volume over d-20 through d-1).
4. Stock 20-session return minus Nifty 20-session return >0.
5. The first signal of an episode is actionable; successive new highs are not repeated entry alerts.
6. The current executable entry reference must be within the declared gap/chase limit.

Hypothesis: participate when a prior trading range resolves upward with participation. A fresh EMA cross may have happened much earlier; BB position will often exceed 0.60. Forcing those old gates onto this setup defeats its purpose.

For B/C, retain the initial -1.5ATR/+3ATR barriers from the actual fill and a ten-session maximum holding period for an apples-to-apples first evaluation. Do not tune entry rules and many exit variants at once. The earlier execution-aware vertical-exit contract applies.

Deduplicate cross-family candidates by exchange:symbol and episode. Emit one BUY proposal, with all qualifying setup tags, and reserve cash once. During a held/pending position there are no further buy proposals for that symbol.

An episode ends on actual trade closure/expiry plus a predeclared reset condition. For an unfilled proposal, reset after five completed sessions and require that the relevant setup was false on at least one completed session before a new episode can begin. This prevents stale repeated alerts from inflating weekly counts.

Do not introduce a fourth falling-market mean-reversion strategy just to populate quiet weeks. Add it only as a separately validated later experiment.

6. Regime and entry quality

Default new entries:
- RISK_ON: completed Nifty Close>EMA50 and required feed/state data valid.
- RISK_OFF: track candidates in shadow, no actionable B/C longs initially.
- DATA_INVALID: no actionable new entries, operational report explains the missing requirement.

Keep the actual regime value in every feature vector. Use breadth and relative strength as research inputs; avoid adding multiple brittle hard gates before evaluating their effect on frequency and performance.

Execution constraints:
- Daily signal from the most recent completed exchange session.
- New entry after actual alert emission plus measured/manual latency.
- Start by testing a maximum absolute displacement of 0.5ATR from signal Close; wider gaps are skipped.
- Alert expires after five minutes or at 09:45, whichever is first.
- No retrospective daily-bar assumptions about fills before an alert.
- Current quotes must be demonstrably fresh; no model probability rescues a delayed/stale price.

TradingView/tvDatafeed supply OHLCV, not guaranteed executable broker quotes or an order book. Reconcile entry price and quantity with the actual Zerodha fill.

7. Current ML must not score the new setups unchanged

The existing model was trained on the old EMA-cross-selected population. Its probability and thresholds are not validated for pullback-resumption or breakout events.

Build a fresh event table with setup_id/episode_id, all valid candidate families, event start/end, execution fidelity and quantity-aware net labels. Keep overlapping same-symbol event families and quantity variants together in folds.

Use the previous 16-feature proposal plus two one-hot setup indicators for the three families, giving 18 features. If optional sector/VIX features are included, the total is at most 20. Do not use a numeric strategy ID as though its values form an ordered quantity in logistic regression.

Benchmark:
1. Rule-only B/C with identical cost/risk constraints.
2. Pooled regularized logistic model with setup indicators and out-of-time calibration.
3. Small depth-2 XGBoost challenger.

If there is insufficient evidence for a shared model, report setup-level uncertainty and continue shadowing; do not train tiny independent boosters to create apparent precision. Old 0.55/0.50 gates do not transfer automatically.

Evaluate conditional expected net R, not only a binary success rate. Require a positive conservative estimate after fees, slippage and integer-ticket economics. Missing models or schema failures cannot silently activate an unvalidated rule-only strategy.

8. Risk policy for a smaller cash pool

Let C be reconciled deployable cash after pending-order reservations and known liabilities; let E be marked total equity including C. C is currently unknown.

For the initial validated pilot:
- Maximum two new Core positions, in different sectors/correlation clusters.
- Park Satellite money in cash.
- Maximum new ticket=min(25% of C, 3% of E); no minimum ticket or forced allocation.
- Stressed loss allowance per new position<=min(1% of C, 0.15% of E).
- Aggregate stressed loss allowance for all open/pending new positions<=min(2% of C, 0.30% of E).
- At least 20% of C remains uncommitted; the two-position limit may leave substantially more.
- Existing legacy risk is reported separately; it is NOT falsely described as bounded by the new-trade heat cap.
- No new sector exposure above 25% of E, or shared theme above 35% of E; grandfather existing breaches and block additions that worsen them.
- Require aligned return-history coverage; strongly HAL/ZENTEC-correlated candidates are rejected while concentration persists.

If C were ₹12,670 and E approximately ₹1,19,148, the maximum ticket would be roughly ₹3,168, per-trade stressed loss allowance ₹127, and aggregate new-book allowance ₹253. These are ceilings, not recommendations to spend those amounts. Actual quantity falls after accounting for stop distance, fees, adverse gaps and slippage.

Use risk budget including a conservative gap allowance, for example an additional 0.5ATR or a worse reliably estimated adverse-gap tail. A gap allowance is a scenario buffer, not a guarantee.

Fixed fees can make tiny tickets unattractive. If a ₹1k–₹2k feasible position has no positive conservative expected return after costs, skip it. Do not solve that by raising risk or dropping cost assumptions.

Use fixed ceilings until after-cost out-of-sample evidence supports a fractional Kelly overlay. A negative/uncertain edge receives zero live allocation. The figures above supersede the prior provisional cash/risk figures; they must be tested against the updated reconciled book.

9. Weekly signals: measurable goals, not forced transactions

The system can reliably report every week. It cannot guarantee an honest buy and a profitable sell every week.

Distinguish:
- WATCH: valid price setup, but not currently executable/admissible.
- BUY: fresh executable opportunity passing economics, cash, concentration and risk checks.
- SELL: an actual held lot meets its specific exit policy.
- HOLD / NO TRADE: no permitted transaction.

These categories must be explicit; WATCH must not be styled as a BUY, and a stop/expiry reminder must not count as a profitable exit.

Research targets:
- Seek roughly 1–3 independent qualified buy opportunities in a typical favorable week; this is an aspiration to measure, not a forecast.
- Report the fraction of eligible RISK_ON weeks with at least one unique actionable BUY after ALL gates and actual cash replay.
- Explore 70% RISK_ON-week coverage as a design target only if after-cost performance and uncertainty remain acceptable.
- Report all-week coverage too; do not hide RISK_OFF weeks, data failures or cash-blocked weeks.
- Report median/p10/p90 unique signals per week, longest zero-signal run, signal-to-fill conversion, rejected counts and reasons.
- Report SELL frequency separately for legacy and new positions, and realized net profitability.
- Do not count repeated messages, several setup tags for one stock, or quantity variants as separate opportunities.

Two positions held for around 5–10 sessions naturally constrain throughput. Cash turnover and signal supply are different measurements. New trades can have regular stop/target/time exits; legacy shares cannot supply regular sells while below BE.

On Friday's existing 15:05 monitor run include a weekly status digest as of that timestamp: current holdings, cash availability, new setups, blocked opportunities, actual filled exits, net realized P&L and upcoming expiry dates. Friday's candle is still incomplete; do not treat this as final EOD labeling. No additional GitHub Actions schedule or parallel ChatGPT trading automation is required.

10. Implementation and verification handoff

This update requires recovering the deployed source before live integration. No source or production state was changed during this analysis.

Implement in this order:

1. Reconcile updated quantities, exact average costs, existing T1/T2 stage flags, HWM and actual available cash against S3 and broker fills. Do not infer cash from holdings.
2. Fix the audit's existing defects: completed-day selection, regime_on, feature parity, model/NaN failure behavior and cash reservation.
3. Add backtest/universe.py or shared/universe.py for dated official constituent downloads, instrument metadata, liquidity filters and archive manifests.
4. Add shared/setups.py for the three pure completed-bar predicates and episode reset/deduplication. Reuse the exact functions offline and in Lambda.
5. Extend backtest/labeler.py with setup/episode grouping, post-alert fills, costed continuous R, censored outcomes and ten-session economic exits.
6. Extend purged walk-forward evaluation to compare original-only, pullback-only, breakout-only and pooled policies on the same eligible dates, cash and costs. Record every experiment; do not select a frequency target on the final test.
7. Export a newly validated model.json with setup indicators, feature hash, exact intercept, scaler/calibrator, cost/execution policy and economic gates.
8. Add runtime/risk.py and state reservation checks for the updated cash/portfolio limits.
9. Extend lambda_function.py to collect/rank/deduplicate candidates before allocation, rather than consume cash in CSV order.
10. Keep the 09:25 recommender and 15:05 monitor schedules and existing ntfy/Gmail destinations. Send weekly status within Friday's existing monitor.
11. Extend portfolio_cli.py to import confirmed fills, handle partial exits, fees and reservations, and protect legacy classifications.
12. Shadow all new strategies until statistical and operational gates pass; then stage a matching code/model release with rollback.

Required metrics and artifacts:
- unique candidate events and independent filled outcomes by setup;
- signal-density and zero-signal-week report before/after portfolio gates;
- dated universe membership and survivorship-bias assessment;
- post-alert execution fidelity and manual latency/slippage distribution;
- out-of-time calibration/Brier and after-cost expected R with date-block uncertainty;
- equity, drawdown, turnover and tail losses for the new book;
- total portfolio attribution separating legacy market movements from new-trade profits;
- all rejected/unfilled/censored outcomes, not just selected winners.

Acceptance cases specific to this update:
- Pullback can trigger without a fresh EMA cross.
- Breakout can trigger with BB position>0.60; baseline cross still obeys its old BB gate.
- Yesterday's high and the prior 20-day breakout threshold are shifted correctly; current high is excluded from the threshold.
- Multiple families on the same stock generate one reserved proposal.
- A held/pending stock, including INFY, never generates another BUY.
- Updated 16/8/8 legacy quantities and stages cannot be overwritten with the earlier 14/6/6 snapshot.
- Unknown current cash blocks actionable sizing; no fallback to ₹27,560.
- All screenshot holdings remain ineligible for a profit-only sale at their screenshot prices.
- Zero-signal weeks are recorded honestly.
- Same signal/episode cannot become repeated weekly “new” opportunities.
- Model feature order/type/setup encoding matches training exactly.
- A stale or mismatched model cannot score new families with old probabilities.
- Two-position pilot and concentration constraints apply after aggregate candidate ranking.
- Runtime under the real TradingView workload meets the deadline without silently dropping low-priority symbols from breadth/history.

Sources checked 7 October 2026

- [Repository commit](https://github.com/Codingbysid/indian_market_swing_system/commit/4b634f2ba46d00f00bac98d10178b0d59e74ebf9).
- User's attached portfolio screenshot supplies the holdings figures; no current cash balance was supplied.
- [Official Nifty 200](https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-200): broad large/mid-cap universe and constituent download.
- [Official Nifty 500](https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-500): larger eligible research universe; not an instruction to buy all constituents.
- [Zerodha: triggered GTT orders can remain unexecuted](https://support.zerodha.com/category/trading-and-markets/charts-and-orders/gtt/articles/why-did-my-gtt-order-trigger-but-was-not-executed).
- [Zerodha GTT mechanics](https://support.zerodha.com/category/trading-and-markets/charts-and-orders/gtt/articles/what-is-the-good-till-triggered-gtt-feature).

Open factual input: actual current available cash and any existing pending orders/stage fills. This is needed to instantiate quantities, not to complete the research design.

