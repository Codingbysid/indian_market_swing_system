#!/usr/bin/env bash
# Parallel year-by-year NSE Bhavcopy download (shared day-shard checkpoints).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs data/raw/bhavcopy_zips data/raw/ohlcv_checkpoint data/clean

START_YEAR=${1:-2016}
END_YEAR=${2:-2026}
SLEEP=${3:-1.0}
JOBS=${4:-4}

download_year() {
  local Y="$1"
  local END_DATE
  if [[ "$Y" -eq 2026 ]]; then
    END_DATE="2026-11-26"
  else
    END_DATE="${Y}-12-31"
  fi
  local START_DATE="${Y}-01-01"
  echo "[year-start] $START_DATE -> $END_DATE"
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
    checkpoint_every=20,
    assemble=False,
)
print("year_done", "$Y")
PY
  echo "[year-done] $Y"
}

export -f download_year
export SLEEP

seq "$START_YEAR" "$END_YEAR" | xargs -P "$JOBS" -I{} bash -c 'download_year "$@"' _ {}

echo "[assemble] build_real_10y_panel from checkpoints"
python -u scripts/build_real_10y_panel.py --start 2016-01-01 --end 2026-11-26 --sleep "$SLEEP"
echo ALL_DONE
