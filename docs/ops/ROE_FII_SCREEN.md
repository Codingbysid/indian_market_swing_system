# ROE / FII entry screen

This change adds eligibility checks to production recommendations before reservations and to new paper admissions. It does not promote strategies, change model probabilities, size positions, or alter HOLD/SELL instructions. Existing paper positions and pending orders retain their existing lifecycle. Raw strategy detections still run and are reported separately from admission.

## Frozen definitions

- 0 < candidate price / trailing EPS < 30. Production uses the maximum entry-band price when available; paper uses the completed-session close at detection. This is a signal-time test, not a guarantee about subsequent fills.
- Latest annual ROE > 20%, not ROI or ROCE.
- Trailing EPS > 0, on the same financial basis and share basis as the price.
- Latest annual net profit > 1.20 times the previous consecutive year's profit; both must be positive. A loss-to-profit transition cannot pass this percentage-growth test.
- Latest disclosed quarterly FII ownership percentage exceeds the preceding consecutive quarter. This is a lagged ownership proxy, not confirmation of a recent purchase or net market buying.

All thresholds are strict. A passing screen is not evidence of trading alpha and does not bypass promotion, broker reconciliation, surveillance, corporate-action, regime, cost or risk gates.

## Evidence contract

Read-only runtime key: `research/fundamentals/roe_fii_snapshots.json` in the configured data bucket. Format: `{"schema_version": 1, "snapshots": [...]}`.

Each record requires:

| Fields | Meaning |
| --- | --- |
| symbol | Verified NSE symbol |
| observed_at | Actual ingestion timestamp, timezone-aware ISO 8601 |
| published_at | Latest actual publication timestamp among all underlying disclosures |
| financial_basis | consolidated or standalone, consistent across financial metrics |
| eps_ttm, ttm_period_end | Trailing EPS and its reporting-period end |
| roe_pct, fy_end | Annual ROE percentage and financial year end |
| previous_fy_end | Consecutive comparable prior financial year end |
| net_profit_fy, net_profit_previous_fy | Annual profits in identical currency units |
| fii_pct, previous_fii_pct | Ownership percentages; 20 means 20%, not 0.20 |
| fii_period_end, previous_fii_period_end | Consecutive calendar quarter ends |
| sources | Object with financials and shareholding HTTPS source URLs |
| source_hashes | Object with financials and shareholding SHA256 hashes of archived source bytes |

The ingestion operator must verify identity, publication timestamps, reporting basis, EPS corporate-action adjustments and source contents. Runtime validates structure and chronology; URLs and hashes alone do not authenticate facts. Archive original documents alongside each normalized vintage. Do not fabricate missing figures or substitute current facts into historical decisions.

The most recent known snapshot wins, including when it fails. Conflicting latest snapshots, absent data, non-finite values, unknown timestamps and stale evidence return UNVERIFIED and block admission. Engineering freshness defaults are 30 days since observation, 200 days since TTM/FII period end and 550 days since annual period end. These are explicit defaults, not user-specified thresholds or proof that no newer disclosure exists; collectors must select the latest available disclosure.

## Preparing real snapshots

`scripts/prepare_fundamental_snapshots.py` validates normalized, sourced input. Each incoming row also needs a numeric `reference_price` for its verification report. It stamps the actual current observation time, rejects unverifiable rows, and preserves FAIL records. It does not fetch data or upload it.

```bash
python scripts/prepare_fundamental_snapshots.py --input sourced_records.json --output new_vintage.json
# Append to a previously downloaded store without erasing historical vintages:
python scripts/prepare_fundamental_snapshots.py --input sourced_records.json --existing prior_store.json --output next_store.json
```

Publish validated stores using an S3 conditional write against the previously read ETag; use IfNoneMatch for a first creation. Retry a conflict by reading and merging, never by overwriting concurrent evidence. Keep raw archives and backups. No production snapshot values are included in this commit. No live financial-data source has been verified by these fixture tests. A source collector and refresh operation remain an operational dependency; absence of that dependency must remain visible as UNVERIFIED.

## Validation and deployment handoff

```bash
python -m pytest -q
python scripts/package_ops_candidate.py
```

The packager creates `dist/roe_fii_ops_candidate.zip` and a SHA256 manifest. It includes application sources and configuration, not sklearn/XGBoost, broker state or credentials. It depends on the existing Lambda dependency layers; inspect compatibility before deployment.

Deployment target: `indian-swing-bot`, region `eu-north-1`, alias `live`. Read the actual alias version, function configuration, layers, EventBridge targets and relevant S3 ETags first. Repository deployment receipts are historical evidence, not current AWS truth. Capture the current alias as rollback. Verify the deployment identity and preserve environment, layers, schedules and permissions; grant only the new read key if necessary.

Publish the candidate as a new immutable Lambda version. Smoke-test that version with alerts off and the existing dry-run mechanism, confirming no broker/paper writes; inspect errors and compare its CodeSha256 with the build manifest. Only after successful smoke checks, update the alias with its current RevisionId. Verify all three EventBridge rules still target `:live`, unchanged clocks, unchanged broker state and unchanged promotion/model files. Roll back by restoring the captured alias version if verification fails.

Never initialize/reset the paper account, copy hypothetical cash into broker state, fabricate reconciliation records or install test-only policy. Never enable an unqualified family to make this filter produce signals. Notifications should show PASS / FAIL / UNVERIFIED coverage honestly. Publish a deployment receipt containing commit, version, code hash, alias target, checks, actual data coverage and rollback version.

This workspace could implement and test the code but had no AWS credentials or AWS connector. It did not deploy, write S3, send alerts or change production. Cursor with AWS access must perform the steps above and populate verified evidence before claiming end-to-end operation.
