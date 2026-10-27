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

The replay reserves pending debit, keeps the loss latch after a later recovery, refuses a second position in the same symbol, and includes a candidate's proposed sector exposure before admission. Held exposure is the marked value. Pending exposure is the reserved debit. This is a partial account replay: pair correlation is not enforced, and defence and PSU caps are not applied because these signals have no theme tag. Missing closes are marked at the last known price and are not fills. `largest_quantity` still searches at 10 bps because that helper is shared production code and was not changed.

## Sector comparison

The 2025 sector residual mean net R of 0.290 and the matched raw mean of 0.224 do not, by themselves, show a difference. The paired comparison uses the same eligible names and signal weeks. Each week contributes the difference of the two mean net R values. Weeks are resampled together, so names that signaled in the same week stay in one block. Reference paths still overlap in calendar time; a hold that crosses two signal weeks can be split. That remaining dependence is part of the interval, and the result stays exploratory on a reused cache.

| Fold | Common weeks | Sector minus raw | Week-block interval |
| --- | ---: | ---: | --- |
| 2023 H2 | 6 | -0.278 | -0.385 to -0.151 |
| 2024 H2 | none | no eligible names | — |
| 2025 | 10 | +0.061 | -0.054 to +0.161 |

The 2025 interval includes zero. Dropping Financial Services changes that fold's point difference from +0.061 to -0.257. The largest single name, HINDCOPPER, is about 12 percent of absolute reference P&L. The 2023 interval is entirely negative. Sector residual momentum is not established as better than matched raw momentum.

## Reference size

₹5,000 is the initial price risk of one reference trade, not the cash spent and not the account. In the 2025 raw list the median notional is about ₹1.15 lakh, the 90th percentile about ₹1.53 lakh, and the maximum about ₹2.32 lakh. The peak sum of notionals open on the same day is about ₹1.40 crore. Those overlapping paths are independent hypotheticals. They are not a portfolio that was funded.

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

The residual sum and the volatility-normalized score both trail matched raw momentum on the 2024 and 2025 folds. Ridge lambda is now chosen inside each fold. The 2023 and 2024 training windows select 10. The 2025 window selects 1, which is why that fold's residual comparison matches the earlier lambda-1 run. The three training errors still differ only near the eighth digit. A fixture shows a larger lambda shrinks the slope.

The 2025 sector residual interval by itself is above zero, and the matched raw interval includes zero. That pair of statements is not a test of the difference. The paired result is in the section above. Hysteresis did not improve the raw list. The pullback variant had 11 completed reference trades and a high point estimate in 2023, then negative results on 18 and 42 trades in the later folds.

The 2023 meta-model test set is only 2023-07-03 through 2023-12-29. Its logistic test Brier is 0.254. The 2024 and 2025 folds still select the constant prior. The 2025 prior's test Brier is 0.242. This is not evidence for a trading overlay.

## HMM, earnings, OU, fundamentals

Full-sample HMM coverage is blocked. Of 2,000 Nifty sessions, 690 fail the 90 percent rule with denominator 150. The failures are mostly pre-listing history in the cache (19,853 name-sessions) and SMA50 warmup (13,631), plus 170 missing vendor bars and 2,000 name-sessions for ZENTEC, which has no file. Nothing was imputed.

The coverage suffix from 2021-06-29 through 2026-10-08 meets the predeclared length and coverage rule. The causal fit is NOT EVALUATED. At observation 492, all six seed and covariance runs were still moving after the predeclared 40 iterations. The last state shares were about 18 to 50 percent, so none had fallen through the 2 percent occupancy floor, and the smallest variance was about 0.13. One diagonal fit had a single near-zero transition. The input hash prefix is `fe721bcc3b36`. The iteration cap was not increased, and missing posteriors were not filled. The argmax gate and the posterior meta-model were not run. Production RISK_OFF was not changed.

Earnings remain an announcement reaction, not SUE. Of 11,771 financial-result board outcomes, 135 completed a reference-budget path after the reaction and consolidation rules. Mean net R was -0.221, profit factor 0.626, week-block interval -0.461 to +0.030, 87 names. Seventeen confirmations did not fill. Admission stays blocked because there is no point-in-time fundamental snapshot. Clocks in the file were read as IST.

The OU screen is research-only. Most windows failed the stationarity, phi, or half-life checks. Eighteen one-share paths completed, six names, mean net R -0.650, profit factor 0.210. The interval includes positive values. Those paths are not the ₹5,000 reference book and were not admitted to the account. The family was not promoted.

Fundamentals stay an AND of the five existing conditions. No comparison was run.

## What is not claimed

Gross returns are not a profitable strategy. Account rejections are not a proof of no alpha. Nothing here meets the promotion rules or changes the live risk policy.
