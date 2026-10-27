# Research v6 experiment plan

Frozen before any v6 test result was computed. Branch `research-regime-residual-v6`, based on `e1ab06682e2d1434990bec14ccb0f5eef4a50c98` (`roe-fii-quality-screen`), which already contains `ops-reviewed-activation` through `34d60b0`. This plan does not change production risk limits, promotion approvals, the live alias, or the hypothetical ₹5,748 book.

Seed for every stochastic routine: `20261026`.

## What is already known, and what is not reused as a result

The existing suite at this commit passed 171 tests before any v6 code ran. That is a regression check, not a trading result.

The India v4 review quotes outer-test Brier 0.23818 (constant prior), 0.26568 (logistic), 0.26729 (XGBoost) and a rules replay loss of ₹1,294.58 with profit factor 0.647. Those figures live in git blob `34ce678:backtest/artifacts/v4/metrics.json`. The raw v4 event file is gitignored and is not in the working tree, so this study does not re-estimate those numbers and does not treat them as an untouched holdout. The v3 metrics file is a different study (₹100,000 replay cash, different Brier). Paired v6 comparisons use one capital assumption: hypothetical launch cash ₹5,748 and `config/v5/research_policy.json`. Broker cash is not inferred from that figure.

Cached prices under `backtest/data/NSE_*.csv` are the TradingView research cache used by earlier studies, last bars around 2026-10-08, adjustment mode `tvdatafeed_as_returned`. `universes/india_150.csv` is the current 2026-10-13 universe, not point-in-time membership. ZENTEC has no cache file. There is no price history after this cache that the v4 study could not have seen. A chronological split inside this cache is a holdout inside reused development data, not untouched forward data.

`config/ops/fundamentals_snapshots.json` and the v5 fundamental store are empty of historical point-in-time rows. Fundamental ablation on this history is BLOCKED. An empty result is not a zero-return strategy.

## Schemas that stay separate

Live scoring still uses the seven-feature production model inside `lambda_function.py`. `swing_core/features.py` is `features_v3_20`. `swing_core/india.py` is `india_v4_20`. `swing_v5` features are the SMA/EMA/ATR research set. v6 does not feed one schema into another scorer. v6 model JSON is research-only and is not copied onto the production model key.

The live regime gate remains Nifty close versus its 50-session EMA in `lambda_function.get_market_regime`. `swing_v5/regime.py` challenger rules are not the live gate and are not retuned here. v6 hysteresis is an offline comparator. It does not change production RISK_OFF behaviour.

## Splits

Signal-date splits, using the Nifty session calendar in the cache:

- Train: signal date on or before 2024-12-31.
- Calibration and selection: 2025-01-01 through 2025-06-30.
- Outer test: signal date on or after 2025-07-01.

A trade whose exit session is on or after the next boundary is purged. Signals in the last 10 sessions before a boundary are purged. Ten sessions is the vertical horizon used for these comparisons. Unfinished paths at the cache end stay censored. They are not losses. Unfilled orders are not losses. A time exit keeps its costed P&L. Positive net P&L is the classification label, which is not the same thing as touching the original target.

No random split. No second pass that picks hyperparameters from outer-test P&L.

## Registered search space

Probability models train on unit-quantity completed trades. The ₹5,748 replay is applied afterwards and is not the training population. Parameters are chosen on calibration Brier of that label, after fitting on train. Equal Brier prefers the smaller `m`, then the smaller `C`, then the constant prior, then logistic, then XGBoost.

- Shrinkage strength `m` in {10, 30, 60}. `m=30` is only a candidate.
- L2 logistic penalty `C` in {0.1, 1.0}, implemented as ridge `lambda = 1/C` on slope coefficients, intercept unpenalized.
- Residual ridge on factor slopes: {0.1, 1.0, 10.0}, chosen by mean squared prediction error of stock returns on the train fit windows only.
- HMM covariance: diagonal first. Spherical variance is the only alternative. The training log-likelihood picks between them. Seeds {0, 1, 2}.

Separate per-strategy logistic experts are estimated only when that strategy has at least 200 completed training trades. Otherwise the pooled model is the recorded result and the expert run is logged as not attempted.

Kelly stays off even if a shrunken expected net R is positive.

## Component order

Each row changes one component relative to the row it names.

| Id | Comparison | What changes |
| --- | --- | --- |
| A | Existing directional rule under the frozen policy | Raw 60-session momentum, top decile, live-style EMA50 regime, shared execution |
| B | Meta-model on A's trades | Admission requires positive shrunken expected net R from the calibration-selected model. Prior, pooled logistic, and XGBoost are all scored. |
| C | Residual versus raw momentum | Ranking score only. Same names, dates, liquidity, regime, entry, stops, costs, and risk. |
| D | Hysteresis versus the EMA50 regime | Regime permission only, on A's raw signals. |
| E | Forward-filtered HMM probabilities | Added to the calibration-winning model from B. Features for each date come from a model fit strictly before that date's block. |
| F | Earnings reaction | Runs only if a verified timestamped announcement archive exists. Otherwise the collector and a BLOCKED report. |
| G | OU residual reversion | Research-only, last. Disabled for production regardless of the path. |

## Execution shared by A–E and G

Long-only NSE cash. Next-session open. Limit is the signal close. Fill only if the open is above `limit - 1.5 * ATR20` and at or below `limit + 0.25 * ATR20`. Stop is `fill - 1.5 * ATR20`. Target is `fill + 3 * ATR20`. Vertical exit is the tenth session after the fill, fill session counted as session one, exit at that close if still open. Same-bar stop and target: stop. A missing bar censors the position; it does not skip ahead. Costs use `swing_v5.costs.fee_components` recomputed at the admitted quantity. Slippage 10 bps per side is inside that fee function. DP is once per sale. Quantity uses `swing_v5.risk.largest_quantity` with the research-policy caps, launch cash ₹5,748, gap buffer `0.25 * ATR20`, and `kelly_enabled` left false. Maximum two new positions including pending orders. Aggregate stressed risk and the ₹1,500 completed-loss latch apply. Proceeds are credited at exit only.

A second table reports the same paths at unit quantity with the cost filter still applied. That table is not the ₹5,748 account.

Top-decile membership is every eligible name at or above the cross-sectional 90th percentile that day. Fewer than 20 eligible names yields no signal. Ties are all kept. Symbol order is alphabetical.

Hypothesis H2 is logged and is not the paired comparison: top decile, close at or below EMA20, volume divided by the prior 20-session median at least 1.2, and ATR5/ATR20 at most 1.0. ATR5/ATR20 is a compression ratio, not idiosyncratic volatility.

## Residual construction

Simple returns, `close[t] / close[t-1] - 1`. Log returns are not used here. The HMM uses a log return as its own observation.

At decision index `i` (a completed session):

- Ranking window: indices `i-64` through `i-5` inclusive (60 sessions). The latest five completed sessions are excluded.
- Fit window: indices `i-184` through `i-65` inclusive (120 sessions), strictly before the ranking window.
- The first return index that may enter a fit is 1.

Sector model, named `residual_momentum_sector`: on the fit window, orthogonalize the sector simple return against the Nifty simple return with an intercept. Ridge-regress the stock simple return on Nifty and that orthogonal sector factor, with an intercept and ridge only on the two slopes. Residuals in the ranking window are one-step predictions using those frozen coefficients. They are not in-sample residuals of the ranking window, so an intercept does not force them to sum to zero. The momentum score is their sum. The stock is unhedged in the P&L.

A name without an aligned sector series on the whole fit and ranking window is excluded from this experiment. It is not passed through a market-only regression under the same name.

`residual_momentum_market_only` repeats the procedure with Nifty as the only factor. That is a separate experiment. Raw momentum in each comparison uses the same names and the sum of simple returns on the same ranking window.

The sector label is the `Sector` column of `universes/india_150.csv` as of 2026-10-13. It is not historical membership. A public chart is accepted only when it parses to OHLC. Attempted tickers are recorded with the HTTP or parse outcome. Financials are not mapped to the bank index.

## Meta-model features

Measured on the signal session, current observation excluded wherever a trailing window is specified:

- ATR5/ATR20.
- Round-trip fee at quantity 1 and a flat exit at the signal close, divided by `1.5 * ATR20`. The account path recomputes fees at the admitted quantity.
- Volume divided by the median volume of the previous 20 sessions.
- Cross-sectional percentile of the ranking score used by that experiment.
- Fifth percentile of adverse overnight gaps over the previous 60 sessions, in ATR units. Today's gap is excluded.
- One indicator column per strategy that actually appears. A constant column is not described as an orthogonal factor.

Dependence is reported as the training correlation matrix. HMM probabilities are absent from A–D. Experiment E appends two forward probabilities, `state_rank_0` and `state_rank_1`. The third is omitted from the regression because the three sum to one. The third is still stored.

Shrinkage, fit on training completed trades only:

`mu_win_s = (n_win_s * mean_win_s + m * pooled_mean_win) / (n_win_s + m)`

The loss formula uses `n_loss_s`, `mean_loss_s`, and `pooled_mean_loss`. Zero wins uses the pooled win mean. Zero losses uses the pooled loss mean. Expected net R is `p * mu_win_s + (1 - p) * mu_loss_s` with losses stored as negative numbers.

XGBoost, when importable and the training count is at least 80, uses depth 2, 160 trees, learning rate 0.03, minimum child weight 50, L2 20, L1 1, subsample 0.8, column subsample 0.8. It is a control, not a promotion candidate.

## HMM

Observations, in order: Nifty log return, log high/low range, breadth above SMA50, cross-sectional standard deviation of simple returns.

Breadth denominator is 150, the universe cap, including names that failed to load. A missing bar counts as not above the average and does not leave the denominator. If fewer than 90 percent of the 150 names have a finite close, or fewer than 90 percent have a finite return, that session is an invalid observation. If more than 5 percent of Nifty sessions are invalid, experiment E is BLOCKED. Dispersion is the standard deviation of the finite returns only after that gate. The gate fraction is stored beside it.

Three-state Gaussian HMM. States are ordered by the training-sample mean of the Nifty log return, low to high, and named `state_rank_0` through `state_rank_2`. Those names are sort keys, not accumulation or liquidation. EM may use forward-backward to estimate parameters. Those smoothed probabilities are discarded. Exported features are `P(S_t | observations through t)` from a forward filter. Parameters for a block are fit on observations strictly before the block. Block length is 60 valid observations after a minimum of 252. Scaling mean and standard deviation are fit on that same prefix. Variance is floored at 1e-3. A state with posterior mass below 2 percent, a non-converged run, or a non-finite parameter rejects that seed. If no seed survives, E is BLOCKED.

The exported JSON is the last successful block. Inference code imports neither hmmlearn, scipy, nor sklearn. A stored posterior advances only when the model hash matches, the session is the next session in the declared calendar, and the session was not already applied.

## Earnings

Reaction, using beta and ATR20 estimated before the event:

`I = (stock_return - beta_prev * index_return) / (ATR20_prev / close_prev)`

Release time before 09:15 IST on a trading session: that session is the reaction session. During 09:15–15:30, after 15:30, or on a non-trading day: the next trading session. Entry is after a later confirmation window, never on a bar that was not yet observable. Daily bars do not provide VWAP. Any intraday price used as a range midpoint is labelled `daily_range_midpoint_proxy`. Without a verified announcement archive this experiment returns BLOCKED and no returns.

## OU

On a market-only residual level, estimate `delta = a + b * level_lag` on the 250 sessions before the decision, without demeaning the level first. `phi = 1 + b` must sit strictly between 0 and 1. Half-life `log(2) / -log(phi)` must sit between 5 and 60 sessions. The regression t statistic on `b` must be at or below -2.86, a frozen approximate 5 percent Dickey-Fuller critical value with a constant. This is a screen, not a proof of cointegration. Equilibrium variance is the regression error variance divided by `1 - phi^2`. Entry, next open, when the level is at least 1.5 equilibrium standard deviations below the estimated mean. Exit at the mean or at a vertical barrier of twice the entry half-life, rounded up, fixed at entry. P&L is the unhedged stock. The family is not written into `config/ops/promotion_approval.json`.

## Fundamental filter

The production conjunction stays AND: 0 < price/trailing EPS < 30, annual ROE > 20, trailing EPS > 0, annual profit growth above 20 percent with both years profitable, and latest disclosed FII ownership above the previous consecutive quarter. FII is a lagged ownership proxy. Filtered and unfiltered research are compared only on dates where a point-in-time snapshot exists for the same names. This cache does not have that history, so the comparison is BLOCKED.

## Reporting

For each experiment that runs: sample span, data status, candidates, admitted orders, completed fills, unique names, net completed P&L, expectancy, profit factor, marked drawdown, Brier and log loss where a probability exists, 10 bps base slippage and a 25 bps stress that only changes the slippage input, capital utilization, non-overlapping 14-day windows with at least two unique admitted entries or exits, and the fraction of those windows with none. Overlapping windows are descriptive only. Uncertainty is a 400-draw bootstrap of ISO-week blocks of completed net R, seed 20261026. Concentration is the largest absolute-P&L name share.

Commands, after the implementation exists:

```bash
python -m pytest -q
python -m research_v6 --out artifacts/research_v6
```

The runner writes under `--out` only. It refuses paths that look like broker or paper books. It does not deploy, notify, or update promotion approvals.
