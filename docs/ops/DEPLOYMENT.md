# Operational deployment, 20 Oct 2026

Production function `indian-swing-bot` in eu-north-1.

- Deployed Lambda version **12**. Code sha256 `x7eK7hBJPFplYKuGs26Wk8yLTSGscI9PDuQs2kWOnFE=`.
- Version 11 (`s3MWLPWE86vinTvMMzmwjD7cFvwj+t3GUN1DPvsIFMw=`) is the package that served the 09:25 schedule. Version 12 adds a same-day dedupe for a repeated holiday notice.
- Rollback version **10**. See `ROLLBACK.md`.
- Layers unchanged: `AWSSDKPandas-Python312:20` and `quant-dependencies:5`.
- Timeout 300 seconds, memory 1024 MB, Python 3.12. No sklearn or XGBoost library in the package. The live model remains the pure-Python 7-feature dump.
- `portfolio_state.json` ETag was `a39cf4a3a2bc851035ac369860565c1d` before and after deployment.

## Schedules

| Rule | UTC cron | IST | Input | Target |
|---|---|---|---|---|
| swing-recommender-daily | cron(55 3 ? * MON-FRI *) | 09:25 | `{"action":"recommender"}` | unqualified `indian-swing-bot` |
| swing-holdings-monitor | cron(35 9 ? * MON-FRI *) | 15:05 | `{"action":"monitor"}` | unqualified `indian-swing-bot` |
| swing-paper-research | cron(40 10 ? * MON-FRI *) | 16:10 | `{"action":"paper"}` | unqualified `indian-swing-bot` |

The first two rules were not retimed. The third runs only after the cash close. On an exchange holiday the recommender and monitor return `exchange_closed` and do not describe the book as all-clear.

## Storage

- Broker book, unchanged: `s3://indian-swing-bot-data-2026/portfolio_state.json`
- Model, unchanged: `s3://indian-swing-bot-data-2026/models/model.json`
- Paper book: `s3://indian-swing-bot-data-2026/research/paper_book.json`
- Source notes: `s3://indian-swing-bot-data-2026/research/sources/`
- Transport idempotency: `s3://indian-swing-bot-data-2026/research/transport_tests.json`

Paper writes use `IfMatch` / `IfNoneMatch`. A second identical snapshot is skipped and does not send another notification.

## Broker reconciliation

S3 still holds the 7 Oct 2026 book: cash ₹15,255.50; HAL 16 @ 5040.63; ZENTEC 8 @ 1711.25; CDSL 8 @ 1363.75; INFY 5 @ 1030; BPCL 6 @ 318.65. All five are `legacy_profit_only`. `broker_reconciled_at` is null. The file's `updated` field is `2026-10-07T06:09:00+00:00`. The object was rewritten by the 19 Oct monitor, but the quantities did not change.

The 17 Oct reference (cash ₹5,748, INFY absent, ZENTEC 10 @ 1669) is not in S3 and was not copied in. There is still no broker-confirmed INFY sale record and no ZENTEC quantity/fill record. New production BUY admission stays blocked until those records exist. Legacy lots stay profit-only. No average-in. No fabricated fills.

## What ran

- Dry-run holiday recommender and monitor returned `BLOCKED / exchange_closed` and sent nothing.
- One transport test sent. CloudWatch recorded `Push notification part 1/1 sent` and `Email alert sent successfully`. A second invoke was `duplicate_skipped` and sent nothing.
- At 09:25 IST the existing recommender rule invoked `$LATEST` twice. Both runs were the closed-exchange notice, not a buy or sell. A later repeat returned `duplicate_skipped` and did not send again.
- Paper evaluation stored one snapshot for completed session 2026-10-19. SUNTV was evaluated on a Yahoo split-adjusted panel and had no trend setup. No paper order was admitted. Advance was a no-op. ZOTA and CMLL were fetched but did not have a bar on that completed session after alignment, so they were not called evaluated. Quality and value were not evaluated: there is no point-in-time fundamental snapshot.

## Commands

```bash
python -m pytest tests
aws lambda get-function-configuration --function-name indian-swing-bot --region eu-north-1 --query '[Version,CodeSha256]'
aws events list-targets-by-rule --region eu-north-1 --rule swing-recommender-daily
```
