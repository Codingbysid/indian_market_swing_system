# Forward research runbook

Production Lambda, EventBridge, the S3 book, and live alerts stay unchanged. These commands write only under `artifacts/v5/forward/`.

```bash
python -m backtest.v5.collect
python -m backtest.v5.forward
python -m backtest.v5.report
```

`collect` archives Screener HTML and hashes. Company URL slugs are not treated as NSE tickers. `forward` records a snapshot with the clock time it actually ran. A weekend has no equity session. Passing `--as-of` with an earlier date classifies the row as a retrospective replay and does not backdate `first_seen_at`.

Paper evaluation of an unpromoted setup is a research detection. It is not a production BUY. Quality and value stay blocked until a snapshot collected by this runner is the one being used, and even then only for sessions after `retrieved_at`.

Proposed later AWS job, not created: a separate function reading and writing `s3://<research-bucket>/v5-forward/` on weekdays after the close. It must not use the live portfolio key or the notification topics.

ZENTEC keeps its universe slot. Missing same-vendor history stays missing. INFY and ZENTEC fills are still not invented.
