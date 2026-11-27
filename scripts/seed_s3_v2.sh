#!/usr/bin/env bash
# One-shot seed of s3://$BUCKET/v2/ from local v2/models/latest/ + data/clean/.
# Run BEFORE `sam deploy` so Day-1 S3-first loads succeed in ap-south-1.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Default matches src.config.S3Paths.default_bucket (existing data bucket; not a Lambda name).
BUCKET="${BUCKET_NAME:-${QUANT_BUCKET:-$(python3 -c 'from src.config import S3Paths; print(S3Paths().default_bucket)')}}"
REGION="${AWS_REGION:-ap-south-1}"
DRY_RUN="${DRY_RUN:-0}"
AWS=(aws --region "$REGION")
S3_SYNC=(aws s3 sync --region "$REGION")
if [[ "$DRY_RUN" == "1" ]]; then
  S3_SYNC+=( --dryrun )
  echo "[seed] DRY_RUN=1 — no writes will be performed"
fi

MODELS_DIR="$ROOT/v2/models/latest"
CLEAN_DIR="$ROOT/data/clean"
PART_DIR="$ROOT/data/clean/ohlcv_adj_partitioned"
STATE_DIR="$ROOT/v2/state"

echo "[seed] bucket=s3://$BUCKET  region=$REGION"
echo "[seed] models=$MODELS_DIR"

[[ -f "$MODELS_DIR/lgbm_v3_ranker.txt" ]] || { echo "missing $MODELS_DIR/lgbm_v3_ranker.txt"; exit 1; }
[[ -f "$MODELS_DIR/MODEL_CARD.json" ]] && python3 - <<PY
import json
from pathlib import Path
card = json.loads(Path("$MODELS_DIR/MODEL_CARD.json").read_text())
print("[seed] MODEL_CARD training_universe=", card.get("training_universe"),
      "adopted=", (card.get("ab_test_2024_2026_top150_ic") or {}).get("adopted"))
PY

# 1) Frozen inference artifacts
"${S3_SYNC[@]}" "$MODELS_DIR/" "s3://$BUCKET/v2/models/latest/" \
  --exclude ".DS_Store" --exclude "__pycache__/*"

# 2) State copies of features + universe (S3-first runtime keys)
mkdir -p "$STATE_DIR"
cp -f "$MODELS_DIR/latest_features.json" "$STATE_DIR/latest_features.json"
cp -f "$MODELS_DIR/latest_150_universe.json" "$STATE_DIR/latest_150_universe.json"
if [[ "$DRY_RUN" != "1" ]]; then
  "${AWS[@]}" s3 cp "$MODELS_DIR/latest_features.json" "s3://$BUCKET/v2/state/latest_features.json"
  "${AWS[@]}" s3 cp "$MODELS_DIR/latest_150_universe.json" "s3://$BUCKET/v2/state/latest_150_universe.json"
  "${AWS[@]}" s3 cp "$MODELS_DIR/MODEL_CARD.json" "s3://$BUCKET/v2/models/latest/MODEL_CARD.json"
else
  echo "[seed] would put latest_features.json + universe + MODEL_CARD → v2/state & v2/models/latest"
fi

# 3) PIT sectors + macro (needed for EOD feature refresh / universe rebuild)
if [[ -f "$CLEAN_DIR/nse_pit_sectors.parquet" ]]; then
  if [[ "$DRY_RUN" != "1" ]]; then
    "${AWS[@]}" s3 cp "$CLEAN_DIR/nse_pit_sectors.parquet" "s3://$BUCKET/v2/clean/nse_pit_sectors.parquet"
  else
    echo "[seed] would put nse_pit_sectors.parquet"
  fi
fi
if [[ -f "$CLEAN_DIR/nifty_vix_10y.parquet" ]]; then
  if [[ "$DRY_RUN" != "1" ]]; then
    "${AWS[@]}" s3 cp "$CLEAN_DIR/nifty_vix_10y.parquet" "s3://$BUCKET/v2/clean/nifty_vix_10y.parquet"
  else
    echo "[seed] would put nifty_vix_10y.parquet"
  fi
fi

# 4) Hive-partitioned adjusted OHLCV (build locally from flat parquet if missing)
if [[ ! -d "$PART_DIR" ]] || [[ -z "$(ls -A "$PART_DIR" 2>/dev/null || true)" ]]; then
  if [[ -f "$CLEAN_DIR/nse_ohlcv_10y_adj.parquet" ]]; then
    echo "[seed] building year=YYYY partitions from nse_ohlcv_10y_adj.parquet …"
    python3 - <<PY
from pathlib import Path
import pandas as pd
src = Path("$CLEAN_DIR/nse_ohlcv_10y_adj.parquet")
out = Path("$PART_DIR")
out.mkdir(parents=True, exist_ok=True)
df = pd.read_parquet(src)
df["date"] = pd.to_datetime(df["date"])
df["year"] = df["date"].dt.year
for year, g in df.groupby("year"):
    dest = out / f"year={int(year)}"
    dest.mkdir(parents=True, exist_ok=True)
    g.drop(columns=["year"]).to_parquet(dest / "data.parquet", index=False)
    print(f"  wrote {dest}/data.parquet rows={len(g)}")
PY
  else
    echo "[seed] WARN: no nse_ohlcv_10y_adj.parquet — skipping OHLCV partition upload"
  fi
fi

if [[ -d "$PART_DIR" ]]; then
  "${S3_SYNC[@]}" "$PART_DIR/" "s3://$BUCKET/v2/clean/ohlcv_adj_partitioned/" \
    --exclude ".DS_Store"
fi

echo "[seed] verifying key objects …"
"${AWS[@]}" s3 ls "s3://$BUCKET/v2/models/latest/" || true
"${AWS[@]}" s3 ls "s3://$BUCKET/v2/state/" || true
"${AWS[@]}" s3 ls "s3://$BUCKET/v2/clean/" || true

echo
echo "[seed] DONE. Next:"
echo "  sam build"
echo "  sam deploy --region ap-south-1 --stack-name nse-quant-swing-v2 \\"
echo "    --resolve-image-repos --capabilities CAPABILITY_IAM \\"
echo "    --parameter-overrides QuantBucketName=$BUCKET AlertFromEmail=... AlertToEmail=..."
echo "  # After 16:05 IST: invoke POST_MARKET and confirm v2/state/latest_features.json as_of date."
