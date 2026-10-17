# Four-screen swing rebuild v5
Design frozen 17 October 2026. This package specifies the research and implementation; it does not contain a newly validated strategy or deployed bot.

Start with [STRATEGY_SPEC.md](STRATEGY_SPEC.md), then give [CURSOR_IMPLEMENTATION_PROMPT.md](CURSOR_IMPLEMENTATION_PROMPT.md) to Cursor.
Machine-readable policy: ../../config/v5/research_policy.json
Broker reference: ../../fixtures/v5/broker_snapshot_2026-10-17.json

## Confirmed user choices
Two horizons (3–15 sessions and 2–8 weeks), free data only, at most150 unique equities including held/pending stocks, one shared cash budget, profit-only existing holdings, loss exits on new trades, cumulative new-trade loss pause at₹1500, no pending orders.
Cash₹5748; HAL16, ZENTEC10, CDSL8, BPCL6.

## Checked in this design pass
- Read actual queries on all four linked Screener screens.
- Verified Screener documents no public API and NSE publishes daily price/delivery reports.
- Inspected v4 branch identity and prior negative research evidence.
- Read both current broker screenshots and incorporated user confirmations.
- Parsed both JSON artifacts and checked book totals, reserve/ticket/risk arithmetic, universe cap, embargo vs horizon and unpromoted flags.
- Expected initial values: total equity114188.20, reserve1149.60, max position debit2299.20, stressed trade risk86.22, aggregate172.44.
These ceilings deliberately may reject all available opportunities if fees/stop distances cannot fit. They are not empirically optimized or proof that two positions are executable.

## Not established
No new v5 backtest, model training, AWS ingestion/latency test, fill verification, live migration or deployment was performed in this design pass. v4's64 passing tests are not v5 implementation tests. Published source availability does not certify an API or automated AWS access. Historical fundamental membership remains unverified.
Cursor must implement the listed commands before running them. Existing v4 code remains unchanged on this design branch. Use current broker reconciliation before any future activation.

## Key design differences
Four screens provide dated eligibility, not automatic BUYs. Rules are tested before optional ML. Historical price-only controls remain distinct from forward fundamental tests. Kelly starts disabled; qualification is mandatory for fixed-risk rules too. No automatic promotion to satisfy the activity goal. New small positions exit in full; legacy tranches preserve policy and actual fee-aware profitability.
