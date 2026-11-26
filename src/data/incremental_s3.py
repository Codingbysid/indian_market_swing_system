"""Hive-partitioned S3 / local Parquet manager for daily UDiFF appends."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from src.config import S3Paths


def year_partition_path(root: Path | str, year: int) -> Path:
    return Path(root) / f"year={year}" / "data.parquet"


def append_daily_rows(
    day_df: pd.DataFrame,
    root: Path | str,
    *,
    date_col: str = "date",
) -> Path:
    """Idempotently append one session into year=YYYY/data.parquet."""
    if day_df.empty:
        raise ValueError("day_df is empty")
    frame = day_df.copy()
    frame[date_col] = pd.to_datetime(frame[date_col])
    year = int(frame[date_col].dt.year.mode().iloc[0])
    path = year_partition_path(root, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_parquet(path)
        existing[date_col] = pd.to_datetime(existing[date_col])
        session_dates = set(frame[date_col].dt.normalize())
        existing = existing[~existing[date_col].dt.normalize().isin(session_dates)]
        merged = pd.concat([existing, frame], ignore_index=True)
    else:
        merged = frame
    merged = merged.sort_values([date_col, "symbol"]).drop_duplicates(
        [date_col, "symbol"], keep="last"
    )
    merged.to_parquet(path, index=False)
    return path


def rebuild_year_partition(year_df: pd.DataFrame, root: Path | str, year: int) -> Path:
    """Replace an entire year partition after corporate-action adjustment."""
    path = year_partition_path(root, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = year_df.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values(["date", "symbol"]).drop_duplicates(["date", "symbol"], keep="last")
    frame.to_parquet(path, index=False)
    return path


def read_partitioned_ohlcv(root: Path | str) -> pd.DataFrame:
    root_path = Path(root)
    parts = sorted(root_path.glob("year=*/data.parquet"))
    if not parts:
        return pd.DataFrame()
    frames = [pd.read_parquet(part) for part in parts]
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values(["date", "symbol"]).reset_index(drop=True)


def s3_uri(paths: S3Paths, key: str) -> str:
    bucket = os.getenv(paths.bucket_env, paths.default_bucket)
    return f"s3://{bucket}/{key}"
