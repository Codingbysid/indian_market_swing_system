# Production Readiness

## Verification completed

| Check | Result |
| --- | --- |
| `pytest` | **41 passed** (corporate actions, PIT universe, HMM gate, GBDT embargo, RL feasibility, BPS costs, Lambda workflow, messages) |
| Slot dry-runs | All 6 payloads OK: `PRE_MARKET`, `SLOT_1` (09:45), `SLOT_2` (12:45), `SLOT_3_PRE_CAS` (15:10), `SLOT_3` (15:30), `POST_MARKET` → `reports/slot_dry_runs/` |
| Ablation report | `reports/backtest_alpha_bps_summary.md` + `.csv` |
| Cost schedule | Default RT **32.3 bps**; stress RT **42.3 bps** |

## Container + SAM (ready, not deployed)

| File | Role |
| --- | --- |
| `Dockerfile` | Lambda Python 3.11 image, `libgomp`, **CPU-only torch**, then `requirements.txt` |
| `.dockerignore` | Excludes `data/`, `reports/`, tests, local venvs |
| `template.yaml` | SAM image function + 5× `ScheduleV2` IST crons + error alarm |

```bash
sam build
echo '{"job_type":"PRE_MARKET","slot_name":"09:05_IST","dry_run":true}' | \
  sam local invoke TradingEngineFunction --event -
sam deploy --guided --region ap-south-1 --stack-name nse-quant-swing-v2 \
  --capabilities CAPABILITY_IAM --resolve-image-repos
```

Deploy to **`ap-south-1`** for NSE/broker latency. Do not enable live schedules until dry-run review.

## Remaining AWS deployment steps (not applied)

1. **IAM:** Covered by SAM policies (S3 `v2/*`, SES, Secrets Manager `/quant-trading/*`).
2. **Package size:** Solved via container image (~650 MB CPU torch path); zip layers are not used.
3. **EventBridge Scheduler:** Five IST `ScheduleV2` rules in `template.yaml`. Slot 3 defaults to **15:10 IST** (pre-CAS); override `Slot3CronExpression` for 15:30 EOD.
4. **S3 layout:** `v2/state/latest.json`, `v2/universes/latest_150.json`, hive partitions under `v2/clean/ohlcv_*_partitioned/year=YYYY/`.
5. **Data:** Ingest 10y Bhavcopy + PIT sectors + Nifty/VIX into S3/`data/clean/`, then re-run ablation with `--full`.

## Architecture map

```
src/config.py
src/data/{ingest_nse,corporate_actions,incremental_s3}.py
src/universe/pit_universe.py
src/models/{hmm_regime,gbdt_ranker,rl_sizing_env,train_ppo}.py
src/backtest/{costs,engine,report}.py
src/aws/{schemas,engine,lambda_handler}.py
scripts/{build_research_panel,run_full_ablation,dry_run_slots}.py
```
