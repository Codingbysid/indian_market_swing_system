# Production Readiness

## Verification completed

| Check | Result |
| --- | --- |
| `pytest` | Full suite green on the V3 production stack (HMM argmax, equity sizing, AWS container slots, GBDT causality, costs) |
| Slot dry-runs | `python3 scripts/dry_run_slots.py` → PRE_MARKET / 09:45 / 12:45 / 15:10 / 15:30 / POST_MARKET |
| Ablation / bake-off report | `reports/backtest_alpha_bps_summary.md` + `.csv` + `subperiod_stability.csv` |
| Cost schedule | Default RT **32.3 bps**; stress RT **42.3 bps** |
| Winner | `Winner_APlus_HardCap_rankBuffer` + V3 regime-resilient LightGBM features (PPO / Option C dropped) |

## Container + SAM (ready, not deployed)

| File | Role |
| --- | --- |
| `Dockerfile` | Lambda Python 3.11 image, `libgomp`, `requirements.txt`, baked `v2/models/latest/` |
| `.dockerignore` | Excludes `data/`, `reports/`, tests, local venvs, artifacts |
| `template.yaml` | SAM image function + 5× `ScheduleV2` IST crons + error alarm |

```bash
docker build -t nse-quant-swing-engine-v2:v2-latest .
sam build
echo '{"job_type":"PRE_MARKET","slot_name":"09:05_IST","dry_run":true}' | \
  sam local invoke TradingEngineFunction --event -
sam deploy --guided --region ap-south-1 --stack-name nse-quant-swing-v2 \
  --capabilities CAPABILITY_IAM --resolve-image-repos
```

Deploy to **`ap-south-1`** for NSE/broker latency. Do not enable live schedules until dry-run review.

## Remaining AWS deployment steps (not applied)

1. **IAM:** Covered by SAM policies (S3 `v2/*`, SES, Secrets Manager `/quant-trading/*`).
2. **Package size:** Container image only — the legacy zip Lambda and its `eu-north-1` EventBridge rules have been deleted.
3. **EventBridge Scheduler:** Five IST `ScheduleV2` rules in `template.yaml` (`nse-quant-swing-v2` in `ap-south-1`).
4. **S3 layout:** `v2/state/latest.json`, `v2/state/latest_features.json`, `v2/models/latest/`, hive partitions under `v2/clean/ohlcv_*_partitioned/year=YYYY/`.
5. **Data:** Keep regenerable panels local/untracked; retrain via `scripts/train_prod_v3.py`. Seed with `scripts/seed_s3_v2.sh`.

## Architecture map

```
src/config.py
src/data/{ingest_nse,corporate_actions,incremental_s3}.py
src/universe/pit_universe.py
src/models/{hmm_regime,gbdt_ranker}.py
src/backtest/{costs,engine,report}.py
src/aws/{schemas,engine,lambda_handler,calendar_nse,notifications,state,quotes,eod_pipeline}.py
scripts/{train_prod_v3,dry_run_slots,run_winner_bakeoff,build_real_10y_panel}.py
v2/models/latest/{lgbm_v3_ranker.txt,hmm_regime.joblib,latest_features.json,latest_150_universe.json}
```

## Model artifacts

Frozen inference weights live in `v2/models/latest/` (also mirrored to `s3://<bucket>/v2/models/latest/` and `s3://<bucket>/v2/state/latest_features.json` at runtime). All files are well under the 25 MB GitHub soft limit.
