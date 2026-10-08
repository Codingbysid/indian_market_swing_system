# Control-only candidate

This package is the review build for the live-control fixes. It is not deployed.

The live contract stays the existing seven-feature model and `ema_cross` as the only BUY family. `trend_pullback`, `range_breakout`, `relative_strength_momentum` and `failed_breakdown_reclaim` remain WATCH. `residual_shock_reversion` remains research-only. The twenty-feature artifact, when one exists, is `backtest/artifacts/v3/model.candidate.json` and is not the production key `models/model.json`.

## What the candidate changes

- Missing or corrupt portfolio state is `PORTFOLIO_INVALID`. There is no ₹24,000 fallback and no BUY.
- Stale marks do not replace a complete equity figure, and stale equity blocks sizing.
- Portfolio ledger and mark snapshots are separate versioned documents. A stale version is rejected.
- Open and unexpired pending stressed risk both reduce the aggregate budget. Expired reservations do not.
- Ticket quantity includes entry fees, slippage and a 0.5 ATR adverse-gap allowance. The displayed cap and the cap used for quantity are the same repair ceiling.
- Legacy holdings stay on the profit-only ladder. New `system_atr` lots use the frozen stop, target and 10-session exit. A trigger is not a fill.
- Dry runs set `emit_alerts` and `write_state` to false.
- A twenty-feature model is rejected by the live scorer.

## Migration dry run

`swing_core.migration.migration_dry_run` reads the 7 October 2026 snapshot and returns a plan. It does not write S3 or the local book. TIMEX is not restored. All five holdings stay `legacy_profit_only`.

## Deploy, only after a separate authorization

1. Build with `python scripts/package_control_candidate.py`.
2. Confirm `dist/control_candidate.zip` has no sklearn or xgboost.
3. Update Lambda code only. Do not change EventBridge rules `swing-recommender-daily` or `swing-holdings-monitor`.
4. Do not upload `portfolio_state.json` and do not replace `models/model.json` with the research candidate.

## Rollback

Restore Lambda code sha256 `TOeunud6CMQm+iDnmWNa0FH30+gp5RedddyS8JPxgJg=` from the 7 October 2026 snapshot. Leave `models/model.json` at sha256 `fac68a53e0776ac1be739d2c08d5c15981a04150d73c0bfa5fdcbfa09dab1cb9`. Do not rewrite the live book as part of a code rollback.

## Remaining control risks

- The NSE holiday list is not bundled, so a holiday gap fails closed and can look one session staler than the exchange calendar.
- Correlation at 0.65 is enforced only when a candidate supplies `corr_to_book`. The live scan does not yet compute that correlation from prices.
- This candidate is not what production is running until the zip is actually updated.
