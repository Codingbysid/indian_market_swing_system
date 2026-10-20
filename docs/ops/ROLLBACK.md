# Rollback

Captured before the 20 Oct 2026 deployment. The broker book was not modified.

- Lambda version **10** is the previous code. Code sha256 `TOeunud6CMQm+iDnmWNa0FH30+gp5RedddyS8JPxgJg=`.
- The same package is stored at `s3://indian-swing-bot-data-2026/ops/rollback/lambda_pre_ops.zip`.
- Portfolio object `portfolio_state.json` ETag `a39cf4a3a2bc851035ac369860565c1d`. Do not restore an older portfolio over a newer broker book.
- Model `models/model.json` was not changed. sha256 `fac68a53e0776ac1be739d2c08d5c15981a04150d73c0bfa5fdcbfa09dab1cb9`.

EventBridge still invokes the unqualified function ARN, which uses `$LATEST`. Restoring version 10 means replacing `$LATEST` with that package:

```bash
aws lambda update-function-code \
  --function-name indian-swing-bot \
  --region eu-north-1 \
  --s3-bucket indian-swing-bot-data-2026 \
  --s3-key ops/rollback/lambda_pre_ops.zip
```

The added research rule does not place orders. Disable it without touching the other two schedules:

```bash
aws events disable-rule --region eu-north-1 --name swing-paper-research
```

Leave `swing-recommender-daily` and `swing-holdings-monitor` unchanged.
