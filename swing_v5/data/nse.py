"""Dated NSE files. A zip header is not a validated session."""

from __future__ import annotations

import hashlib
import io
import time
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

UDIFF = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
DELIVERY = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{dmy}.csv"
REQUIRED = {"TradDt", "ISIN", "TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"}


def _get(url: str, timeout: int = 40, retries: int = 2) -> bytes:
    last = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise last


def fetch_udiff(session: str, raw_dir: Path) -> dict:
    """session is YYYY-MM-DD. Saturday is refused before any download."""
    day = pd.Timestamp(session)
    if day.weekday() >= 5:
        return {"status": "blocked", "reason": "not_an_exchange_session", "session": session, "parsed": False}
    ymd = day.strftime("%Y%m%d")
    url = UDIFF.format(ymd=ymd)
    try:
        payload = _get(url)
    except Exception as exc:
        return {"status": "failed", "error": type(exc).__name__, "url": url, "parsed": False, "historical_file_verified": False}
    digest = hashlib.sha256(payload).hexdigest()
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"udiff_{ymd}.zip"
    raw_path.write_bytes(payload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        bad = archive.testzip()
        names = archive.namelist()
        if bad or len(names) != 1:
            return {"status": "invalid_zip", "parsed": False, "sha256": digest, "members": names}
        frame = pd.read_csv(archive.open(names[0]))
    except (zipfile.BadZipFile, pd.errors.ParserError, KeyError) as exc:
        return {"status": "parse_failed", "error": type(exc).__name__, "parsed": False, "sha256": digest}
    missing = sorted(REQUIRED - set(frame.columns))
    if missing:
        return {"status": "schema_mismatch", "missing": missing, "parsed": False, "sha256": digest}
    dates = set(frame["TradDt"].astype(str).str.slice(0, 10))
    if dates != {session}:
        return {"status": "date_mismatch", "dates": sorted(dates), "parsed": False, "sha256": digest}
    equity = frame[(frame["SctySrs"] == "EQ")].copy()
    for column in ("OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"):
        equity[column] = pd.to_numeric(equity[column], errors="coerce")
    malformed = equity["OpnPric"].isna() | equity["HghPric"].isna() | equity["LwPric"].isna() | equity["ClsPric"].isna()
    malformed = malformed | (equity["HghPric"] < equity["LwPric"]) | (equity["OpnPric"] <= 0) | (equity["ClsPric"] <= 0)
    malformed = malformed | (equity["TtlTradgVol"] < 0)
    duplicates = int(equity["TckrSymb"].duplicated().sum())
    if int(malformed.fillna(True).sum()) or duplicates:
        return {
            "status": "invalid_rows",
            "parsed": False,
            "historical_file_verified": False,
            "malformed_ohlc": int(malformed.fillna(True).sum()),
            "duplicate_symbols": duplicates,
            "sha256": digest,
        }
    normalized = equity.rename(
        columns={"TckrSymb": "Symbol", "OpnPric": "Open", "HghPric": "High", "LwPric": "Low", "ClsPric": "Close", "TtlTradgVol": "Volume", "TradDt": "Date"}
    )[["Date", "Symbol", "ISIN", "Open", "High", "Low", "Close", "Volume"]]
    out = raw_dir / f"udiff_{ymd}_eq.csv"
    normalized.to_csv(out, index=False)
    return {
        "status": "parsed",
        "parsed": True,
        "historical_file_verified": True,
        "session": session,
        "url": url,
        "sha256": digest,
        "rows": int(len(frame)),
        "eq_rows": int(len(normalized)),
        "duplicate_symbols": 0,
        "raw": str(raw_path),
        "normalized": str(out),
        "legacy_format": "dispatched_only_after_a_verified_legacy_file",
    }


def fetch_delivery(session: str, raw_dir: Path) -> dict:
    day = pd.Timestamp(session)
    if day.weekday() >= 5:
        return {"status": "blocked", "reason": "not_an_exchange_session", "optional": True}
    url = DELIVERY.format(dmy=day.strftime("%d%m%Y"))
    try:
        payload = _get(url)
    except Exception as exc:
        return {"status": "unavailable", "error": type(exc).__name__, "optional": True, "absence_is_not_zero": True}
    frame = pd.read_csv(io.BytesIO(payload))
    frame.columns = [column.strip() for column in frame.columns]
    if "DELIV_QTY" not in frame.columns and "DELIV_PER" not in frame.columns:
        return {"status": "schema_mismatch", "columns": list(frame.columns)[:12], "optional": True, "absence_is_not_zero": True}
    path = raw_dir / f"delivery_{day.strftime('%Y%m%d')}.csv"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "status": "parsed_optional",
        "rows": int(len(frame)),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "path": str(path),
        "used_in_model": False,
        "absence_is_not_zero": True,
    }


def probe_url(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(400)
            return {
                "url": url,
                "status": "response",
                "http_status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "sample_bytes": len(payload),
                "zip_header": payload[:2] == b"PK",
                "historical_file_verified": False,
                "note": "prefix only; use fetch_udiff for a parsed session",
            }
    except Exception as exc:
        return {"url": url, "status": "failed", "error": type(exc).__name__, "historical_file_verified": False}
