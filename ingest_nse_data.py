"""NSE cash bhavcopy cache. Legacy files end on 7 July 2024. UDiFF starts 8 July 2024.

A holiday response that repeats the previous session is dropped when its
internal trade date does not match the requested date. This module does not
download anything unless ``build_ohlcv_parquet`` is called.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

UDIFF_CUTOFF = date(2024, 7, 8)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/zip, text/csv, */*",
    "Referer": "https://www.nseindia.com/all-reports",
}


def bhavcopy_url(day: date) -> tuple[str, str]:
    if day >= UDIFF_CUTOFF:
        stamp = day.strftime("%Y%m%d")
        return (
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{stamp}_F_0000.csv.zip",
            "UDIFF",
        )
    month = day.strftime("%b").upper()
    name = f"cm{day.strftime('%d')}{month}{day.strftime('%Y')}bhav.csv.zip"
    return (
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{day.strftime('%Y')}/{month}/{name}",
        "LEGACY",
    )


def fetch_and_cache_bhavcopy(
    day: date,
    cache_dir: Path,
    session: requests.Session,
    sleep_sec: float = 1.2,
) -> tuple[Path | None, str]:
    url, fmt = bhavcopy_url(day)
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_path = cache_dir / f"{day.isoformat()}_{fmt}.zip"
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path, fmt
    try:
        response = session.get(url, headers=HEADERS, timeout=20)
        time.sleep(sleep_sec)
        if response.status_code == 404:
            return None, fmt
        response.raise_for_status()
        local_path.write_bytes(response.content)
        return local_path, fmt
    except requests.RequestException as exc:
        logger.warning("Failed %s (%s): %s", day.isoformat(), fmt, exc)
        return None, fmt


def parse_bhavcopy_zip(zip_path: Path, expected_date: date, fmt: str) -> pd.DataFrame | None:
    """Return EQ rows only when the file's own trade date is the requested date."""
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            return None
        raw = pd.read_csv(io.BytesIO(archive.read(names[0])), dtype=str)
    raw.columns = [column.strip() for column in raw.columns]
    if fmt == "UDIFF":
        mapping = {
            "TradDt": "date",
            "TckrSymb": "symbol",
            "SctySrs": "series",
            "ISIN": "isin",
            "OpnPric": "open",
            "ClsPric": "close",
        }
        value_column = "TtlTrfVal" if "TtlTrfVal" in raw.columns else None
        volume_column = "TtlTradgVol" if "TtlTradgVol" in raw.columns else None
    else:
        mapping = {
            "TIMESTAMP": "date",
            "SYMBOL": "symbol",
            "SERIES": "series",
            "ISIN": "isin",
            "OPEN": "open",
            "CLOSE": "close",
        }
        value_column = "TOTTRDVAL" if "TOTTRDVAL" in raw.columns else None
        volume_column = None
    missing = [source for source in mapping if source not in raw.columns]
    if missing or value_column is None and volume_column is None:
        logger.warning("Skipping %s: missing columns %s", zip_path.name, missing or "turnover")
        return None
    keep = list(mapping)
    if value_column:
        keep.append(value_column)
    if volume_column and volume_column not in keep:
        keep.append(volume_column)
    frame = raw[keep].rename(columns={**mapping, value_column: "traded_value_inr"} if value_column else mapping).copy()
    if volume_column and "traded_value_inr" not in frame.columns:
        frame = frame.rename(columns={volume_column: "volume"})
    elif volume_column:
        frame["volume"] = raw[volume_column]
    for column in ("symbol", "series", "isin", "date"):
        if column in frame.columns:
            frame[column] = frame[column].astype(str).str.strip()
    frame = frame[frame["series"] == "EQ"].copy()
    if frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["date"], format="mixed", dayfirst=False, errors="coerce")
    actual = frame["date"].dt.date.dropna().unique()
    if len(actual) != 1 or actual[0] != expected_date:
        logger.info("Dropping %s: internal date %s != %s", zip_path.name, list(actual), expected_date)
        return None
    for column in ("open", "close", "traded_value_inr", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "traded_value_inr" not in frame.columns:
        frame["traded_value_inr"] = frame["close"] * frame["volume"]
    frame = frame.dropna(subset=["symbol", "open", "close", "traded_value_inr"])
    frame = frame[(frame["close"] > 0) & (frame["traded_value_inr"] >= 0)]
    return frame[["date", "symbol", "isin", "open", "close", "traded_value_inr"]].reset_index(drop=True)


def build_ohlcv_parquet(
    start_date: date,
    end_date: date,
    raw_cache_dir: Path,
    output_parquet: Path,
) -> pd.DataFrame:
    raw_cache_dir.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    with requests.Session() as session:
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:
                path, fmt = fetch_and_cache_bhavcopy(current, raw_cache_dir, session)
                if path is not None:
                    parsed = parse_bhavcopy_zip(path, current, fmt)
                    if parsed is not None:
                        frames.append(parsed)
            current += timedelta(days=1)
    if not frames:
        raise RuntimeError("No valid Bhavcopy files were parsed.")
    ohlcv = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(["date", "symbol"], keep="last")
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    ohlcv.to_parquet(output_parquet, index=False, compression="snappy")
    logger.info("Saved %d rows to %s", len(ohlcv), output_parquet)
    return ohlcv


def pit_sectors_from_snapshots(snapshots_dir: Path) -> pd.DataFrame:
    """Read ``YYYY-MM-DD_nse_sectors.csv`` snapshots. Missing files stay missing."""
    records: list[pd.DataFrame] = []
    for csv_path in sorted(snapshots_dir.glob("*_nse_sectors.csv")):
        effective = pd.to_datetime(csv_path.name.split("_")[0])
        raw = pd.read_csv(csv_path, dtype=str)
        raw.columns = [column.strip().lower() for column in raw.columns]
        rename = {}
        for column in raw.columns:
            if column in {"symbol", "tckrsymb"}:
                rename[column] = "symbol"
            elif column in {"isin", "isin code"}:
                rename[column] = "isin"
            elif column in {"sector", "industry"}:
                rename[column] = "sector"
            elif column in {"macro-economic sector", "macro_sector", "macro economic sector"}:
                rename[column] = "macro_sector"
        frame = raw.rename(columns=rename)
        required = {"symbol", "sector", "macro_sector"}
        if not required.issubset(frame.columns):
            raise ValueError(f"{csv_path.name} missing {required - set(frame.columns)}")
        if "isin" not in frame.columns:
            frame["isin"] = pd.NA
        frame["effective_date"] = effective
        for column in ("symbol", "isin", "sector", "macro_sector"):
            frame[column] = frame[column].astype(str).str.strip()
        records.append(frame[["effective_date", "symbol", "isin", "sector", "macro_sector"]])
    if not records:
        raise RuntimeError(f"No *_nse_sectors.csv files found in {snapshots_dir}")
    pit = (
        pd.concat(records, ignore_index=True)
        .drop_duplicates(["effective_date", "symbol"], keep="last")
        .sort_values(["effective_date", "symbol"])
        .reset_index(drop=True)
    )
    return pit


def build_pit_sector_parquet(snapshots_dir: Path, output_parquet: Path) -> pd.DataFrame:
    pit = pit_sectors_from_snapshots(snapshots_dir)
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    try:
        pit.to_parquet(output_parquet, index=False, compression="snappy")
    except ImportError as exc:
        raise ImportError("Install pyarrow to write the sector parquet.") from exc
    return pit
