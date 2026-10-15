# India 150 v4: implementation and research review

Review date: 15 October 2026. Base: quant-v3-registry / bb4912c. Research data frozen at the completed 13 October session. No production resources were changed. This is a research and control candidate, not a promoted trading strategy.

## Audit and changes

The v3 research helper was not the live allocator. Live admission still needed aggregate risk, pending-order cash, correlation, fees and gap checks. The holdings level calculation referenced an undefined `trail_active`, and the system-lot exit helper was not called by the monitor. The CLI could bootstrap from stale local/default cash. Monitor writes risked replacing a concurrently updated book. The research replay also needed chronological cash settlement, actual resizing, marked equity and correctly separated model/calibrator selection.

The candidate wires shared admission into Lambda, uses conditional S3 state updates, records idempotent fills and distinct BUY/SELL intents, separates monitor marks from the broker book, and cancels monitor signals if the book changes during its run. New buys must be system lots; legacy status cannot be assigned to a new losing trade. Tranches remain immutable. Prior trailing levels are checked before increasing the watermark. Dry runs suppress writes and notifications.

Keep the profit-only legacy policy, completed-session features, no averaging, bounded cash sizing, and lightweight JSON scorer. Retire the default-cash fallback, fail-open model/regime gates, reverse-engineered Kelly ticket sizing, repeated-alert activity counts and prediction-only performance claims. Older research scripts remain for reproducibility; they are not the v4 training/export path.

## Universe and portfolio

`universes/india_150.csv` contains exactly 150 names across 17 sectors, selected from official current Nifty 200 membership plus held CDSL/ZENTEC. All 150 have research history; 147 were individually affordable below the fixture ticket ceiling. The five holdings are monitor-only, not additional-buy candidates. Current membership, affordability and liquidity selection introduce survivorship/selection bias. This is not a point-in-time universe backtest.

The unchanged broker fixture is cash ₹15,255.50; HAL 16 @ 5,040.63, ZENTEC 8 @ 1,711.25, CDSL 8 @ 1,363.75, INFY 5 @ 1,030, BPCL 6 @ 318.65. On 15 October this 7 October snapshot fails the seven-day freshness gate for new BUYs. Reconcile actual broker cash and quantities before enabling admission; do not merely refresh its timestamp.

| Repair ceiling, using the Oct 7 fixture | Amount |
| --- | ---: |
| Cash reserve, 20% | ₹3,051.10 |
| Core deployment envelope, satellite parked | ₹8,136.27 |
| Per-ticket maximum | ₹3,652.00 |
| Per-trade stressed risk maximum | ₹152.56 |
| Aggregate new stressed risk maximum | ₹305.11 |

These are ceilings, not target allocations. Maximum two new positions includes pending reservations. Sizing includes fees, whole shares, entry-band reservation and downside gap stress. A stop is not a guaranteed loss bound. These new-trade limits do not bound the existing legacy book's downside.

Sector exposure is capped at 25% of marked equity, defence at 25%, PSU at 35%, and pair correlation at 0.65 with sufficient overlapping returns. Existing breaches block additions; they do not force loss sales. Official defence/PSU membership and conservative government-linked metadata are frozen with sources. HAL/ZENTEC dominate concentration: dilution comes from retained cash, permitted profitable tranches and independently qualified new trades over time.

Legacy BE remains avg_cost * 1.0025 + 18/remaining_qty. T1/T2/runner and profit-only weakness exits remain. New system lots retain 1.5 ATR stops, 3 ATR targets and ten-session exits, including losses. The research does not justify changing these targets to manufacture activity.

## Strategies, features and validation

Four v4 families are evaluated: original EMA cross, leader pullback, volume/range breakout and sector momentum. None is promoted. Failed-breakdown and residual-shock work remains in the older research registry. WATCH is not BUY.

The 20-feature `india_v4_20` contract is shared offline and in Lambda:

| Features | Hypothesis being tested |
| --- | --- |
| rsi14, ema21_slope5_atr, dist_ema21_atr | Trend quality and entry extension |
| atr_pct, volatility_ratio63 | Risk scale and volatility expansion |
| rel_volume20, log_turnover20 | Participation and executable liquidity |
| last_gap_atr, downside_gap95_60 | Overnight gap exposure |
| beta60, residual_return20, relative_return60 | Separate index exposure from stock strength |
| momentum126_252, sector_excess20 | Medium-term leadership and sector rotation |
| nifty_distance50_atr, breadth50 | Market participation and trend context |
| cost_R | Whether a small ticket can support its friction |
| setup_pullback, setup_breakout, setup_sector_momentum | Distinguish conditional setup economics |

These are hypotheses, not established edges. Delivery data, VIX and USDINR are not fabricated or made mandatory. Small sectors with insufficient peer coverage fail feature validation. Momentum draws a factor prior from Nifty's volatility-adjusted 6/12-month methodology; that does not establish a ten-day swing edge.

The stricter research regime requires two closes above EMA50, positive five-day EMA50 slope, breadth at least 50%, adequate coverage and an index volatility-ratio limit. Missing data blocks BUY. It did NOT demonstrate improved expectancy over the simpler regime and remains unpromoted. The frozen Oct 13 proxy was RISK_OFF: Nifty 22,721.95 versus EMA50 23,363.06, breadth 18.67%. This is not a live Oct 15 market call. Weekday-only freshness may conservatively block around holidays until an authoritative holiday calendar is bundled.

Labels use path-dependent stop/target/vertical exits, costs, entry gaps, unfilled gaps through the stop, conservative stop-first same-bar handling and censoring. Timeouts use actual net economics rather than automatic zero outcomes. Gap losses may exceed 1R. Daily next-open execution is a proxy, not a validated 09:25 fill model.

Six expanding outer folds use purging/ten-session embargo, separate calibration-fit and model-selection periods, fixed logistic/XGB alternatives and a constant prior. XGB uses depth 2, 160 trees, learning rate .03, minimum child weight 50, L2 20, L1 1, gamma 1 and .8 subsampling. Platt calibration operates on logits; each model retains its own calibrator. Native/JSON raw and calibrated parity, including boundary cases, passed for all folds and the forward shadow fit; maximum errors were below 1.3e-7. No sklearn or XGBoost is imported by the Lambda scorer.

## Results: activity goal is not met safely

See `backtest/artifacts/v4/metrics.json`, `diagnostics.json`, `data_manifest.json` and replay fills. There are 2,194 unique outer-test candidate fills across 130 names. They are overlapping hypothetical opportunities, not independent executed trades.

| Costed RISK_ON candidate family | Count | Mean net R | Approved quarter Kelly |
| --- | ---: | ---: | ---: |
| EMA cross | 129 | +0.0084 | 0 |
| Range breakout | 815 | -0.3172 | 0 |
| Trend pullback | 533 | -0.2561 | 0 |
| Sector momentum | 51 | -0.5904 | 0 |

EMA's 47.3% win rate, 1.248R average win and 1.104R average loss are not a reliable edge: its week-block bootstrap lower bound is -0.192R. Full/half Kelly diagnostics are 0.5%/0.25%; approved sizing is zero because uncertainty and sample requirements fail. Other families are negative. Kelly maximizes empirical log growth over costed returns; it does not substitute nominal 2R for observed payoffs.

The chronological rules-only repair-sleeve replay completed 69 round trips, lost ₹1,294.58, had profit factor 0.647 and 11.63% maximum cash-sleeve drawdown. It generated two distinct BUY/SELL intents in 47.13% of rolling 14-calendar-day windows (48.69% of ten-session windows). Median activity was one. Repeated reminders, WATCH and target touches do not count. Model-edge and Kelly-approved replays admitted zero trades. This counterfactual freezes legacy valuation and is not a historical reconstruction of the user's book.

Outer-test Brier: constant prior .23818, logistic .26568, XGB .26729, selected .23796. Complexity did not establish an improvement. The separately fitted forward shadow XGB has better recent calibration-selection Brier, but no subsequent forward performance. Reused v3 history and current universe selection make all these results exploratory. Do not retune thresholds until the frequency quota appears.

## Sentiment and data reliability

NSE announcements and held-stock Google News feeds are bounded, timestamped, deduplicated risk overlays. Unknown is not neutral. Event/adverse-news review can veto a new BUY; positive headlines cannot override risk gates or trigger below-BE legacy sales. Sentiment is not a trained alpha feature: no historical news is backfilled. Archive contemporaneous snapshots before testing incremental value.

No existing private sentiment backend implementation, URL or authentication was found. An optional HTTPS backend contract is implemented and fixture-tested (`SENTIMENT_BACKEND_URL`); its actual service is not connected or verified. Expected response: `as_of`, and `events` containing symbol, title, url, published_at, tone in [-1,1], event_review.

Official membership/theme downloads, NSE RSS, Google News and Yahoo chart returned successful responses during probes. Later NSE/HAL requests timed out. TradingView initially supplied Nifty plus 73 stocks, then the environment blocked its data endpoint. Thus neither 150-name TradingView parity nor a full 300-second deployed scan has been certified. Historical evaluation uses a uniform, explicitly offline-only Yahoo chart fallback; Lambda remains TradingView-only. Failures become unavailable data, not fake observations. No API can be guaranteed to work perfectly.

## Reproduce and review

Install offline research dependencies from the repository requirements plus pytest; pin tvDatafeed to the commit in requirements-lambda.txt. Save the official membership CSV from https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv to a local file, then:

```bash
python scripts/fetch_india_history.py --metadata-file path/to/ind_nifty200list.csv --bars 2000 --refresh
# Explicit research-only alternative; never a Lambda market-data fallback:
python scripts/fetch_research_fallback.py --metadata-file path/to/ind_nifty200list.csv
python scripts/build_india_universe.py --as-of 2026-10-13 --metadata-file path/to/ind_nifty200list.csv
python -m backtest.india_v4 --config config/research_v4.json
python -m backtest.v4_diagnostics
python -m backtest.preview_v4
python -m pytest -q
python scripts/package_control_candidate.py
```

Use a new configuration/data snapshot for future research, retaining the frozen evidence. Raw histories are not committed; source hashes and protocol are recorded. The old v3 export/parity commands are not the v4 pipeline.

CLI: `python portfolio_cli.py show`; preview a confirmed fill with `python portfolio_cli.py --dry-run fill path/to/fill.json`. Saved fills require an idempotent broker `fill_id`, actual fees and explicit Zerodha `available_cash_after`. BUYs require entry_date, entry_atr, stop, target, setup and system classification; SELLs require the correct immutable tranche/exit stage. Use actual fills, not alert acknowledgements. S3 writes use an ETag lease and stop on conflict.

## Deployment boundary and next evidence

The package switches new-entry inference to the 20-feature contract. The existing seven-feature production model is NOT compatible. Deploying this candidate would pause new BUYs until a qualified v4 model is promoted. The bundled model is forcibly shadow-only. This is not a drop-in signal-frequency upgrade.

Before promotion: reconcile broker book; restore and certify complete TradingView data/feature parity; verify full-scan latency/memory and read-only deployed dry runs; supply the real sentiment backend contract if desired; collect untouched forward observations and actual fill/slippage records. Require at least three purged folds, 200 unique OOS portfolio fills, 50 wins, 50 losses, 20 names and a positive lower confidence bound after costs, plus calibration and drawdown checks. Research/model changes require fresh evidence. No family currently qualifies.

Do not merge/deploy automatically. Keep EventBridge, ntfy/Gmail, S3 broker truth, no averaging, legacy BE protections and cash-only swing scope. Do not sell the legacy book at a loss or increase size to chase the December checkpoint. The current constraints and evidence cannot support a promise of two actionable trades each fortnight.

Sources: https://www.niftyindices.com/indices/equity/strategy-indices/nifty200-momentum-30 ; https://www.nseindia.com/static/rss-feed ; https://zerodha.com/charges/ . Frozen universe/theme URLs are also recorded in the manifests.
