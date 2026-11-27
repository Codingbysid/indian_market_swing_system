#!/usr/bin/env bash
# Resumable year-by-year NSE Bhavcopy download + final panel assembly.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs data/raw/bhavcopy_zips data/raw/ohlcv_checkpoint data/clean

START_YEAR=${1:-2016}
END_YEAR=${2:-2026}
SLEEP=${3:-0.75}

for Y in $(seq "$START_YEAR" "$END_YEAR"); do
  if [[ "$Y" -eq 2026 ]]; then
    END_DATE="2026-11-26"
  else
    END_DATE="${Y}-12-31"
  fi
  START_DATE="${Y}-01-01"
  echo "[year] $START_DATE -> $END_DATE"
  python -u - <<PY
from datetime import date
from pathlib import Path
from src.data.ingest_nse import build_ohlcv_parquet_resumable
build_ohlcv_parquet_resumable(
    start_date=date.fromisoformat("$START_DATE"),
    end_date=date.fromisoformat("$END_DATE"),
    raw_cache_dir=Path("data/raw/bhavcopy_zips"),
    checkpoint_dir=Path("data/raw/ohlcv_checkpoint"),
    output_parquet=Path("data/clean/_partial_${Y}.parquet"),
    sleep_sec=float("$SLEEP"),
    checkpoint_every=15,
)
print("year_done", "$Y")
PY
done

echo "[assemble] build_real_10y_panel remaining steps (uses checkpoints)"
python -u scripts/build_real_10y_panel.py --start 2016-01-01 --end 2026-11-26 --sleep "$SLEEP"
echo ALL_DONE
