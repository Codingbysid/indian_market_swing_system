# Capital feasibility decision

Date: 18 October 2026. Active policy is unchanged. No deposit is assumed. Cash reference remains ₹5,748. The ₹1,500 new-sleeve loss latch stays in every scenario.

## The bound

Let R be quantity times the entry-to-stop distance, F the round-trip cost including slippage, and G the extra stressed gap loss.

The reference rules are F ≤ 0.25R and R + G + F ≤ 86.22.

F ≤ 0.25R is the same statement as R ≥ 4F. Substituting that minimum R into the risk cap gives a stressed loss of at least 5F + G. The implemented cost model matches this identity on a case that clears the filter (`tests/test_v5_capital.py`).

F includes a ₹15.34 DP charge once per scrip sale, not once per share. On a one-share round trip at ₹200 that fixed charge is most of the roughly ₹16 fee. An ordinary ₹20 stop has R = 20, so F is already above 0.25R. The filter therefore rejects the trade before the ₹86.22 cap is the interesting constraint. Buying more shares spreads the DP charge, but the ₹2,299 debit cap and the ₹86.22 risk cap run out before an ordinary ATR stop can both clear the filter and fit the risk budget.

This is not a recommendation to use a ₹65 stop on a ₹100 share. That point can satisfy the arithmetic and is still a poor swing stop. The synthetic cases below use 1.5 ATR and 2 ATR floors, which are the strategy's own minimum distances.

## What was measured

Structural stops already stored on the 13 corrected long fills that also have a gap estimate: **0 admitted** under the reference caps. Six fail the cost filter and seven fail the trade-risk cap. The stop was not widened.

Synthetic 1.5 ATR and 2 ATR floors at ₹100, ₹300, ₹800 and ₹1,500, with a 0.5 ATR gap: **0 admitted**. The cheap and mid prices fail the cost filter. The ₹1,500 long floor fails the ₹86.22 risk cap.

One-share results on the reused development cache, before any new sizing:

| Horizon | Fills | Names | Gross price R | Net R after costs |
| --- | ---: | ---: | ---: | ---: |
| Long | 15 | 14 | −0.36 | −1.30 |
| Short | 35 | 27 | −0.23 | −2.03 |

Gross price expectancy is already negative. Costs make it worse. The long one-share net loss is about ₹503.

## Predeclared grid

Two positions, 20% cash reserve, and the ₹1,500 latch stay fixed. Aggregate stressed risk is twice the trade ceiling. Rule B, admission from validated expected net P&L, was not run: there is no validated payoff. It stays research-only.

| Trade ceiling | Full losses to ₹1,500 | Cost filter | Admitted long fills | Net P&L | Profit factor |
| --- | ---: | --- | ---: | ---: | ---: |
| ₹86.22 reference | 17.4 | on | 0 | 0 | — |
| ₹86.22 | 17.4 | off | 6 | −₹179 | 0.43 |
| ₹100 | 15.0 | on | 0 | 0 | — |
| ₹100 | 15.0 | off | 8 | −₹220 | 0.58 |
| ₹125 | 12.0 | on | 2 | −₹212 | 0.00 |
| ₹125 | 12.0 | off | 8 | −₹313 | 0.49 |
| ₹150 | 10.0 | on | 4 | −₹417 | 0.00 |
| ₹150 | 10.0 | off | 9 | −₹526 | 0.36 |

A gap through the stop can lose more than the estimate, so those "full losses to the latch" counts are not a guarantee. Every scenario that admits trades loses money. The setting with the most trades loses the most. None is qualified.

## Candidate, inactive

`config/v5/candidate_policy.json` is inactive. The recommendation is to keep the reference caps. Raising the ceiling or dropping the cost filter would only let a losing price-only sample lose more of the ₹1,500 budget. Do not treat that as a policy improvement.

## Forward work that can run now

```bash
python -m backtest.v5.forward --as-of YYYY-MM-DD
```

This appends to `artifacts/v5/forward/observations.jsonl` on the local disk. It records today's screen status when a provider file exists, marks quality and value observations blocked until a snapshot is archived, and does not write S3 or send alerts. Holiday, corporate-action, and restriction metadata are still missing, so unknown status is not treated as eligible.
