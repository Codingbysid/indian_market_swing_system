# Corrections after the 20be013 review

These rules were written before the corrected run. They do not retune the frozen risk policy.

The zero-order account result in `20be013` shows that the hypothetical ₹5,748 policy could not admit the tested candidates. It is not an estimate of signal alpha, because those candidates were rejected before a label existed.

## Three recorded populations

- Gross paths: next-open execution checks, gross price returns, MAE and MFE, time exits, and censoring. No fees and no account limits. Hypothetical diagnostics, not orders.
- Reference trades: integer quantity `floor(5000 / (1.5 * ATR20))`. Fees are charged at that quantity. The ₹5,748 admission filter is not applied. Each row records whether that quantity would have failed the frozen account. This population is not a set of admitted portfolio fills.
- Frozen account: unchanged `config/v5/research_policy.json`, launch cash ₹5,748, sticky ₹1,500 loss latch, cash reserve, fee-and-gap stressed risk, no second position in the same symbol. Zero admissions remain a valid account result.

## Folds

Embargo is 10 sessions, the vertical horizon. A trade whose exit crosses the next boundary is purged.

| Fold | Train through | Calibration | Test |
| --- | --- | --- | --- |
| fold_2023h2 | 2022-12-31 | 2023-01-01 to 2023-06-30 | 2023-07-01 to 2023-12-31 |
| fold_2024h2 | 2023-12-31 | 2024-01-01 to 2024-06-30 | 2024-07-01 to 2024-12-31 |
| fold_2025 | 2024-12-31 | 2025-01-01 to 2025-06-30 | 2025-07-01 to 2026-10-08 |

The cache remains reused development data. There is no untouched holdout.

## Residual scores

`residual_momentum_market_only` remains the sum of the 60 out-of-sample prediction errors.

`residual_momentum_vol_normalized` is that sum divided by `max(sample standard deviation of those same errors, 1e-4)`. The floor is a constant, not estimated from test returns. Both are compared with raw momentum on the names that have both scores that day.

Ridge standardizes the factor columns inside each fit window. The penalty applies to standardized slopes only. Lambda in {0.1, 1, 10} is chosen by training mean squared prediction error.

## HMM coverage

Breadth denominator stays 150. Missing names are not dropped from it and are not imputed.

A session is invalid when fewer than 90 percent of the 150 names have a finite close and a finite return. The full-cache experiment stays blocked when more than 5 percent of checked sessions are invalid.

A separate study is allowed only on the longest suffix that ends on the last cache session, contains at least 252 sessions, and has no invalid session inside it. That suffix is chosen from coverage alone. HMM and the EMA50 baseline are compared only on that suffix, and only if it exists.
