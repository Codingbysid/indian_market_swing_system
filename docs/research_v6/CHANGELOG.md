# Change log after 53e6751

The previous draft separated gross paths, reference trades, and the ₹5,748 account. These measurement corrections change some reported model numbers. They do not change the account result.

- Fold test metrics no longer include a later period through the old global "test" flag. The 2023 logistic test Brier is 0.254 on signals from 2023-07-03 to 2023-12-29, not the mixed figure from 53e6751.
- Residual ridge is chosen inside each fold's training window. The 2023 and 2024 folds select lambda 10. The 2025 fold still selects lambda 1, so its residual-versus-raw comparison is unchanged.
- A paired week-block interval for sector residual minus matched raw includes zero in 2025 (+0.061, -0.054 to +0.161) and is negative in 2023 (-0.278, -0.385 to -0.151). Separate intervals are not used as a difference test. Removing Financial Services flips the 2025 point difference negative.
- HMM remains NOT EVALUATED. At observation 492, all six fits were still moving after 40 iterations. Last state shares were above 2 percent and variances were not collapsed. The iteration cap was not raised.
- ₹5,000 is initial price risk per reference trade. Median reference notional in the 2025 raw list is about ₹1.15 lakh, the 90th percentile about ₹1.53 lakh, and the maximum about ₹2.32 lakh. Peak overlapping notional is about ₹1.40 crore. Those sums are not a funded portfolio.
- The account replay is partial: correlation, defence, and PSU caps are still not enforced. It still admits no raw-momentum orders.
- Default runs read the archived sector files and do not download a new vintage.

What did not change: market residual sum and the volatility-normalized score still trail matched raw momentum on the 2024 and 2025 folds. The meta-model still selects the constant prior for 2024 and 2025. The frozen account still has no executable quantity under the tested constraints.
