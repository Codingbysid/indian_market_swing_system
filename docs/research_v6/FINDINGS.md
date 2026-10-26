# Research v6 findings

No experiment improved on a costed baseline. The frozen ₹5,748 research policy admitted zero orders in every account replay, including the raw-momentum baseline. The one-share paths were also rejected by the existing 25 percent cost filter, so there was no completed-trade sample for a meta-model. This is a negative feasibility result. It is not a reason to loosen fees, risk caps, or evidence rules.

The prices are the reused TradingView cache from 2018-09-11 through 2026-10-08, 149 of the 150 current universe names. ZENTEC is missing. That cache was already used in earlier research. The outer window from 2025-07-01 is a holdout inside reused data, not an untouched forward test. The quoted v4 Brier figures and the ₹1,294.58 rules loss were not recomputed: the v4 event file is gitignored. Those numbers stay a prior reference. The v3 metrics file is a different study and was not used as this baseline.

## Account layer

`largest_quantity` under `config/v5/research_policy.json` found no admissible share for the candidate prices and ATR values that reached the replay. The binding combination is the ₹15.34 DP charge, the 25 percent fee-to-initial-risk cap, the about ₹86 trade-risk cap on ₹5,748, and the 0.25 ATR entry-band stress. Moving a target farther away does not change that initial-risk ratio. Kelly stayed off. Every outer-test fortnight in the account replays had zero admitted entries and zero exits. Two-or-more unique intents occurred in none of the 34 non-overlapping 14-day windows.

| Experiment | Outer-test candidates | Admitted | Completed fills | Net P&L |
| --- | ---: | ---: | ---: | ---: |
| A raw momentum, EMA50 regime | 2,512 | 0 | 0 | 0 |
| B meta-model | blocked | 0 | 0 | — |
| C market residual | 2,520 | 0 | 0 | 0 |
| C raw momentum on that same set | 2,520 | 0 | 0 | 0 |
| C sector residual | 450 | 0 | 0 | 0 |
| C raw momentum on the sector set | 450 | 0 | 0 | 0 |
| D two-session hysteresis | 2,557 | 0 | 0 | 0 |
| H2 pullback and compression, logged only | 49 | 0 | 0 | 0 |
| E HMM features | blocked | 0 | 0 | — |
| F earnings reaction | 152 confirmations | 0 | 0 | 0 |
| G OU | 154 statistical entries | 0 | 0 | 0 |

One-share labelling of A produced 15,845 rejected paths and no completed, unfilled, or censored paths. The market-residual and sector-residual lists were the same: every path was rejected before a fill. There is therefore no profit factor, no Brier score, and no week-block interval to report for a costed book.

## What each component did

Residual ridge on the training windows chose lambda 1.0. Lambdas 0.1, 1, and 10 had mean squared prediction errors of about 0.00043524 to 0.00043529. The ranking is real and the economic difference is negligible. Sector indexes that parsed were auto, pharma, FMCG, metal, energy, IT, realty, and Nifty Financial Services. `^CNXFIN` did not parse. Names in sectors without a parsed index, including power, chemicals, telecom, capital goods, and several others, were excluded from the sector experiment. They were not silently scored with a market-only model. The market-only residual experiment is the separate row above.

The meta-model did not fit. The training rule requires 30 completed unit-quantity trades, and there were none. Separate strategy experts were not estimated. XGBoost was not fit. Shrinkage was not applied to a live score. The code paths for the prior, the pooled logistic model, and shrinkage were exercised by fixture tests.

The HMM was blocked before a trading feature was used. Breadth uses a fixed denominator of 150, which keeps ZENTEC in the denominator when its file is missing. On 32.1 percent of Nifty sessions fewer than 90 percent of those 150 names had a usable bar or return. The frozen rule blocks the experiment when that fraction exceeds 5 percent. The rule was not relaxed after seeing the 32.1 percent. Production RISK_OFF is unchanged. No HMM JSON was exported.

The NSE corporate-announcement download for 2025-07-01 through 2026-10-08 is a real JSON list, sha256 `ed5f7cc510a0a7009a1619fd98a3b4ba6704bee6b8b751c1e612ca11761506b5`, 167,728,012 bytes. It is not committed. Of 11,771 board-outcome filings whose text states financial results, 11,047 symbols are outside the 149-name price panel. Three reaction sessions could not be aligned. 280 filings had a positive abnormal reaction and a volume impulse of at least 1.5. 152 also stayed inside a three-session ATR band. All 152 were then rejected by the one-share cost filter. None were admitted, because no point-in-time fundamental snapshot exists. This is not an earnings-surprise or SUE study. Daily bars were not treated as VWAP. Disclosure clocks in the file have no timezone; they were interpreted as IST.

The OU screen rejected 39,325 windows for the stationarity t-statistic, 3,564 for phi outside (0, 1), and 50 for a half-life outside 5 to 60 sessions. The 154 windows that passed and were far enough below the estimated mean were rejected before a fill. The family was not added to the promotion file.

Fundamental filtered-versus-unfiltered performance is BLOCKED. `config/ops/fundamentals_snapshots.json` has no historical rows. The conjunction remains AND. FII ownership remains a lagged disclosure proxy.

## What this does not say

It does not say the raw or residual rankings are profitable before costs. Those paths were not allowed to ignore DP and the cost cap. It does not say the v4 loss of ₹1,294.58 was reproduced or overturned. It does not support promotion, a live regime change, or a claim of two trades a fortnight.
