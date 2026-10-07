Indian equity swing system — repository audit and Cursor implementation specification
Prepared 7 October 2026

A. Repository audit

Audit boundary and conclusion

I read every nonempty tracked file in Codingbysid/indian_market_swing_system, its complete recursive tree, the available branches, and recent commit history. The only branch exposed was main, at 4b634f2ba46d00f00bac98d10178b0d59e74ebf9, dated 17 August 2026. The live behavior described in the request is materially newer than this repository.

This is a source-code audit and a proposed research specification. It is not an audit of the deployed Lambda package, current S3 objects, EventBridge configuration, actual broker fills, or current market prices. I did not invoke Lambda, send alerts, edit the GitHub repository, or deploy anything.

The most urgent upgrade is to make research, deployed code, model metadata, actual executions and portfolio state describe the same strategy. Increasing model complexity before that would make the results harder to trust.

Evidence:
- [Audited commit](https://github.com/Codingbysid/indian_market_swing_system/commit/4b634f2ba46d00f00bac98d10178b0d59e74ebf9)
- [Complete tracked tree](https://github.com/Codingbysid/indian_market_swing_system/tree/4b634f2ba46d00f00bac98d10178b0d59e74ebf9)
- [Lambda](https://github.com/Codingbysid/indian_market_swing_system/blob/4b634f2ba46d00f00bac98d10178b0d59e74ebf9/lambda_function.py)
- [Features](https://github.com/Codingbysid/indian_market_swing_system/blob/4b634f2ba46d00f00bac98d10178b0d59e74ebf9/backtest/features.py)
- [Labeler](https://github.com/Codingbysid/indian_market_swing_system/blob/4b634f2ba46d00f00bac98d10178b0d59e74ebf9/backtest/labeler.py)
- [Trainer](https://github.com/Codingbysid/indian_market_swing_system/blob/4b634f2ba46d00f00bac98d10178b0d59e74ebf9/backtest/train.py)

What the checked-in code actually does

| Area | Verified repository behavior | Difference from the supplied live description |
|---|---|---|
| Entry | Fresh EMA9/21 cross, RSI 40–65 inclusive, Bollinger position below 0.60 | Matches the stated entry predicate |
| Regime | Fetches NSE:NIFTY; below its EMA50 returns before the ticker loop | No half-size dip mode in this commit |
| Failed index fetch | Defaults to RISK_ON | Matches the stated fail-open weakness |
| Features | Seven listed features, computed from the final returned row | No completed-session selection or timestamp freshness check |
| Regime feature | score_setup(..., regime_on=1.0) at Lambda line 624 | Hard-coded; dormant for valid RISK_OFF in this commit because that path returns early; becomes an active bug in a dip-mode version |
| Feature columns | SMA_50 created at line 584; Vol_MA20 at line 592 | Neither is missing from this normal scanner path |
| ML gate | One MIN_SIGNAL_PROB, default 0.55; missing model or scoring exception bypasses it | No separate Satellite 0.50 gate |
| Capital | TOTAL_CAPITAL environment variable, default ₹60,000 | No cash-driven S3 portfolio sizing |
| Allocation | Every signal independently receives the same risk/capital calculation | No aggregate cash reservation, portfolio exclusion, max-position count or sleeve accounting |
| Universe | Five hard-coded Screener S3 keys | No satellite_high_beta.csv or it_bluechips.csv in the live scanner |
| Actions | scraper and recommender only | A holdings-monitor event would return an unknown-action response |
| Holdings | No portfolio loader, legacy exits or fill ledger | Cannot verify the supplied T1/T2/T3 policy from this source |
| GitHub Actions | No cron; manual confirmation path deliberately exits with failure | AWS-only scheduling intent is preserved |

Missing from the tracked tree: portfolio_cli.py, data/portfolio_state.json, both named supplemental CSVs, all Screener CSVs, historical prices, events.csv, metrics.json and model.json. The CSVs and backtest artifacts are explicitly ignored. Absence from GitHub is not evidence that they are absent in S3.

The reported approximately 21% overall TP rate / 36% resolved win rate cannot be independently recomputed. Code comments mention the 36% figure, but there is no auditable result artifact. Do not treat this report as empirical confirmation of those numbers.

Severity-ordered findings

1. Critical: code/deployment drift and no recoverable live specification.

The described cash book and holdings policy are absent. First capture the actual deployed package, dependency-layer versions, Lambda CodeSha256, model S3 key/version/hash, portfolio state version and EventBridge action payloads. Read only necessary AWS configuration fields; do not put credentials or environment secrets in reports. Reconcile that snapshot into a research branch before any source-based redeployment. Keep S3 as authoritative for the live book.

2. Critical: fail-open behavior turns a model outage into extra exposure.

Lambda lines 318–365 catch extraction/scoring errors and return None; lines 624–630 then omit the probability gate. Model loading and index loading also fail open. Missing SMA_50 and Vol_MA20 each reproduce this behavior, even though both columns exist in the audited normal path. NaN predictions are not rejected: NaN < 0.55 is false.

Require a valid model contract, finite required inputs, a finite probability in [0,1], fresh required data and valid state before a new BUY can be actionable. Return structured error codes and an operational alert. Holdings monitoring must remain operational independently. An explicitly approved, separately validated rule-only champion is a versioned strategy, not an exception handler.

3. Critical: signal information and executable entry are misaligned.

At 09:25 IST the current daily candle is incomplete. The code unconditionally uses iloc[-1], including its partial-day volume and index close. Offline data uses completed daily candles and enters at that day's final Close, which was not knowable at 09:25. This is a research/live information mismatch; ordinary trailing EMA/RSI calculations themselves are not forward-looking.

The same problem applies to treating a 15:05 candle as a completed daily weakness cross. Normal NSE cash trading closes at 15:30. Separate completed-session indicators from a timestamped intraday execution/monitor price.

4. High: label censoring, fills and costs are wrong.

Labeler lines 57–75 enter at signal Close; ignore opening gaps when filling a stop/target; assign exact -1R/+2R; charge no costs. Lines 113–116 claim to skip incomplete horizons but execute pass and retain the event. A signal with only one future bar is treated as a completed timeout/loss.

The labeler already records timeout mark-to-market R. Keep that information. The problem is throwing every timeout into class 0 and then reporting a fixed payoff of 2.0 among resolved trades. That payoff is hard-coded, not measured. No event end timestamp or path diagnostics are saved.

5. High: validation is one row split, with overlap and no economic selection test.

train.py lines 30–34 sort rows by date and cut at 75%. A date with several symbols can appear on both sides. Labels of training events can use outcomes from the test interval. There is no purging, embargo, rolling re-estimation, separate calibration sample, untouched final period, or portfolio cash replay.

Brier and calibration buckets are calculated, but only buckets are printed and no calibrator is fitted. Precision/F1 at 0.55 do not answer whether executable after-cost returns improve.

6. High: the exported model is not guaranteed to reproduce the native booster.

train.py line 114 writes base_score=0.5 without setting that parameter during fitting or reading the fitted value. XGBoost has supported automatic intercept estimation since 2.0. Balanced class weights may happen to mask a discrepancy in some versions/data; a mismatch is not proved without the missing native model. It is nevertheless an invalid export contract.

Read the fitted configuration and export the actual raw base margin; verify it against native predictions. Keep float32 split comparisons. The legacy array walker returns base_weights and does not implement the same float32 comparisons; remove it from the supported production contract unless separately proven against its exact model format. Do not assume a comment about JSON precision proves the cause of historical mismatch.

The nested logistic model in the export is not an automatic fallback: score_setup only uses logistic when the top-level type is logistic. threshold_default in the payload is also not the live threshold source.

7. High: current probabilities are not calibrated economic probabilities.

scale_pos_weight=neg/pos changes the fitting objective and can distort probability levels. No out-of-time calibration corrects this. The predicted event is TP within the window, not profit after costs. A 0.55 threshold has no demonstrated economic interpretation. The logistic comparator is fitted without feature scaling and is neither evaluated as a challenger nor used as the advertised fallback.

8. High: the Kelly implementation forces risk even without an edge.

For p=0.35 and b=2, conventional full Kelly is 2.5% and half Kelly is 1.25%. The code sets max(0.05, full_kelly/2), therefore uses a 5% capital cap. A p=0.20 negative-edge case still receives that 5% cap. With the default ₹60,000, a ₹100 stock and ATR ₹2, both cases allocate ₹3,000.

The formula's fraction is a fraction of bankroll exposed to the modeled loss, not automatically the fraction of cash invested in stock. Mapping it directly to notional ignores stop distance. There are no covariance or existing-exposure controls.

9. High: data selection and availability bias.

fetch_history.py applies today's locally available Screener CSVs across approximately 750 historical bars. Historical screen membership is not reconstructed. Current fundamentals or a current RSI-oversold screen can select names using information unavailable at the historical trade date. Current survivors, missing tickers, mapping failures and first-page-only scraping add selection bias.

The offline fetcher reads all CSVs, while Lambda reads five fixed keys. A model can therefore be trained on a different universe than the live scanner. Cached stock files over 100 bytes are never refreshed; the index is fetched only if its file does not exist. There is no complete-bar, duplicate-date, exchange-session, adjustment or stale-cache validation.

10. Medium/high: live/offline indicator parity is not exact.

Offline RSI leaves the first delta as NaN and replaces zero losses with NaN. Live RSI seeds with zero and permits RSI=100 on an all-rising sequence. Both use adjust=False, so initialization/history length matters. Offline starts signals at index 55; live accepts just 20 bars, synthesizing zero distance when SMA50 is NaN. Live fetches 100 bars; offline has 750. Index EMA50 warm-up differs too. Bollinger zero-width feature defaults differ, although the entry gate normally rejects that situation.

Use one implementation with an explicit warm-up/initialization contract. Merely copying FEATURE_NAMES is insufficient.

11. Medium: operational defects and maintenance debt.

- No handler-level exchange calendar or duplicate-event gate.
- Repeated signals do not reserve cash, so multiple actionable alerts can overcommit the same money.
- S3 append_log_to_s3 is unguarded read-modify-write; concurrent invocations can lose log chunks.
- The ticker catch-all obscures calculation errors as data failures.
- Screener requests have no timeout/status validation and no pagination.
- fetch_history imports lambda_function for SYMBOL_MAP before dotenv loading, triggering boto3 client construction and unnecessary live dependencies.
- requirements use unbounded lower versions and an unpinned tvDatafeed Git URL.
- invoke_aws reports invocation transport status but does not fail the CLI on Lambda FunctionError or an application statusCode >=400; it has no dry-run/notification suppression.
- recommender.py is a separate yfinance implementation with stale strategy duplication. Disabled Actions do not make that script an authoritative live reference.

Synthetic audit verification performed

These tests executed selected existing pure functions using local pandas/NumPy. They did not call AWS, broker APIs, TradingView or notification services.

| Case | Observed result |
|---|---|
| Entry ₹100, ATR ₹2, available final Close ₹103 with no barrier hit | label=0, timeout, R=+1 |
| Entry ₹100, ATR ₹2, following Open ₹90, High ₹92, Low ₹89 | stop, R=-1 instead of modeling the gap loss |
| Subsequent bar touches both barriers | stop wins, as intended |
| Only one subsequent bar available in process_file | Incomplete timeout retained |
| p=0.35, b=2 | Actual cap 5%, not theoretical half-Kelly 1.25% |
| p=0.20, b=2 | Actual cap still 5% |
| Missing SMA_50 or Vol_MA20 | score None; gate bypass path |
| Logistic score with NaN required feature | score NaN; existing threshold condition does not reject it |
| Seven events on one date, one on the next | First date shared between train and test |
| All-rising 100-value Close series | Offline RSI NaN; live RSI 100 |

No full backtest, fitted-model parity test, empirical Sharpe estimate or AWS integration test was possible from the available artifacts.

Keep / replace / retire

Keep the entry rule as the frozen baseline and initial live candidate generator, ATR price barriers, conservative ambiguous-bar convention, TradingView adapter, bounded pure-Python dump scoring, AWS-only orchestration, notifications, and explicit cash/book constraints.

Replace labeling, model validation, outcome probability semantics, sizing and state accounting. Share indicator implementations and symbol resolution.

Retire the live Kelly floor, silent rule fallbacks, unsupported legacy model formats, stale duplicate recommender path and fixed capital default from production admission logic. Preserve old files/results in version history for reproducibility.

B. Quant redesign

The v2 research question

Among stocks that satisfy the SAME completed-day EMA/RSI/Bollinger entry rule and are actually affordable and liquid, does an additional model improve after-cost, execution-aware returns compared with:
1. every eligible rule signal with the same risk controls;
2. a simple Nifty/liquidity filter;
3. a regularized logistic model?

XGBoost earns promotion only if it beats those baselines out of time. A rare candidate set may not support a useful ML model yet.

Time and execution contract

At the 09:25 invocation, d is the most recent COMPLETED exchange session, not simply yesterday and not unconditionally iloc[-2]. Select rows by exchange-local session and data availability time. Use all stock/index/context features through d. Require a documented warm-up, preferably persisted EMA states or a shared fixed 300-bar calculation window used identically offline and live. Longer history used for research must not silently seed live indicators differently.

The intended buy is after an alert can actually be received and acted on. Record scan completion, alert emission, quote timestamp and fill time. For research, use the first tradable one-minute bar after emission plus an explicitly assumed manual latency, then adverse slippage. Start with a two-minute latency scenario; stress five and ten minutes, and replace assumptions using the execution journal.

Provisional execution controls, frozen before the validation run:
- No actionable entry after 09:45.
- Alert expires five minutes after emission or at 09:45, whichever comes first.
- No entry if the current executable reference price differs from d Close by more than 0.5 ATR_d; this catches both gap chases and sharp gap-down entries. This threshold is a conservative research choice, not a measured optimum.
- Send a bounded maximum buy price; reject if the current quote/fill breaches it. Recompute quantity downward for the actual price.
- Use decision-time quote/fill assumptions; never use the future daily high/low to decide whether a 09:25 limit order could fill.

Daily next-open plus slippage is an acceptable diagnostic baseline, NOT an executable backtest of this 09:25 manual-alert system. Daily OHLC cannot tell whether an entry-day high/low occurred before the alert. Keep a dataset execution_fidelity flag; daily-proxy results cannot satisfy the final deployment gate. Capture available minute data prospectively and backfill only what the feed really supplies. This is execution measurement for swing trades, not a new intraday strategy.

tvDatafeed documents up to 5,000 bars per request/timeframe. That can support much more daily history than 750, but only limited intraday history. Five thousand five-minute bars cover roughly 67 normal NSE sessions; one-minute bars cover roughly 13. Availability is not guaranteed. Do not claim a multi-year 09:25 replay from daily candles.

Labels: economic triple barrier

For a filled NEW system position:
- E = actual or simulated executable fill price.
- A = ATR14 from completed session d, frozen for this event.
- S = E - 1.5A; T = E + 3A; one price-risk R = 1.5A.
- Signal-time entry filters stay unchanged.
- Baseline maximum holding period = 10 exchange sessions, counting the entry session as session 1.
- The vertical exit is the first executable price after the day-10 15:05 monitor alert and the same manual latency contract. Do not label at the closing auction if execution is scheduled at 15:05.
- Test 5 and 15 sessions only as two predeclared alternatives in inner validation; no wide barrier/horizon search.
- If the user does not implement a vertical exit for NEW trades, do not train a model that assumes one.

Replay barriers in chronological order after the fill. An opening gap below S fills at the first obtainable lower price with adverse slippage, not at S. A gap above T receives no optimistic improvement beyond a conservatively executable target fill. When the first relevant bar genuinely has ambiguous SL/TP ordering, SL wins. If the opening print itself establishes an exit before later intrabar movement, use that known order. Stops may lose more than 1R, especially in circuits, suspensions or unfilled stop-limit conditions.

A truncated event is right-censored/pending. It is not a loss and is excluded from supervised training until mature. Keep already-observed real exits even near a data boundary, with complete evidence for that exit. Suspended/untradeable positions remain open and marked/stressed until an executable exit is available; do not synthesize a fill because a barrier was touched.

For quantity q:

    gross_pnl = q * (exit_fill - entry_fill)
    net_pnl = gross_pnl - all_incremental_buy_and_sell_charges
    net_R = net_pnl / (q * 1.5 * ATR_d)
    y_net = 1 if net_R > 0 else 0

Entry/exit fill prices already include modeled slippage. Do not subtract slippage a second time. Dividends received while held are separately booked cash flows; price adjustment and cash dividends must not double count one another.

Save barrier outcome and economics separately: target / stop / vertical / censored / unfilled, positive-profit label, net_R, gross_R, MAE_R, MFE_R, duration, entry/exit timestamps, costs, fill method and data-quality flags. An after-cost +1R timeout is a positive example; an after-cost loss at the vertical barrier is negative. Exactly zero is class 0.

Quantity-dependent fixed fees must not disappear

Fixed DP fees matter at ₹2k–₹5k tickets. Use a small declared set of affordable research tickets, initially ₹2,250 and ₹4,500, to construct quantity-specific economic labels from the SAME market path. Skip variants with zero shares. A pre-trade cost_R feature estimates round-trip charges at an unchanged decision price divided by the proposed stop-risk amount; it never uses the realized exit. The actual label uses actual simulated exit economics.

All quantity variants of an event stay in the same fold and receive weights summing to one. They do not count as additional independent market events. Report unique event counts prominently. Live scoring uses the actual proposed q; if cash/risk rounding changes q, recompute cost_R and rescore. Extrapolation outside the validated ticket/cost range is not permitted without revalidation.

Start with a dated cost configuration based on broker charges and reconcile contract notes PLUS the funds ledger. Zerodha currently lists zero standard retail delivery brokerage, STT on both sides, exchange charges, GST, SEBI charges, stamp duty on purchases and DP charges when selling. Its published common DP amount is ₹15.34, generally per stock per day, not per individual share or automatically per order. BSE groups can have different transaction rates. The legacy ₹18 allowance remains a conservative policy floor; it is not a substitute for a dated cost model. Personal income/capital-gains tax is outside trade-level labels and must be identified as excluded in equity projections.

Base slippage hypotheses: 10 bps per side for sufficiently liquid Core names, 20 bps per side for Satellite; stress 25/50 bps and delayed/manual fills. These are assumptions to test, not measurements of this account.

Features: 16 required, at most 18 after evidence

All continuous preprocessing is fitted inside training folds. Prices/returns use a documented, consistent corporate-action policy. No future screen values, future index constituents, ticker identity feature, delivery volume, invented order-book data or intraday volume scaled to a full-day average.

| # | Feature | Definition / intended incremental edge |
|---|---|---|
| 1 | rsi14 | Existing candidate-state anchor; test whether location within the admitted range still matters |
| 2 | rsi_change3 | RSI_d - RSI_(d-3); short recovery acceleration, replacing the current 14-session difference mislabeled as a slope |
| 3 | ema21_slope5_atr | (EMA21_d - EMA21_(d-5)) / ATR_d; distinguishes an improving underlying trend from a one-bar cross |
| 4 | dist_ema21_atr | (Close_d - EMA21_d) / ATR_d; extension/pullback depth in risk units |
| 5 | atr_pct | ATR14_d / Close_d; stop width and cost burden relative to price |
| 6 | volatility_ratio63 | ATR14_d divided by its trailing 63-session median; expansion versus compression |
| 7 | rel_volume20 | Volume_d / median(Volume_(d-20:d-1)); completed-day participation versus a strictly prior baseline |
| 8 | log_turnover20 | log(1 + median(Close*Volume over 20 completed sessions)); tradability proxy, not a spread claim |
| 9 | last_gap_atr | (Open_d - Close_(d-1)) / ATR_(d-1); recent overnight repricing |
| 10 | downside_gap90_60 | 90th percentile of max(0, Close_(t-1)-Open_t)/ATR_(t-1), last 60 sessions; overnight tail exposure |
| 11 | beta60 | Stock/Nifty return beta from 60 aligned sessions, shrunk toward 1; market dependency |
| 12 | residual_return20 | Stock 20-session return minus beta60*Nifty 20-session return; relative strength beyond market beta |
| 13 | nifty_distance50_atr | (Nifty Close - EMA50) / Nifty ATR14; strength of regime, not only its sign |
| 14 | breadth50 | Fraction above EMA50 in the dated eligible scan universe; tests whether index strength has participation |
| 15 | regime_on | Actual completed-session Nifty flag, 1 or 0; no hard-coded value |
| 16 | cost_R | Known proposed-ticket round-trip fee estimate / planned gross stop risk; tests economic feasibility of small tickets |
| 17, optional | sector_excess20 | Stock return minus a verified sector-index return; only for explicit, versioned mappings and verified feed coverage |
| 18, optional | india_vix_rank252 | Trailing percentile of completed-session NSE:INDIAVIX; tests a distinct volatility regime |

Each row is a testable hypothesis, not a claimed established edge. Run feature-group ablations. Discard features that fail to improve economic/calibration performance across outer folds. Drop BB position from the model initially because it already tightly constrains candidate selection; keep it in the entry rule and diagnostics. Drop the duplicated SMA50 distance feature in favor of the explicit trend/market context above.

breadth50 is breadth of YOUR sampled universe, not NSE-wide breadth. Compute it on all eligible names before choosing candidates, with a fixed dated denominator, at least 95% required price coverage and a coverage field outside the model. Do not exclude data failures silently or compute historical breadth from today's screen membership.

Fetchability and regime policy

Keep Nifty close/EMA50 for the first controlled experiment. The minimal package needs the same stock OHLCV plus one Nifty series already used by the bot. Compute breadth from the existing scan; fetch market context once per invocation.

TradingView publicly lists NSE:INDIAVIX and NSE:CNXIT. This establishes symbol existence, not successful authenticated tvDatafeed access in this Lambda. Implement a probe that verifies symbol resolution, sufficient completed bars, date alignment and repeated availability before either becomes required. Sector indices are not interchangeable: CNXIT is an IT series, not a proxy for defence.

Use three explicit states: RISK_ON, RISK_OFF and DATA_INVALID. Required context missing/stale means DATA_INVALID and no new actionable entries. Do not silently set VIX to zero or reuse a model trained with unavailable features. Optional enhanced models need their own validated schema and deployment bundle; the 16-feature version can be the independently approved champion.

Do not add USDINR in v2. It increases alignment and parameter-search burden without demonstrated incremental value. Delivery percentage is unavailable from this OHLCV route and is excluded.

For RISK_OFF compare three predeclared policies on identical out-of-sample opportunities:
- skip;
- half the RISK_ON risk/notional caps;
- a separately specified mean-reversion candidate sleeve, shadow-only and not merged into the original model.

Initial production default for NEW trades should be skip until a costed out-of-sample comparison supports another choice. Halving a negative-expectancy strategy reduces its loss magnitude; it does not create an edge. Estimate policy performance under the actual total book and cash constraints, not just RISK_OFF win rate.

Data and sample design

Request 2,000–5,000 daily bars where available, incrementally refresh caches, and document actual coverage. Require chronological unique sessions, valid OHLC, nonnegative volume, warm-up and adjustment checks. Detect splits/bonuses/dividend adjustments; review unexplained large jumps and quarantine unresolved cases instead of treating them as tradable gaps.

Archive dated Screener membership and screen values going forward. Membership for session d must have been known by decision time, with publication timestamps for any fundamental inputs. Do not backfill a current Piotroski/capacity/oversold membership into the past. Expand research across a documented liquid equity panel only where historical eligibility can be justified. Use sector and symbol holdouts to test whether results depend on a handful of stocks.

If point-in-time historical membership is unavailable, label the historical experiment survivorship/selection-biased and use it for development only. It cannot establish a deployable edge. Build the unbiased evidence prospectively. Keep the live entry gate unchanged while measuring scarcity; do not manufacture sample size with random non-signal days, SMOTE or duplicated quantity variants.

Provisional tradability filters: median 20-session rupee turnover >=₹5 crore for Core and >=₹2 crore for Satellite; order <=0.1% of that turnover; at least 60 valid recent common return observations; exclude unresolved lot-size, circuit/suspension or corporate-action cases. These are conservative admission hypotheses, frozen and sensitivity-tested, not measured optima. Known SME/lot restrictions require actual instrument metadata; never assume every cash equity has a one-share tradable lot.

Validation and calibration

Use trading dates and event information intervals, not shuffled rows.
- Seek at least 36 months initial training, six months calibration/policy-selection development, then six months outer test; roll forward six months, expanding or using a predeclared five-year cap.
- Purge any training/development event whose signal-to-exit interval crosses the next segment's boundary. Labels must already be mature when a model/calibrator is fitted.
- Keep all symbols on a date, all variants of an event and repeated opportunities during an existing position in the correct groups.
- Add a conservative ten-session exclusion buffer at train/calibration/test boundaries for the baseline horizon. Use fifteen sessions when comparing the longer horizon. A label's actual end time, including delayed exits, overrides a shorter fixed buffer.
- In any split admitting samples after an evaluated interval, embargo those subsequent samples too. A strictly past-to-future fit has no future-side training data; the boundary buffer is an additional precaution, not a substitute for purging.
- Fit scalers/imputation, model hyperparameters, calibration and policy thresholds using only the development data. Use nested chronological splits where choosing between alternatives.
- Reserve the latest six months as a final untouched chronological test. Never tune a threshold or choose XGBoost by this last period.
- Date-block bootstrap complete cross-sections with blocks at least the typical holding horizon; examine 10–20-session blocks and report the sensitivity. Do not bootstrap individual highly overlapping trades as independent observations.

Fit Platt calibration on out-of-time predictions/margins disjoint from model fitting. Calibrator training must not see outer-test labels. For limited data this is the preferred calibrator. Isotonic is only a challenger after roughly 1,000 independent calibration events and a stability check; correlated quantity variants do not satisfy that count.

Initial model candidates

1. Rule/liquidity/regime-only baseline.
2. L2 logistic regression, standardized continuous inputs using training-only mean/scale; C in {0.1, 1.0}; no class balancing; deploy coefficients, scaler and calibrator as JSON.
3. XGBoost challenger, small declared search:

    objective=binary:logistic
    booster=gbtree
    tree_method=hist
    max_depth in {2, 3}
    n_estimators <= 300
    learning_rate=0.03
    min_child_weight in {10, 20}
    subsample=0.8
    colsample_bytree=0.8
    reg_lambda=10
    reg_alpha=1
    scale_pos_weight=1
    max_bin=64
    early_stopping_rounds=30
    n_jobs=2
    random_state=42

Prefer depth 2. min_child_weight and regularization discourage isolated-name leaves. Stop at the inner-validation best iteration and export only retained trees. If there are too few events for stable leaves/calibration, use the simpler validated model or stay in shadow mode. Pin the offline XGBoost version; disable unsupported categorical/DART/multi-output variants.

A ranker is not the first choice: daily candidate groups are likely tiny, it supplies no calibrated probability, and it still needs an economic/risk layer. Regression directly on net_R is a useful later challenger, but a rare, gap-heavy target can be noisy.

Probability-to-trade mapping

The new probability means P(net_pnl(q)>0 under the specified execution/exit/cost policy). It does not mean P(TP first). Display that definition and the model version in logs.

Calibrated probability alone is insufficient. Estimate positive and negative net_R magnitude distributions from development out-of-time predictions, with shrinkage toward pooled results and very few sufficiently populated cells. Use probability bands and regime only if supported; include ticket-cost dependence. Sparse groups cannot produce confident bespoke estimates.

    expected_net_R(q) = p(q)*mean_positive_net_R
                        + (1-p(q))*mean_nonpositive_net_R

Record uncertainty using date-block resampling. Rank admissible signals by a conservative expected_net_R estimate after portfolio penalties; do not allocate in CSV order. Admit only if the development-frozen lower confidence bound of expected_net_R is positive and all execution/risk/state constraints pass. A cohort bound is not an individual-trade guarantee.

Freeze mapping/thresholds and evaluate the entire admission policy on each outer test. Replace Core 0.55 / Satellite 0.50 with economically selected thresholds. Satellite needs at least as much economic evidence and a larger friction allowance; it does not deserve a lower threshold merely to produce more signals.

Required research outputs before trusting a model

Persist events.csv or parquet, fold_manifest.json, out_of_fold_predictions.parquet, fills/rejections.csv, portfolio_daily.csv, calibration.json, metrics.json, model_native, deployment model.json and a dataset manifest with hashes.

Report:
- Unique candidate events, unique filled events and quantity variants separately; dates, symbols, sector/sleeve/regime and failure coverage.
- Target/stop/vertical/censored/unfilled counts; net-win rate, gross and net mean/median R, payoff distributions, gap losses, MAE/MFE, duration.
- Brier and log loss against a training-prevalence benchmark, calibration intercept/slope, reliability buckets with counts/intervals; PR-AUC and precision at actual trade coverage as secondary diagnostics.
- Net expectancy with date-block confidence intervals; profit factor, turnover, fill rate, cash rejection rate, daily marked-to-market drawdown, worst month and tail losses.
- Performance by fold, symbol, sector, regime, cost range and baseline/challenger; concentration of P&L in best names/dates.
- Stress for fees, slippage, delayed execution, missing feeds and unresolved stop orders.
- Portfolio results using ₹27,560 initial cash and the supplied legacy inventory, separately attributing new-trade P&L, legacy price movement, dividends, deposits and withdrawals.

Provisional promotion minimums: at least three outer folds, 200 unique mature out-of-sample fills, at least 50 positive and 50 nonpositive fills, at least 20 names, positive after-cost lower confidence bound and improvement over the simpler baseline without materially worse calibration/drawdown. These are governance floors, not proof of statistical power. A RISK_OFF policy needs its own adequate sample; an overall count cannot establish it. Insufficient evidence means shadow mode, not relaxing gates.

Online monitoring

Write an immutable decision record even for rejected candidates, retaining signal/quote times, features, feature/schema hash, raw/calibrated score, expected R, model hash, regime, proposed q, rejection reason and alert ID. Separate suggested, alerted, acknowledged, partially filled, filled, canceled, expired and closed states.

Only mature outcomes enter delayed Brier/log-loss/calibration metrics. Report both all eligible shadow opportunities and the actually filled subset to expose execution-selection effects. Monitor realized-vs-predicted probability, net_R, observed slippage, feed coverage, rejection counts and calibration drift by model version.

Run lightweight summaries from the existing monitor path or offline audit command; no new heavy library is needed in Lambda. Use trailing 50/100 UNIQUE mature events where available, not a fake weekly precision estimate on five trades. A predeclared rule such as negative Brier skill and nonpositive net-expectancy evidence for two successive sufficiently populated windows should pause new model-driven entries pending review. Immediate schema/data/state failures pause them immediately. Feature drift alone triggers investigation, not automatic retraining or threshold changes.

C. Risk and portfolio

Kelly: what can be calculated now

The 35%/36% figures are supplied assumptions, not verified artifacts. Under a two-outcome, frictionless +2R/-1R model:

| p | Expected gross R | Full Kelly risk fraction | Half Kelly |
|---|---:|---:|---:|
| 0.35 | +0.05R | 2.50% | 1.25% |
| 0.36 | +0.08R | 4.00% | 2.00% |

For gains +G and losses -L in gross-stop-risk units:

    f_star = max(0, (p*G - (1-p)*L) / (G*L))

This is a risk fraction of the chosen trading bankroll, not a direct stock notional allocation.

If costs consume c R for either outcome, G=2-c, L=1+c and breakeven p=(1+c)/3. At c=0.10R, p must exceed 36.67%; 35% and 36% both have zero positive Kelly. Timeouts, gap losses, varying stops and correlated positions invalidate the simplistic binary shortcut further.

Illustration using the existing conservative allowance, not a measured fill sample:
- ₹4,500 ticket, stop distance 3% of entry: planned price risk ₹135.
- 0.25% + ₹18 = ₹29.25, or 0.2167R.
- Breakeven resolved win rate becomes 40.56%.
- Add 10 bps adverse slippage per side: ₹9 extra, total 0.2833R; breakeven is 42.78%.

Thus the old win-rate estimate cannot justify the current ticket size. It also cannot prove that every version is unprofitable: timeout economics and ML selection remain unmeasured.

Re-estimate after-cost Kelly from the complete out-of-sample net_R distribution, including profitable timeouts, adverse gaps and execution failures:

    maximize over f: mean(log(1 + f*R_net))

Use date-block uncertainty, stress losses and a cap below any bankruptcy boundary. Prefer the conservative lower tail of the estimated optimal fraction; if its evidence is nonpositive, f=0. For the concentrated account, use at most quarter-Kelly as an additional risk ceiling initially; report half-Kelly for comparison, not as the deployment default. There must be no minimum positive Kelly floor.

Initial cash/risk admission policy

These are conservative starting limits for validation, not fitted optimal weights.
- C is reconciled deployable S3 cash after unsettled amounts, fee reserve and existing order reservations. Freeze its value for one allocation run, then decrement available cash as proposals are reserved.
- Preserve cash sleeve envelopes: Core 2C/3, Satellite C/3. Do not borrow from legacy marked value or expected sales.
- Preserve max four Core and two Satellite NEW positions; filled and pending new entries count. Existing legacy holdings do not consume these new-strategy slots, but do count in exposure limits and symbol exclusions.
- Maximum ticket = min(C/6, 5% of current marked total equity), with no requirement to fill the ticket.
- Core proposed loss budget <=0.50% of C; Satellite <=0.35% of C.
- Aggregate stressed loss budget of open and pending NEW trades <=2% of C at admission. A correlated cluster gets <=1% of C.
- C falling after fills may block future admissions; it does not generate forced legacy sales. These admission rules intentionally throttle turnover when cash is depleted.
- Maintain roughly 10% of cash uncommitted initially for fees, gaps and execution discrepancies; sleeve limits are envelopes, not a demand to spend them all.
- Apply any validated conservative fractional-Kelly ceiling on top of these limits, never instead of them.

At C=₹27,560 before reserves:
- Core envelope ₹18,373.33; Satellite ₹9,186.67.
- Maximum ticket ₹4,593.33, subject to the equity cap.
- Core loss ceiling ₹137.80; Satellite ₹96.46.
- New-trade aggregate stressed loss ceiling ₹551.20.
Actual deployable figures are lower after the reserve and any outstanding commitments.

Size by iterating integer q downward:
1. q satisfies actual lot size, available sleeve cash and total cash including buy fees.
2. q times stressed stop distance plus charges <= individual risk allowance.
3. Post-trade new-book heat, sector/theme caps and correlation constraints pass.
4. Recompute cost_R, probability and conservative expected net_R for q.
5. q=0 or no positive conservative expectancy means skip.

Stress distance should include at least the greater of an additional 0.5 ATR gap allowance or a reliably estimated adverse-gap tail, plus execution slippage; freeze the chosen rule in validation. It is a stress allowance, not a guaranteed maximum loss. Where empirical tails are poorly measured, use the explicit conservative floor and scenario tests.

Legacy concentration and no-loss policy

Using the user-supplied 21 September book:

| Holding | Qty | Average cost | Cost basis | BE under supplied formula |
|---|---:|---:|---:|---:|
| HAL | 14 | ₹5,105.01 | ₹71,470.14 | ₹5,119.06 |
| CDSL | 6 | ₹1,388.33 | ₹8,329.98 | ₹1,394.80 |
| ZENTEC | 6 | ₹1,760.00 | ₹10,560.00 | ₹1,767.40 |
| BPCL | 6 | ₹318.65 | ₹1,911.90 | ₹322.45 |
| INFY | 5 | ₹1,030.00 | ₹5,150.00 | ₹1,036.18 |
| Total | | | ₹97,422.02 | |

Cash plus cost basis is ₹124,982.02. HAL is 73.36% of invested cost and 57.18% of this cost-plus-cash proxy. HAL plus ZENTEC is 65.63% of that proxy. Current marked weights require current prices and reconciled holdings; these are not NAV estimates.

Store each existing lot as legacy_profit_only. NEW entries use system_atr. A losing system_atr trade may not be relabeled legacy_profit_only to escape its stop or vertical exit.

Preserve every requested legacy invariant:
- BE floor = avg_cost*1.0025 + 18/qty.
- T1 = max(BE + 1 ATR, nearest causally known resistance above BE).
- T2 = avg_cost + 3 ATR, bumped when necessary to remain above T1.
- Approximately one-third at T1, one-third at T2; remainder is a runner.
- Trail only becomes eligible after observed LTP>T1; trail=max(BE, HWM-1.5 ATR).
- EMA9 below EMA21 weakness exit only when LTP>BE.
- No new buys of any current holding, including INFY.
- No below-BE recommendation or order for HAL, CDSL, ZENTEC, BPCL or INFY.

Resistance must use already confirmed pivots/observations; no future swing highs. Persist planned target levels, original/stage quantities, actual fills and HWM timestamps. A repeated alert must not repeatedly sell another third. For HAL, 5/5/4 is one deterministic approximately-third schedule; six shares can use 2/2/2, five can use 2/1/2.

Keep the requested BE formula as a hard minimum, and add a net-positive tranche check using the actual proposed sale quantity and cumulative charges. DP charges on separate sale dates can recur; a whole-position ₹18/qty allowance does not necessarily cover every small tranche. Neither a previously positive LTP nor a trigger guarantees a profitable fill. Use an executable sell limit no lower than the stricter BE/net-profit floor. If the market gaps below it, allow no fill and report the position as still exposed. This preserves the rule but cannot cap downside loss on the legacy book.

Concentration repair without forced losses

Grandfather existing breaches; block additions that worsen them. Starting targets for admission:
- New single-name exposure <=5% of marked total equity.
- Sector exposure <=25%; correlated investment theme/PSU overlay <=35%.
- Defence and PSU tags overlap; test each independently, do not sum overlapping percentages into a fictitious total.
- No new defence/PSU exposure while the relevant legacy bucket exceeds its cap.
- Use 60- and 120-session aligned return correlations with shrinkage; corr>0.70 forms a cluster. While HAL remains dominant, reject new candidates tightly correlated with it even if they have a different sector label.
- Missing correlation data is not proof of diversification. Require minimum common observations or a conservative mapped cluster.

Set legacy single-name concentration milestones such as below 40%, then below 25%, achieved only through permitted profit-taking, changes in market value and additional realized capital growth. Do not promise a deadline for HAL recovery.

For intuition only, holding the cost-plus-cash denominator fixed: selling five of 14 HAL shares near permissible T1 would reduce HAL's cost-weight proxy from 57.2% to 36.8%; selling another five at permitted T2 leaves four and roughly 16.3%. Real weights depend on actual market prices and net fills. Recovered principal becomes cash; it is not profit. Approximately ₹25.6k from selling five shares near BE must not be logged as ₹25.6k earned.

Confirmed net sale proceeds increase cash, and the next invocation recalculates sleeves. Do not count an alert, GTT trigger or unsettled sale proceeds as deployable cash without reconciliation.

Goals and measurement

₹1.35L is about 8.02% above the cost-plus-cash proxy, but current marked NAV could be materially lower because of underwater legacy positions. Calculate the required return from actual starting NAV, net of contributions. The 15 December checkpoint should report net liquidation value, realized/unrealized attribution, drawdown, concentration and model-validation progress.

Doubling total equity takes approximately 17.7 months at 4% every month or 11.9 months at 6%, mathematically. Those rates are aggressive scenarios, not evidence-backed forecasts. If only ₹27,560 compounds and the ₹97,422 legacy value stays flat, the same cash-return assumptions produce only about ₹1.42L–₹1.53L total after 12 months, or ₹1.53L–₹1.76L after 18 months. The ₹2.5L goal depends on whole-book returns, released capital and/or contributions, not the cash sleeve alone.

D. Cursor implementation order

Instructions to the implementing agent

Work on an isolated research branch. Preserve production AWS schedules and notification destinations. Begin with read-only provenance capture and reconciliation. Do not deploy this repository's older Lambda over the newer live implementation. Do not fabricate a model file without the required data and validation evidence.

Phase 0 — establish an authoritative baseline
- Add scripts/audit_deployment.py: read-only AWS artifact/configuration inventory, code/model/schema hashes and required S3 object availability, excluding secrets.
- Recover the actual portfolio CLI/state schema and holdings monitor implementation; compare against the audited commit.
- Preserve the user-supplied book as a reconciliation fixture, not a blind overwrite of today's S3 state.
- Capture baseline artifacts and data provenance. Mark historical daily-price-only runs as diagnostic.
- Put symbol mappings in shared/symbols.py; stop importing Lambda for a constants table.

Phase 1 — backtest and data contracts first

| File | Required change |
|---|---|
| shared/calendar.py | Exchange sessions/holidays, Asia/Kolkata conversion, completed-session and freshness selection; checked calendar file refreshed from official exchange notices |
| shared/indicators.py | One RSI/ATR/EMA/Bollinger implementation with explicit seeding, warm-up and flat/rising edge cases |
| shared/features.py | Canonical feature order/formulas, input checks, quantity-aware cost_R; no model library imports |
| backtest/features.py | Thin import/re-export for research compatibility, no duplicated formulas |
| shared/costs.py + config/costs.json | Dated fees, per-stock/per-day DP accounting, actual sell quantities, slippage separate from fees |
| shared/universe.py + data/universe_snapshots/ | Point-in-time membership, exchange-qualified identity, sectors/themes, lot metadata, source/as-of dates |
| backtest/fetch_history.py | Incremental refresh, up to feed-supported bars, no stale cache hit shortcut, daily/minute provenance, index alignment and data-quality checks |
| backtest/labeler.py | Entry/exit execution contract, triple barriers, vertical economics, censoring, gaps, paths, quantity variants and immutable event IDs |
| backtest/execution.py | Chronological post-alert fills, latency scenarios, unfilled/circuit cases, limit/GTT behavior and fill fidelity |
| backtest/splits.py | Date-based purged walk-forward, label availability, embargo and event-family grouping |
| backtest/portfolio.py | Cash/fees/settlement/reservations, overlapping holdings, no averaging, risk caps, actual integer lots, separate legacy attribution |

Phase 2 — train, validate and freeze

| File | Required change |
|---|---|
| backtest/train.py | Logistic and XGB comparison, training-only preprocessing, inner early stopping, no class reweighting by default |
| backtest/calibrate.py | Out-of-time Platt fit, reliability diagnostics, optional isotonic challenger |
| backtest/evaluate.py | Fold/portfolio/stress metrics, probability-to-economic mapping, block intervals, explicit promotion failures |
| backtest/export_model.py | Versioned deployment artifact, actual intercept, retained trees, calibration/scaler/cost/policy metadata and integrity manifest |
| scripts/check_model_parity.py | Offline native vs deployment scorer probabilities/margins and decision parity |
| config/research_v2.json | Frozen entry, horizons, latency, costs, features, folds, hyperparameter grid and promotion rules |
| backtest/requirements.lock | Pin tested offline versions separately from Lambda dependencies |

Only freeze a candidate after the predeclared evaluation is complete. After selecting the model family/hyperparameters, fit the deployment estimator with a final disjoint recent calibration segment; if refitting on that segment, regenerate appropriate out-of-time calibration rather than attaching an old calibrator to a differently fitted model. Final reported test metrics belong to the frozen evaluation procedure, not an invented evaluation of a model trained through the test.

model.json contract, schema version 2:
- model_id, training_cutoff, research_commit, training-library versions.
- model type, raw base margin, ordered feature_names, feature_schema_hash.
- indicator/feature/cost/execution versions and required feature set.
- exact retained dump trees OR logistic coefficients plus mean/scale.
- Platt coefficients and its exact input convention (raw margin).
- economic mapping/distribution summaries, uncertainty policy and valid ticket/cost range.
- policy version, regime admission rules, expiry/review date.
- source artifact hashes and out-of-sample metrics manifest reference.

Do not silently load unknown raw learner JSON, ignore feature changes, omit preprocessing or fall back to an unvalidated nested model. threshold_default must either be enforced by the versioned policy or removed.

Phase 3 — Lambda integration, then shadow mode

| File | Required change |
|---|---|
| runtime/scorer.py | Small validated JSON interpreter; float32 split comparisons, base margin, stable sigmoid, scaler/calibrator; no sklearn/XGBoost |
| runtime/risk.py | Cash-only integer sizing, zero edge=>zero size, stressed heat, sector/theme/correlation admission |
| runtime/state.py | S3 source of truth, schema validation, conditional version writes, immutable fill/reservation events, reconciliation |
| runtime/monitor.py | Recover/preserve legacy behavior; add NEW-trade vertical exits and execution-status tracking without applying them to legacy positions |
| lambda_function.py | Orchestrate shared daily features and timestamped monitoring prices; real regime flag; collect all candidates, rank, reserve and alert; weekend/holiday/dedup gates |
| portfolio_cli.py | Recover if it exists outside repo; import confirmed fills, partial fills, fees, cash adjustments, cancellations and reconciliation; no optimistic fill inference |
| invoke_aws.py | Valid actions, explicit --dry-run/--no-notify and fixture mode; nonzero exit on FunctionError/application failure; dry-run must suppress ALL writes/notifications |
| .github/workflows/trading_pipeline.yml | Leave production schedules disabled; optional CI only for offline tests |
| requirements-lambda.txt + scripts/build_lambda_layer.sh | Pin tested tvDatafeed commit/dependencies, package shared/runtime code and run Python3.12 import smoke; no ML training wheels |

S3 state must distinguish order reservation from holdings. An alert may reserve a bounded amount, but only a confirmed fill changes holdings/cash. Unacknowledged alerts expire; acknowledged orders retain reservation until canceled/filled/reconciled. Idempotent broker fill IDs prevent duplicate debits. Maintain unsettled vs available cash and daily fees.

Use version-checked S3 writes or a single-writer mechanism to prevent the monitor, recommender and CLI from overwriting one another. Keep immutable per-run/per-event JSON objects instead of appending one growing text blob by read-modify-write.

Keep EventBridge at 09:25 and 15:05 IST. At 15:05, use current verified price for eligible legacy profit/trail alerts, but completed-day indicators for a confirmed daily weakness signal. Any current-day EMA cross may be displayed as provisional, not silently treated as the completed-day strategy.

Lambda runtime budget
- Compute indicators/context once per symbol/run and Nifty once per run.
- Reuse bounded history from S3; request incremental updates. Validate unchanged historical data against adjustment revisions.
- In a two-pass scan, establish universe coverage/breadth first, then score/rank candidates.
- Score JSON in memory; a few hundred shallow trees or a small logistic model is inexpensive compared with network fetches.
- Use bounded timeouts/retries, explicit elapsed-time limits and enough remaining time for state/log writes.
- Do not enlarge the live universe beyond the measured p95 runtime budget. Offline training may cover a larger justified panel.
- Standard Lambda functions have a 900-second ceiling; this strategy should finish much sooner to preserve the 09:45 entry cutoff. A 360-second invoke client timeout does not configure Lambda's timeout.

Phase 4 — release only after gates

Run a versioned shadow candidate alongside existing notifications without producing extra actionable BUY messages. Minimum operational soak: 20 exchange sessions plus fully matured outcomes; this is an engineering check, not a replacement for the statistical sample floors. Compare stored feature vectors, raw/calibrated scores, candidate ordering, alert latency and fills.

Stage immutable code/model objects, validate them together, and switch the approved deployment pointer/alias as one controlled release. Preserve a working previous bundle for rollback. Never overwrite the active model key before the compatible scorer is ready. Monitor legacy-policy invariants during rollout.

Commands

The following three commands exist in the audited repository. They require local universe CSVs, dependencies and feed access, and produce an UNCORRECTED BASELINE only:

    python backtest/fetch_history.py
    python backtest/labeler.py
    python backtest/train.py

They are not valid evidence for deploying v2. The current fetcher also requires more live-import dependencies than its backtest requirements alone make clear.

The implementing agent must ADD the following proposed CLI interfaces; these are not claimed to exist today:

    python -m pip install -r backtest/requirements.lock
    python scripts/audit_deployment.py --read-only --out artifacts/deployment_snapshot
    python -m backtest.fetch_history --config config/research_v2.json --refresh --bars 2000
    python -m backtest.labeler --config config/research_v2.json --out artifacts/v2/events.parquet
    python -m backtest.train --config config/research_v2.json --walk-forward --out artifacts/v2
    python -m backtest.evaluate --config config/research_v2.json --artifacts artifacts/v2 --stress
    python -m backtest.export_model --artifacts artifacts/v2 --require-promotion-pass --out artifacts/v2/model.json
    python scripts/check_model_parity.py --artifacts artifacts/v2
    python -m pytest -q tests
    python invoke_aws.py recommender --dry-run --no-notify --model-key models/candidates/v2/model.json

No production upload/activation command is implied by this report. In particular, the last command is unsafe on the current CLI until dry-run suppression is actually implemented and tested.

Acceptance tests before any deployment

1. Timing: 09:25 and 15:05 never use today's incomplete daily bar as a completed signal; weekends, exchange holidays, stale feeds and missing sessions are handled by session IDs.
2. Causality: changing future bars cannot change an earlier feature/candidate; index and breadth joins are as-of; confirmed resistance cannot see future pivots.
3. Indicators: shared outputs and feature arrays match offline/live fixtures, including RSI flat/up/down sequences, warm-up, ATR gaps, zero volume and exact column order.
4. Regime: a RISK_OFF candidate passes regime_on=0; missing index =>DATA_INVALID; no fail-open buys.
5. Labels: positive/negative vertical exits, same-bar ambiguity, opening gaps, censored tails, unfilled orders, post-entry-only prices, circuit/suspension and quantity-dependent costs.
6. Costs: per-stock/day DP aggregation, separate-day tranches, actual price/quantity, no double-counted slippage, net BE floor.
7. Splits: no shared signal dates, overlapping label intervals, quantity families or unpublished future labels across boundaries; preprocessing/calibration fit only on permitted rows.
8. Scorer: native XGB/logistic vs JSON raw/calibrated probabilities within predeclared tolerance (e.g. absolute <=1e-6), float32 nextafter values around every split, missing branch behavior, true base margin, retained tree count. Same admission decisions outside a small declared score guard band; inside it skip.
9. Failure handling: missing model/column, reordered schema, invalid hash, NaN/Inf probability, stale context or state conflict never emits an actionable new BUY.
10. Portfolio: price>cap=>0 shares; all fills/reservations fit available cash including fees; actual lot sizes; max 4/2 new positions; no current holdings buys; concentration/correlation/heat limits.
11. Legacy invariants: no below-BE recommendation, tranche net-profit guard, no trail before LTP>T1, correct T1/T2 bump, no duplicate stage execution, no sale quantity>holding.
12. Accounting: TIMEX example credits sale proceeds ₹3,560 before charges and records ₹550 gross profit; these are distinct. Partial fills, canceled/expired reservations and idempotent fill IDs reconcile.
13. State/operations: concurrent CLI/monitor writes cannot lose fills; rerun same EventBridge event cannot duplicate orders/alerts; dry-run produces zero state writes and zero ntfy/email sends.
14. Runtime/package: Python3.12 Lambda-compatible imports succeed without sklearn/xgboost; realistic maximum scan meets time/memory budget; invocation failures return nonzero CLI status; rollback restores matching code/model.
15. Economic gate: corrected out-of-sample portfolio replay beats the simpler baseline after realistic costs and stress without violating user constraints. If not, do not promote the model.

E. Explicit exclusions and invariants

- No selling HAL/CDSL/ZENTEC/BPCL/INFY below the required true-profit floor.
- No averaging down or additional purchases of any current holding.
- No converting a failed new system trade into a protected legacy trade.
- No sklearn, XGBoost, training jobs or new heavy ML dependencies inside Lambda.
- No options, intraday strategy, leverage, margin or shorting.
- No enlarged bets to chase ₹2.5L by December; no assumed monthly return used as evidence.
- No fabricated events, win rates, calibration, delivery data, full intraday history or point-in-time membership.
- No assumed paid NSE API or bhavcopy key. Base features use the existing OHLCV route; calendar files can be maintained from free official exchange publications.
- No random row split, unpurged overlapping labels, test-set threshold selection or free retrain loops until performance looks good.
- No next-open daily proxy presented as a 09:25 executable result.
- No silent fail-open change of strategy.
- Keep EventBridge, ntfy/Gmail routing, AWS-only schedules, S3 cash authority and the protected legacy policy.

External technical and operational references

These sources were checked on 7 October 2026; they support mechanics, not the existence of a trading edge:
- [XGBoost fitted intercept behavior](https://xgboost.readthedocs.io/en/stable/tutorials/intercept.html)
- [XGBoost parameter documentation](https://xgboost.readthedocs.io/en/release_3.1.0/parameter.html)
- [scikit-learn calibration guidance](https://scikit-learn.org/stable/modules/calibration.html)
- [tvDatafeed repository and documented limits](https://github.com/rongardF/tvdatafeed)
- [TradingView India VIX symbol](https://www.tradingview.com/symbols/NSE-INDIAVIX/)
- [TradingView Nifty IT symbol](https://in.tradingview.com/symbols/NSE-CNXIT/)
- [Zerodha charges](https://zerodha.com/charges/)
- [Zerodha DP charging examples](https://support.zerodha.com/category/account-opening/resident-individual/ri-charges/articles/what-do-dp-charges-mean)
- [Zerodha GTT behavior](https://support.zerodha.com/category/trading-and-markets/charts-and-orders/gtt/articles/what-is-the-good-till-triggered-gtt-feature): triggers and orders are separate, gap triggers can remain unfilled, and account/authorization requirements matter. Current documentation describes limit and protected-market variants; do not assume every GTT is a guaranteed stop fill.
- [NSE cash-market timings](https://www.nseindia.com/static/market-data/market-timings)
- [NSE exchange holidays](https://www.nseindia.com/resources/exchange-communication-holidays)
- [AWS Lambda timeout documentation](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html)

