# Four-screen swing engine: research specification v5
Date: 2026-10-17. Status: DESIGN / UNPROMOTED. Implementation handoff, not a tested profitable strategy.
Base reference: quant-v4-india150 at 34ce678c3033507c9b43d264bf293d1f5907db4e.
This document supersedes earlier strategy/risk design for the rebuild; it does not change live resources.

## 1. Objective and evidence
Develop two independently evaluated holding horizons, using four Screener references plus a liquid NSE comparison pool, under one cash/portfolio allocator. Cash CNC, manual broker execution, no leverage, derivatives or shorts. Maximum 150 unique equities including holdings, reservations and research controls. Indices are reference series, not extra equities.

Activity target: at least two distinct actionable BUY or SELL intents in a rolling 14-calendar-day period. Report its achieved frequency; never force it. HOLD/WATCH, repeat notifications and target touches are not fills or new actionable signals.

Previous v4 evidence is a negative baseline: 69 rules-only round trips, -₹1,294.58 net P&L, PF 0.647, two intents in 47.13% of fortnight windows; no approved Kelly allocation. Reused history remains development data. Rebuilding does not erase that evidence or create an untouched holdout.

## 2. Confirmed book and migration
User confirmed 17 October: cash ₹5,748; no pending orders/GTTs; TIMEX inactive.
HAL 16 @ 5040.63, ZENTEC 10 @ 1669.00, CDSL 8 @ 1363.75, BPCL 6 @ 318.65.
Screenshot marks: HAL 4989, ZENTEC 1655.70, CDSL 1286.90, BPCL 294.
Broker displayed invested amount ₹110,162.00, holdings value ₹108,440.20; plus cash ₹114,188.20.
Rounded averages imply a two-paise cost discrepancy: preserve exact broker cost basis if available; do not manufacture a fill to reconcile rounding.
INFY is absent. Its sale and the extra ZENTEC buys lack fill details. Do not invent prices/dates/P&L or rewrite historical ledgers.
HAL+ZENTEC = ₹96,381 = about 84.4% of total marked assets. No averaging into any existing holding. Existing shares are legacy protected.

The fixture is an onboarding reference, not a direct S3 overwrite payload. Read current S3 + ETag and broker records, produce a diff and reconcile under a conditional write. A quarterly-settlement bank payout is an external transfer; neither a trading loss nor deployable broker cash. Legacy sale proceeds enter new capital only after available broker cash is confirmed. Do not make this old fixture look newly reconciled on future runs.

Legacy BE policy remains avg_cost * 1.0025 + 18/remaining_qty. Approximate values: HAL5054.36, ZENTEC1674.97, CDSL1369.41, BPCL322.45. For a proposed tranche also calculate actual net proceeds versus allocated cost/fees; require BOTH the policy BE floor and positive tranche net P&L, because a small sale has its own fixed DP charge.
Keep saved immutable tranche plans and filled stages. HAL5/5/6, CDSL3/3/2, BPCL2/2/2 only if no prior stage fills; never reset a stage. ZENTEC changed quantity: reconstruct from fill history. If unavailable, show PLAN_RECONCILIATION_REQUIRED; 3/3/4 may be offered as an explicit new remaining-position plan, not silently substituted.
T1=max(BE+ATR, nearest resistance above BE), T2=max(avg+3ATR,T1+one valid tick), runner trail only after observed price>T1, never below BE. Check prior trail before raising watermark; weakness exit only above BE. Compute ATR from completed bars. Unknown resistance falls back to BE+ATR. For reproducibility propose resistance from confirmed two-left/two-right pivot highs over 126 completed sessions, nearest above BE; exclude unconfirmed pivots.
Profit-only protection may retain losing positions indefinitely. A new-trade risk budget does not limit legacy losses.

## 3. Universe construction: at most 150
Read the actual queries, not just descriptions:
- Piotroski: score>7 (8 or 9), not exclusively 9.
- Magic Formula reference: ROIC>25%, earnings yield>15%, positive book value, market cap>₹15 crore. This is a threshold screen, not the original rank-and-hold portfolio.
- Coffee Can reference: sales growth>10%, ten-year sales growth>10%, ROE>15%, ten-year average ROCE>15%, market cap>₹1,000 crore. Aggregate growth/average returns do not prove every individual year passed.
- Golden Crossover: today's SMA50>SMA200 and yesterday's SMA50<SMA200. Recalculate locally from completed adjusted bars.

Monthly universe freeze; daily screen/status observations do not silently change its constituents. Reserve slots for all held/pending equities first. Of remaining slots, allocate up to 100 to the deduplicated screen union, then fill remaining capacity from the contemporaneous Nifty 200 liquidity baseline (excluding duplicates). For screen slots, round-robin the four lists ordered by median traded value, cap any one sector at 20% of total slots, deterministic symbol tie-break. Reallocate unused slots. Retain membership flags and source cohorts. If adequate candidates are unavailable, fewer than 150 is correct. Do not backfill with illiquid names.
Open/pending lots retain slots until closed/cancelled even if they leave screens. Replacing a candidate must not create a 151st unique research ticker in a frozen run. Research both horizons on this SAME universe; combined shadow/research/live pool <=150.

New-entry eligibility: NSE main-board ordinary EQ shares, >=300 completed sessions, positive verified prices, >=95% of expected sessions available over 60 sessions, 60-session median daily traded value >=₹10 crore and 20-session median >=₹5 crore. Minimum liquidity is a fixed research policy, not an optimized edge. Exclude SME, suspended, trade-to-trade, unresolved splits/bonuses, exchange restrictions or unverifiable instrument status. ASM/GSM/price-band status requires dated official metadata; missing restriction data blocks new admission. No selection based on share price alone.
Affordable quantity and costs are checked at admission, not used to backselect historical winners.
Screen data may include BSE-only stocks; retain exclusion reason, do not fabricate NSE symbols.
ROIC/earnings-yield comparability is poor for banks/NBFCs: mark these factors not applicable and exclude financials from the value hypothesis. They may participate in price-only controls.

## 4. Free-data architecture
Screener states it has no API. Use public permitted pages with bounded fetching/pagination, or user-provided dated snapshots/CSV when available. Do not require a paid export plan, bypass login/CAPTCHA, or infer historical screen memberships from today's results. Store query text, retrieval time, report period, complete/incomplete status and source URL. Empty results differ from fetch failure. Never accept page 1 as a complete 23-page screen.
Fundamentals: actual public filing availability is the effective date. If a filing timestamp/restatement history cannot be reconstructed, treat fundamentals as forward-only. A fixed 45/90-day lag alone does not establish point-in-time correctness.
Official NSE daily UDiFF bhavcopy and security-wise delivery files are free downloadable report candidates, not an assumed stable authenticated API. Fetch one bulk file per session, validate date/schema/hash, join by stable instrument/ISIN and EQ series. Implement effective-dated legacy/UDiFF parsers. Obtain index history and holiday/corporate-action metadata from official publications where accessible.
TradingView stays the existing live quote candidate; verify from AWS with bounded concurrency. Unofficial tvDatafeed availability is not guaranteed. Price history fallback can be research-only with vendor tags; do not silently splice it into production features. A source change requires a full overlap/parity report.
Delivery ratio is optional, unavailable is not zero. Its incremental model must train and infer with the same availability contract.
Archive raw immutable files plus normalized bars; keep raw execution prices separate from corporate-action-adjusted feature/return series. No backward use of future fundamental revisions. Dividends enter economic returns on correct entitlement/payment assumptions.
Public NSE announcements and company investor-relations filings support event flags. Google News may supplement held-stock context; no paid news/LLM API is required. No existing private sentiment backend is assumed.
Each provider health record: endpoint, requested/session dates, status, rows, schema version, elapsed time, retries, coverage, last-good age. Backoff and cache within a strict budget. Don't label a source working based on a homepage HTTP200.

## 5. Three registered hypotheses across two horizons
Thresholds below are initial pre-registered experiments, NOT empirically optimal values. No new family becomes BUY until validation gates pass. Indicators use split-adjusted completed sessions; any t signal first executes after t.

Q: quality continuation (3–15 sessions). Eligible Piotroski or Coffee Can members; close>SMA50>SMA200; SMA50 rising versus five sessions earlier; stock 63-session return minus Nifty return positive. Pullback condition: at least one of the last three completed lows <= its EMA20, with no close below SMA50 during those sessions. Trigger: close_t>high_(t-1), close_t>EMA20_t, volume_t>=median(volume_(t-20:t-1)). Reject close_t more than 1ATR above EMA20. No RSI prerequisite.

T: trend transition (2–8 weeks). A locally confirmed fresh golden cross in preceding ten sessions; SMA200_t>=SMA200_(t-20); positive 63-session index-relative return. Trigger close_t>maximum high of prior 20 sessions and volume_t>=1.2*prior20 median. Quality membership is a tagged interaction to test, not mandatory. Keep a separate same-price-rule non-screen control.

V: improving value (2–8 weeks). Magic Formula member, nonfinancial, Piotroski>=6 only when that score is independently available as of signal (membership alone implies>=8 but nonmembership is not a score). Positive EBIT and equity, no unverified one-off earnings assumption. Close>SMA50; 63-session index-relative return positive. Trigger close_t>prior20-session high and close_t<=1.5ATR above SMA50. Require actual eligible snapshot, do not hallucinate fundamentals. If score unavailable, no V event.

Candidate dedup: symbol+signal_session. If multiple families fire, record all tags, assign owner by highest training-only conservative expected net return per reserved rupee-session; before such estimates exist use frozen Q,T,V priority in research. Never open multiple lots to average the same stock or split it across horizons.

Primary exit experiments:
- Short: stop distance=max(1.5ATR14, entry-prior5-session-low+0.1ATR), reject if >2.5ATR. Full target at entry+2*initial_distance; full remaining exit at close of session15 if neither barrier hit. No minimum holding lock prevents stops.
- Long: distance=max(2ATR14, entry-prior10-session-low+0.1ATR), reject if >3ATR. Full target at entry+3*distance; vertical exit session40. One pre-registered challenger replaces target with a 3ATR close-based trailing stop activated after a completed close>=entry+2R, effective next session. Stop never loosens.
- Cross-horizon ablation: run Q with long exits and T/V with short exits. Label these secondary experiments, include all tried specifications in multiple-testing accounting.
No tiny partial exits on new trades by default. Legacy tranches remain separate. Use whole-position exits to avoid repeatedly charging fixed sale costs.

## 6. Regime experiment
Baseline: Nifty completed close>EMA50, otherwise no new BUY. Missing/stale index is DATA_INVALID.
Challenger: baseline plus breadth>=50% above SMA50 and index realized-vol20/vol63<=1.5. Breadth is computed on the fixed available baseline cohort; coverage>=90% and index session alignment required.
Test the baseline, challenger and no-regime diagnostic with identical candidates/costs. Previously a stricter regime made mean returns worse: do not assume extra gates add alpha. Freeze deployment regime from validation, with no automatic optimizer.
RISK_OFF: HOLD/EXIT management continues; no dip-buy exception or new mean-reversion sleeve at launch. India VIX may be a logged diagnostic if official history aligns; USDINR adds no mandatory live dependency.

## 7. Features and model hierarchy
18 core fields: ATR/close; EMA20 distance/ATR; SMA50 five-session slope/ATR; drawdown from63-high; 20-session return;63-session Nifty-relative return;126-session Nifty-relative return;60-session beta;prior20 relative volume;log60-median turnover;63-session downside-gap95/ATR;20/63 volatility ratio;Nifty distance EMA50/ATR;breadth50;screen flags Piotroski/Magic/Coffee;recent-golden-cross flag.
Core numeric feature schema exactly these18, finite values, units/order hash frozen. Setup and horizon are model routing metadata, not undisclosed extra columns. Optional delivery surprise and sector-relative return expand a separately versioned schema to20 only after full coverage/availability testing. Missing optional data cannot enter the18-field model by accidental zero imputation.
Piotroski/quality/value continuous fundamentals are an explicitly separate forward-only experiment until reliable point-in-time history exists. Membership flags likewise require historical snapshots; price-only historical models OMIT them under a different explicit schema.

Compare (1) frozen rules, (2) regularized logistic meta-label predicting net profitable outcome, (3) shallow XGB only with adequate sample. Fit each horizon separately; share a model across family tags only if sample too thin and expose that limitation. Start fixed logistic C=0.1; XGB depth2,160trees,lr.03,min_child_weight50,lambda20,alpha1,subsample/colsample.8. No large grid search.
Do not trade on p>=0.55 alone. Estimate conditional net-win/net-loss magnitudes from purged training outcomes, shrink toward horizon-level estimates. Admit only positive conservative expected net P&L after actual sized fees/slippage AND portfolio constraints. Compare this mapping against rules-only; model is optional if rules prove stronger.
Platt fits logits; isotonic allowed only with >=500 independent calibration observations and a separate calibration-selection block. Prior baseline included. Export only the exact selected model and its own calibrator. JSON supports logistic or XGB without native ML dependencies in Lambda.

## 8. Labels, fills and costs
Record signal, intended order, admissible price band, actual/hypothetical fill, exit, censoring and net P&L. Unfilled/rejected/expired orders never become losing labels. Positive label is net P&L>0 at actual exit; retain continuous net return, R, adverse/favourable excursion and duration.
First fill next session; no signal-close fill. Research next-open is only a proxy for09:25 alerts. Additional daily conservative band-touch replay requires open in band or subsequent daily range intersection; same-session ambiguity must be adverse, not optimistically infer intraday path. Promotion requires observed09:25 quotes/fills or appropriate intraday evidence.
Entry band reference signal close to signal close+0.25ATR for upward triggers; a downward gap is not automatically a valid breakout entry. A marketable limit is bounded by alert max price. No chase above band. Valid09:25–09:45 IST, expire afterwards. If prices gap below planned protective stop before entry, no fill. Refresh/recompute risk at actual entry. Never allow requested limit to increase risk beyond reservation.
Existing positions: gap through stop exits at executable open less sell slippage, potentially worse than1R. Same-bar target+stop =>stop first. Vertical barrier counted by exchange sessions from fill (entry=session1), not weekdays. Pending halts/circuit blocks defer execution; do not fabricate a stop fill without tradability evidence.
Include STT, exchange/SEBI/IPFT, GST, stamp and DP using effective-dated tariffs. Current reference: delivery brokerage0, STT0.1%each side, NSE txn0.00307%, stamp0.015%buy, SEBI₹10/crore, GST18%applicable components, DP₹15.34 per scrip sale debit basis. Verify broker-day aggregation/rounding against contract notes and ledger. Use historical rates where known; otherwise tag conservative present-cost approximation.
Base slippage10bps each side; stress25bps and50bps. These are assumptions, not measured fill quality. Fixed DP matters for tiny tickets. Target payoff must clear costs; don't loosen a stop to improve a model score.

## 9. Capital and risk policy (proposed conservative starting values)
Initial new sleeve NAV N=₹5748; includes assigned broker cash plus market value of system lots, less estimated liquidation fees. Legacy lots outside sleeve. Shared cash ledger; no 50/50 allocation and no obsolete satellite split. No leverage.
Reserve >=20% of N in uncommitted cash. Per-position debit cap=min(40%N, remaining cash after reserve/reservations), including buy fees. Maximum2 new/pending positions; at launch cap₹2299.20 and reserve₹1149.60. Two full caps plus reserve exactly exhaust N, so fees must fit inside caps.
Per-trade stressed-loss cap=min(₹100,1.5%N,remaining_loss_headroom/10)=₹86.22 initially.
Aggregate open+reserved stressed-loss cap=min(₹200,3%N,20%remaining_loss_headroom)=₹172.44 initially.
Integer q is largest satisfying all debit and risk constraints. Stressed loss=q*(entry-stop+gap_buffer)+roundtripfees+slippagestress. Gap buffer=max(0.5ATR,price*lagged95th-percentile downside overnight gap over126sessions). No observed gap history =>noBUY. Estimate all costs at that q; iterate integers, do not divide by stop distance and add fees afterwards. Reject when costs+base slippage >25% of q*(entry-stop); this may leave no affordable trades and must be reported.
Do not hard-code a minimum ticket forcing risk. Recalculate after actual fills. Out-of-band manual fills remain broker truth and raise risk breach; never delete them to make risk look compliant.
Legacy concentration: no new defence or PSU until relevant exposures below25% and35% of whole-book equity respectively. Sector cap25%; max60-session pair correlation .65 versus all holdings and open/reserved names, require55 aligned returns. Missing sector/theme/correlation blocks BUY. Maximum one new position per sector. Existing breaches don't cause forced legacy loss sales.

User loss limit: new-sleeve PnL P=N-N0-net_external_inflows (net of fees and estimated liquidation costs). Do not count legacy unrealized P&L. Deposits/withdrawals and settled legacy proceeds are flows, not alpha; no loss-counter reset.
If P<=-1500: latch NEW_BUYS_PAUSED, cancel recommendation reservations (request user cancellation of actual broker orders), retain all system stop/time exits and legacy profit-only management. No automatic restart on a deposit or mark rebound; require reviewed explicit resume.
Admission headroom=max(0,1500+P). Existing-risk measurement should be remaining stressed loss from current mark to adverse liquidation, so marked losses are not double-counted against headroom; report original entry risk separately.
This is a cumulative net-loss boundary from launch, not a trailing high-water drawdown guarantee. Also report peak-to-trough drawdown without inventing a second user-authorized forced-sale rule.
₹1500 is26.1% of launch cash. It is a circuit breaker, never a per-trade budget. Gap/illiquidity risks can exceed caps.

Kelly: no initial Kelly sizing. Fixed risk is a policy for a strategy that has independently qualified, not permission to deploy unproven rules. Later estimate generalized log-optimal fraction on costed return paths with week/block bootstrap, shrinkage and portfolio replay. Enable at most quarter Kelly only with positive one-sided95%lower expected-return bound, >=200 qualifying OOS portfolio fills,>=50wins and50losses,>=20names,>=3folds. Apply as an additional ceiling; never override cash/concentration/₹1500 limits. Report binary Kelly only as a diagnostic using observed payoffs, not nominal target ratios.

## 10. Honest validation
Record a protocol hash, all tested hypotheses and data freeze before evaluation. Existing v3/v4 history is development; reserve later untouched dates for true forward evidence.
Price-only retrospective baseline can use archived prices and contemporaneous membership if obtained. If constituents/delistings are unavailable, label current-universe bias. Four-screen fundamental historical results require contemporaneous snapshots/filing reconstruction; otherwise forward-only, not backfilled with today's membership.
Expanding training36months, calibration6months (split fit/selection), outer test6months where data supports. Purge any sample whose execution-to-exit interval overlaps a boundary; use embargo at least max40-session label horizon for cross-horizon combined tests. Train-only preprocessing and model choice. Cluster uncertainty by calendar blocks; overlapping same-stock events aren't independent. Compare to cash, matched rules without screens, liquid-pool same rules and horizon-matched index exposure.
Report: admitted unique fills, symbols, family/horizon/cohort, turnover, net expectancy/PF, win/loss magnitudes, marked drawdown, exposure-days, cost/slippage sensitivity, Brier/logloss/calibration, rejection funnel, 14-day activity/zero-signal windows, tail gap losses. Deduplicate combined horizons; report overlapping-window statistics descriptively.
Promotion requires data/feature/scorer parity, positive lower after-cost expectancy bound and positive result under25bps-per-side stress, no risk-policy breaches, at least3purged folds/200OOS portfolio fills/50wins/50losses/20names, and at least60sessions of untouched forward shadow observation with measured execution/rejection logs. These are necessary, not sufficient; failure means WATCH. Do not relax gates to meet a date or quota.
Assess performance on the actual ₹5748 constraints as well as a scalable reference portfolio. An attractive unconstrained backtest can be untradeable here. Count promotion per deployable strategy/horizon rather than pooling weak families to hit200.
Sentiment ablation only after timestamped archive exists. No synthetic past headlines or LLM confidence as calibrated return probability.

## 11. Sentiment and outputs
Start with deterministic event/risk flags: earnings dates, auditor/resignation/default/regulatory material events, corporate actions. Each event needs source URL, publication time, first_seen, symbol resolution, expiry, reliability and review status. Missing feed=unknown, not good news. News alone never removes legacy BE protection.
Flag earnings from previous session through next session after announcement as no-new-entry; if schedule/announcement coverage unknown, require manual event check before actionable BUY. Don't classify every word 'loss' as adverse company news without context.
Google headlines are supplementary and noisy; duplicate syndicated articles count once. Positive tone cannot increase quantity. Backend adapter optional; credentials via environment/Secrets Manager, no paid LLM assumed.

Per-stock output: BUY, SELL, HOLD, WATCH, or DATA_INVALID with reason codes. BUY includes strategy/horizon, completed-bar date, entry band/expiry, quantity/max debit, stop, exit rule, net expected economics only when calibrated, stressed ₹risk, reservation ID. SELL distinguishes system stop/target/time from legacy tranche. HOLD means position retained under its policy, not predicted price appreciation.
Daily summary may show no trade and a short rejection funnel. Two-week review shows actual unique actionable intents and fills separately. Do not send repeated BUYs to improve activity.
Do not claim stop protection is automatic: user must place/confirm their broker stop/GTT; record protective_order_status. The15:05 monitor alone cannot protect against morning/intraday gaps.

## 12. Runtime and handoff boundary
Nightly/after-close ingestion and features cache in S3; scan reads prepared150-name snapshot plus bounded current quotes. Existing09:25 recommender/15:05 monitor retained; propose a separate after-close ingest schedule in deployment plan, don't silently change EventBridge.
No150-stock full-history download inside each notification invocation. Budget workers/deadlines; Lambda remains JSON scorer with numpy/pandas already layered, no sklearn/XGB installs. Research trains off Lambda. Publish immutable schema/model/config/data hashes and provenance. Model export contains selected calibrator and training/validity dates.
Separate dry-run/paper namespaces. No notifications or execution/state writes in pure dry run. Conditional S3 book writes, idempotent fill IDs, signal expiry, concurrency tests, complete marks only. Missing model must block model-dependent strategies; an independently promoted explicit rules strategy can run without ML, never a silent fallback.
Prepare migration, shadow execution and rollback before deployment. Candidate-only until user approves an exact deployable result. Keep v4 code as reference; no importing its candidate model as qualified v5.

## 13. Sources verified for design
https://www.screener.in/screens/2/piotroski-scan/
https://www.screener.in/screens/59/magic-formula/
https://www.screener.in/screens/57601/coffee-can-portfolio/
https://www.screener.in/screens/336509/golden-crossover/
https://support.screener.in/article/28-export-screen-results
https://www.nseindia.com/all-reports
https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives
https://zerodha.com/charges/
These source pages were reviewed; this does not certify successful automated ingestion from AWS. Provider smoke tests and measured latency remain implementation acceptance work.
