# Corrected research v6 findings

The earlier zero-order write-up treated account infeasibility as if the signals had been evaluated. That was wrong in two ways. The price bars used by the path simulator were stored under one literal key, so gross and one-share paths never saw a symbol's prices. And even a correct account rejection is not an alpha estimate. This note replaces that conclusion.

The cache is still reused development data from 2018-09-11 through 2026-10-08, 149 names, ZENTEC missing. There is no untouched holdout. The v4 Brier figures and the ₹1,294.58 rules loss remain an unreproduced historical reference because the event file is gitignored. Prior artifacts are in `artifacts/research_v6_superseded_20be013/`.

## Frozen account

Every outer-test candidate was rejected before a fill. For the 2,512 raw-momentum candidates in the 2025 fold, the feasibility file shows quantity 0 on every row. One example, ABCAPITAL on 2025-07-01:

- quantity 0
- gap allowance ₹1.96
- one-share fees ₹16.53
- one-share price risk ₹11.73
- cost cap ₹2.93
- spendable cash ₹4,598.40 after the 20 percent reserve
- trade-risk cap ₹86.22 and aggregate room ₹172.44

The failed line is `no_integer_quantity`. All 34 non-overlapping fortnights in that window had zero admitted entries and zero exits. The same rejection, `risk_or_cost`, covers the residual, hysteresis, and pullback lists. This is account infeasibility. It is not evidence that the signals have no edge, and it is not evidence that they have one.

The replay now reserves pending debit, keeps the loss latch after a later recovery, refuses a second position in the same symbol, and counts fee-and-gap stress in aggregate risk. It does not model pair correlation. Defence and PSU caps are not applied unless a tag is present, and these signals do not carry those tags. Missing closes are marked at the last known price and counted; they are not fills. `largest_quantity` still searches at 10 bps even when a stress run asks for 25 bps, because that helper is shared production code and was not changed.

## Gross paths and reference trades

Gross paths are price returns with no fees. Reference trades use quantity `floor(5000 / (1.5 * ATR20))` and charge fees at that quantity. Those rupee totals are not ₹5,748 account results. The meta-model, where it fits, is trained on the reference trades and is labelled that way.

Latest fold, signals from 2025-07-01 through 2026-10-08. Week-block intervals are for reference-trade net R.

| Comparison | Gross paths | Mean gross return | Reference trades | Mean net R | Week-block interval | Account fills |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| Raw momentum | 2,229 | -0.0032 | 2,229 | -0.167 | -0.318 to -0.012 | 0 |
| Residual sum | 2,252 | -0.0050 | 2,252 | -0.228 | -0.379 to -0.058 | 0 |
| Raw, same names as the residual sum | 2,237 | -0.0034 | 2,237 | -0.173 | -0.322 to -0.013 | 0 |
| Residual volatility score | 2,234 | -0.0039 | 2,234 | -0.221 | -0.356 to -0.051 | 0 |
| Sector residual sum | 377 | +0.0161 | 377 | +0.290 | +0.012 to +0.524 | 0 |
| Raw, same sector names | 372 | +0.0148 | 372 | +0.224 | -0.069 to +0.578 | 0 |
| Hysteresis | 2,290 | -0.0028 | 2,290 | -0.159 | -0.311 to -0.008 | 0 |

The residual sum and the volatility-normalized score both lost to matched raw momentum on this fold. They also lost on the 2024 fold. The 2023 fold was positive for raw and for the residual sum, and the raw list was stronger there too. Ridge lambda 1.0 won the training mean-squared-error comparison. Lambdas 0.1, 1, and 10 differ only near the eighth digit of that error. A fixture confirms a larger lambda shrinks the slope, so the search is not a no-op. The prediction error is dominated by residual variance, which is why the three errors are close.

The 2025 sector residual interval is above zero, on 25 names and 12 week blocks. The matched raw interval includes zero. The 2023 sector fold was negative, and the 2024 sector test window had no eligible names under the EMA50 gate. That is not a demonstrated strategy.

Hysteresis did not improve the raw list. The pullback variant had 11 completed reference trades and a high point estimate in 2023, then negative results on 18 and 42 trades in the later folds.

The meta-model selected logistic on the 2023 fold, where calibration Brier was 0.2499 against a prior of 0.2519. On the 2024 and 2025 folds the calibration choice was the constant prior. Test Brier on the 2025 reference trades was 0.242 for that prior. The logistic and XGBoost controls were slightly worse in calibration on those later folds. This is not evidence for a trading overlay.

## HMM, earnings, OU, fundamentals

Full-sample HMM coverage is blocked. Of 2,000 Nifty sessions, 690 fail the 90 percent rule with denominator 150. The failures are mostly pre-listing history in the cache (19,853 name-sessions) and SMA50 warmup (13,631), plus 170 missing vendor bars and 2,000 name-sessions for ZENTEC, which has no file. Nothing was imputed.

The coverage suffix from 2021-06-29 through 2026-10-08 meets the predeclared length and coverage rule. A causal HMM fit on that suffix failed at observation 492 because no seed produced an acceptable model. The baseline-versus-HMM comparison on that window was not run. Production RISK_OFF was not changed.

Earnings remain an announcement reaction, not SUE. Of 11,771 financial-result board outcomes, 135 completed a reference-budget path after the reaction and consolidation rules. Mean net R was -0.221, profit factor 0.626, week-block interval -0.461 to +0.030, 87 names. Seventeen confirmations did not fill. Admission stays blocked because there is no point-in-time fundamental snapshot. Clocks in the file were read as IST.

The OU screen is research-only. Most windows failed the stationarity, phi, or half-life checks. Eighteen one-share paths completed, six names, mean net R -0.650, profit factor 0.210. The interval includes positive values. Those paths are not the ₹5,000 reference book and were not admitted to the account. The family was not promoted.

Fundamentals stay an AND of the five existing conditions. No comparison was run.

## What is not claimed

Gross returns are not a profitable strategy. Account rejections are not a proof of no alpha. Nothing here meets the promotion rules or changes the live risk policy.
