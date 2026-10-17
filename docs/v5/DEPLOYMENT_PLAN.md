# v5 deployment plan — review only

Nothing in this plan has been applied. The candidate is not a promoted strategy. Deploying it would replace the live seven-feature path with an unpromoted 20-or-15 feature contract and pause new BUYs.

## Current production, last recorded 7 October 2026

- Function `indian-swing-bot`, Python 3.12, eu-north-1, timeout 300s, memory 1024 MB.
- Code sha256 `TOeunud6CMQm+iDnmWNa0FH30+gp5RedddyS8JPxgJg=`.
- Model `models/model.json` sha256 `fac68a53e0776ac1be739d2c08d5c15981a04150d73c0bfa5fdcbfa09dab1cb9`.
- Schedules stay `swing-recommender-daily` at 09:25 IST and `swing-holdings-monitor` at 15:05 IST.
- Do not change those rules in this step.

## Migration

1. Read `portfolio_state.json` with its ETag. Do not upload `fixtures/v5/broker_snapshot_2026-10-17.json`.
2. Diff cash, quantities, and average costs against the 17 October reference: cash ₹5,748; HAL 16 @ 5040.63; ZENTEC 10 @ 1669; CDSL 8 @ 1363.75; BPCL 6 @ 318.65. INFY should be absent. Missing INFY and ZENTEC fills stay unknown.
3. ZENTEC is `PLAN_RECONCILIATION_REQUIRED`. The 3/3/4 split is a proposal, not an applied plan.
4. Add sleeve fields only through an explicit migration: `launch_nav`, `external_inflows`, `legacy_transfers`, `circuit_latched`. A deposit must not clear a −₹1,500 latch.
5. Write with `If-Match`. On conflict, reload. Keep marks in a separate object from the fill ledger.

## Proposed later change, not created

An after-close ingest rule could prepare the 150-name feature snapshot. It is not an EventBridge change in this branch. The 09:25 run should read that snapshot and only request current quotes.

## Package

`python -m runtime_v5.package_check` writes `dist/v5_candidate.zip`. It contains `swing_v5` and `runtime_v5` only. sklearn and xgboost stay out. The zip was not uploaded.

## Rollback

Restore the code sha256 and model sha256 above. Do not rewrite the broker book as part of a code rollback.

## Activation gate

Reconcile the live book, certify TradingView coverage from Lambda, collect 60 untouched forward sessions, and pass the promotion floors in `config/v5/research_policy.json`. Until then every family stays research and output is WATCH.
