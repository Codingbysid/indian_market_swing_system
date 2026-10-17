# Cursor handoff: implement the four-screen swing brain v5

You are the implementing quantitative engineer. Build the specification in this branch into a reproducible research engine and a reviewable AWS integration candidate. The user has AWS/notification access in this environment. Do actual implementation and testing, not just more documentation. No trade or performance claim without artifacts.

## Bootstrap
Repository: https://github.com/Codingbysid/indian_market_swing_system
Design branch: strategy-v5-blueprint
Design source: docs/v5/STRATEGY_SPEC.md, config/v5/research_policy.json, fixtures/v5/broker_snapshot_2026-10-17.json.
Read applicable AGENTS.md if present. Inspect branch state and git diff before editing. Create implementation branch strategy-v5-implementation from the design branch; preserve user edits.
The design branch descends from quant-v4-india150 / 34ce678c3033507c9b43d264bf293d1f5907db4e. Existing v4 code and evidence are REFERENCE, not a qualified model or a production deployment.
Read docs/QUANT_V4_INDIA150_REVIEW.md and backtest/artifacts/v4/metrics.json before changing anything. Do not cherry-pick only favorable research.

## Non-negotiable user contract
Cash5748. Legacy: HAL16@5040.63,ZENTEC10@1669,CDSL8@1363.75,BPCL6@318.65; no pending orders/GTT; TIMEX inactive. This is a17Oct reference, not assumed current forever. INFY absent; missing transactions must not be invented.
Existing four profit-only exits, no averaging. NEW system lots allow loss exits. CNC only, manual execution, no derivatives/leverage.
Research3–15sessions and2–8weeks separately, but share capital/reservations/position limits. Maximum150 unique equities across both families/cohorts/held/pending; free data only. S3 is live broker-book source of truth.
Cumulative new-sleeve net loss<=-1500 latches no-new-BUY. External cash transfers cannot reset/offset this; legacy losses excluded. Continue protective exits.
Initial proposed caps from design: reserve1149.60; position debit2299.20 includingbuyfees; stressedtrade86.22; aggregate172.44; max2new/pending positions. Configurable policy values, not research conclusions.
Two distinct BUY/SELL signals per fortnight is a measured objective, never a quota override. WATCH/reminders don't count.

## Phase 1: inventory and provider feasibility
Inspect live AWS configuration read-only: function/layers/environment variable NAMES, model/schema/config versions, EventBridge payloads, notification interfaces, S3 state schema and permissions. Never print secrets. Record deployed code hash and model hash; do not assume main equals Lambda.
Implement provider interfaces under swing_v5/data/. Probe actual historical/date-specific report payloads from AWS or matching network, not only homepages. Produce artifacts/v5/provider_health.json with date, schema, coverage, source restrictions, failures and latency.
Screener has no API. Implement complete permitted pagination with snapshot hashes OR offline CSV/HTML import; no paid-plan dependence or auth bypass. Four sources exact URLs from spec. All-page completeness required. Date fundamental eligibility by first known publication/observation.
NSE UDiFF bhavcopy + delivery: implement bulk fetch/cache, strictschema/date validation, historical-format dispatch, ISIN/exchange symbolmapping, holiday calendar and corporate actions. Verify any URLs empirically rather than guessing and declaring success.
Preserve TradingView as livequote adapter. Failure means invalid data, not Yahoo live fallback. Research alternative vendor must be explicitly configured and never mixed without parity evidence.
Fundamental screen backtests with current membership are prohibited as genuine historical tests. If historical snapshots unavailable, implement forward collection and price-only retrospective evaluation; report the limitation clearly.

## Phase 2: pure decision engine
Implement new namespace swing_v5/ with small interfaces:
- types.py: immutable dated bar/screen/event/model/signal/lot schemas; enums/reasoncodes.
- universe.py: monthly <=150 selection, held/pending pinning, source tags, liquidity/instrument filters; no hidden larger research comparison universe.
- features.py: versioned18core schema and price-onlyschema omitting unavailable membership;20optional schema only when verified. Adjusted indicators vs raw executionprices.
- strategies.py: exact Q/T/V hypotheses and short/long exit contracts from spec; registryversions and no automatic promotion.
- regime.py: baseline/challenger identical data alignment; no retrospective gate tuning.
- execution.py: bounded entry window/band, integerqty, unfilled/gap handling, same-bar ambiguity, exchange-session vertical barriers.
- costs.py: effective-dated delivery tariffs, brokerage/DP aggregation/slippage, fee allocation; reconcile examples to broker calculator/ledger.
- risk.py: shared NAV/cash/fees, caps, sector/theme/correlation, current-mark remaining risk, cumulativeflow-adjusted loss circuitbreaker.
- portfolio.py: legacy/system separation, brokertruth, immutabletranches, fillidempotency, pendingreservations and invalidstate failclosed.
- sentiment.py: timestamped source/event risk overlay, optional backend adapter; no news-generated price targets or auto-positive quantity increases.
- decision.py: deterministic evaluate(snapshot,book,policy,registry)->intents/rejections with no network or writes.
- scorer.py: pure-Python JSON logistic/XGB, precise schema/calibration contract.
- health.py: data/calibration/drift/fillcost reports, minimum sample and maturity rules; unknownhealth isn't silently green.

Reuse tested v4 utilities only after contract tests establish compatibility. Do not keep two conflicting risk engines wired to the live path.
Keep funding flows separate from tradeP&L. Add launchNAV, externalflows, legacy-transfers, liquidatablemarks, circuitbreakerlatched fields via explicit migration. A screenshot is no historicalfillledger.
Resolve ZENTEC10 trancheplan from actualrecords; if unavailable produce reviewneeded status and proposed3/3/4, never reset previousfills.
Legacypartialexit must exceed both policyBE and actual tranche net-cost threshold. Avoid small new-lot partialexits.

## Phase 3: research
Implement backtest/v5/commands below with shared production feature/strategy/cost code. Commands listed here are REQUIRED interfaces to create, not claims they already exist.
Persist an immutable protocol+datahash before running modelselection.
Use expanding36monthtrain/6monthcalibration/6monthtest where feasible. Purge overlapping holdingintervals; maximum40-session embargo for combinedhorizon study; separate calibration-fit/modelselection.
Compare rules, logisticC.1, fixed shallowXGB and prior. Use separate horizon models and exact matching calibrators. No broad hyperparameter search.
Economic targets: positiveNETP&L classification and continuousnetR/duration; unfilled/censoredstatuses excluded appropriately, not convertedto0losslabels.
Report costs ONCE: probabilities and conditional already-net payoff estimates must not have the same charges subtracted a second time. Quantity-dependent costs require recomputing candidate economics at executableqty.
Chronological sharedcash replay with exchange sessions, reservedcash, realistic settlement/tradability, no future-exit credit, mark-to-market drawdown, actualbookscenario + scalablebenchmark. Never report independentfamily capital as combinedperformance.
Compare screen vs no-screen matchedrules, short vs long, baseline vs challengeregime, delivery/sentiment only when timestampeddataexists. All triedvariants in experimentregistry; preserve negativefindings.
Generate funnel from source->liquid->setup->regime->affordable->risk->filled->closed; netmetrics and frequency distribution, including zero weeks, unique14dayintent counts and duplicatednotificationcounts.
No automaticpromotion: meet all requiredgates in spec, including untouchedforwardobservation. Price-only historicalsuccess cannot qualify the fundamental version.

## Phase 4: tests and measured acceptance
Write meaningful tests for:
1) future-bar/future-filing/future-news mutations cannot change earlier decisions;
2) screenpagination failure !=emptyresults; membership validity begins onlyatknown time;
3) <=150 unique symbols across horizons withheld/pending pinning;
4) adjusted/raw splits, holidaybarriers, gap-entry unfilled, gapstop>worst1R, samebarstopfirst, circuitlockedexit remains pending;
5) fee-aware integer sizing and entryband reservations; caps match fixture arithmetic; partialfills keep correctreservation;
6) no doublecash use by two strategies or duplicatealerts; concurrency via S3IfMatch conflict;
7) lossP&L excludesexternalflows and legacy marks; latch at-1500; deposits cannotreset; exits stillallowed;
8) newlosinglotcannotbecomelegacy, belowBElegacyexitblocked, tranchefees, immutablefilledstages, priortrailbeforeHWM;
9) portfolio invalid/stale or incompletequotes cannotoverwritegoodmarks; pure dryrun makes zero writes/notifications;
10) nativevsJSON raw/calibrated parity including splitthreshold/equality/missingvalue behavior, featureorder hash, correctchosenmodelcalibrator;
11) complete Lambda handler with mockservices and read-only AWSdryrun; runtimepackage contains no sklearn/XGB and meets real configuredtimeout/memory.
Don't claim mocked provider tests establish live API health. Record untested assertions separately.

## Phase 5: integration and release candidate
Wire small adapter under runtime_v5/ to existing notification formatting and S3 APIs. Existing09:25/15:05 schedules retained. Prepare proposedaftercloseingestjob; don't activateEventBridge changes yet.
150-name historicalfeatures prepared outside09:25scan; boundedcurrentquotes only. Run dryrun and shadow under separateprefix, with write/notification controls asserted.
Modelregistry supports explicit independentlypromoted rules OR model version; missingmodel mustnot silentlyfall back. Unpromoted output WATCHonly.
Brokerexecution remainsmanual. Record truefills and protectiveorderconfirmation; alerts aren'tfills and afternoonmonitor isn't an executable stop.
Prepare updatedstate migration diff and rollback, ziphash/layerrequirements, exact proposedIAM/EventBridgechanges, deploymentcommands and healthchecks. Do not overwrite S3book, promote model, send realalerts or deploy merelybecause AWScredentials exist. User asked for implementation first and reviewable deployment later.
Commit implementation; open draftPR against strategy-v5-blueprint with concrete metrics, tests, failinggates and blockerlist.

## Commands to implement and demonstrate
python -m backtest.v5.providers --probe --out artifacts/v5/provider_health.json
python -m backtest.v5.snapshot --as-of YYYY-MM-DD
python -m backtest.v5.universe --as-of YYYY-MM-DD --max-symbols 150
python -m backtest.v5.evaluate --protocol config/v5/research_policy.json
python -m backtest.v5.replay --book fixtures/v5/broker_snapshot_2026-10-17.json
python -m backtest.v5.export --candidate-only
python -m backtest.v5.parity
python -m pytest -q
python -m runtime_v5.preview --dry-run --book fixtures/v5/broker_snapshot_2026-10-17.json

If data prevents any step, implement/test its interface, emit a structured blockedstatus and continue independentwork. Never manufacture missinghistory, silently weakeneligibility, reuse today'sscreens in past, or relabel WATCH as BUY to finish.

## Final report
Return commit/PR, implementationsummary, observedproviderhealth, exactdatacoverage, untouched-vs-reused periodlabels, costs/PF/drawdown/activity results perhorizon and combined, modelparity, testcounts, deploymentstatus and requirednextaction.
If neitherhorizon demonstrates an edge, say so and keep tradingpromotion off. Engineeringcompletion and alpha-validation are separate results.
