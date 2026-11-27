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
from urllib.parse import quote

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
    return build_ohlcv_parquet_resumable(
        start_date=start_date,
        end_date=end_date,
        raw_cache_dir=raw_cache_dir,
        checkpoint_dir=raw_cache_dir.parent / "ohlcv_checkpoint",
        output_parquet=output_parquet,
    )


def build_ohlcv_parquet_resumable(
    start_date: date,
    end_date: date,
    raw_cache_dir: Path,
    checkpoint_dir: Path,
    output_parquet: Path,
    sleep_sec: float = 1.2,
    checkpoint_every: int = 20,
    assemble: bool = True,
) -> pd.DataFrame | None:
    """Download/parse weekdays with zip cache + per-day parquet checkpoints.

    Set ``assemble=False`` for parallel year workers that only fill day shards.
    """
    raw_cache_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    done_path = checkpoint_dir / "completed_dates.txt"
    completed: set[str] = set()
    if done_path.exists():
        completed = {line.strip() for line in done_path.read_text().splitlines() if line.strip()}

    parsed_since_ckpt = 0
    with requests.Session() as session:
        # Warm NSE cookies (archives sometimes require a landing hit)
        try:
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=15)
            time.sleep(0.5)
        except requests.RequestException:
            pass

        current = start_date
        while current <= end_date:
            if current.weekday() < 5:
                key = current.isoformat()
                shard = checkpoint_dir / f"day_{key}.parquet"
                holiday = checkpoint_dir / f"holiday_{key}.flag"
                # Parallel-safe: day parquet / holiday flag are the source of truth
                if shard.exists() or holiday.exists():
                    if key not in completed:
                        with done_path.open("a") as handle:
                            handle.write(key + "\n")
                        completed.add(key)
                    current += timedelta(days=1)
                    continue
                path, fmt = fetch_and_cache_bhavcopy(
                    current, raw_cache_dir, session, sleep_sec=sleep_sec
                )
                wrote = False
                if path is not None:
                    parsed = parse_bhavcopy_zip(path, current, fmt)
                    if parsed is not None and not parsed.empty:
                        tmp = shard.with_suffix(".parquet.tmp")
                        parsed.to_parquet(tmp, index=False)
                        tmp.replace(shard)  # atomic on POSIX
                        wrote = True
                        parsed_since_ckpt += 1
                if not wrote:
                    holiday.write_text("no_eq_session\n", encoding="utf-8")
                if key not in completed:
                    with done_path.open("a") as handle:
                        handle.write(key + "\n")
                    completed.add(key)
                n_shards = len(list(checkpoint_dir.glob("day_*.parquet")))
                if len(completed) % 10 == 0:
                    print(
                        f"[ingest] {key} completed_weekdays={len(completed)} day_shards={n_shards}",
                        flush=True,
                    )
                if parsed_since_ckpt and parsed_since_ckpt % checkpoint_every == 0:
                    logger.info("Checkpoint progress through %s (%d shards)", key, n_shards)
            current += timedelta(days=1)

    if not assemble:
        n = len(list(checkpoint_dir.glob("day_*.parquet")))
        print(f"[ingest] assemble=False range={start_date}→{end_date} day_shards={n}", flush=True)
        return None

    shards = sorted(checkpoint_dir.glob("day_*.parquet"))
    if not shards:
        raise RuntimeError("No valid Bhavcopy files were parsed.")
    # Stream-concat in batches to bound peak memory
    chunks: list[pd.DataFrame] = []
    batch: list[pd.DataFrame] = []
    for i, shard in enumerate(shards, 1):
        try:
            batch.append(pd.read_parquet(shard))
        except Exception as exc:
            logger.warning("Skipping unreadable shard %s: %s", shard.name, exc)
            continue
        if len(batch) >= 60:
            chunks.append(pd.concat(batch, ignore_index=True))
            batch = []
        if i % 200 == 0:
            print(f"[ingest] loaded {i}/{len(shards)} day shards", flush=True)
    if batch:
        chunks.append(pd.concat(batch, ignore_index=True))
    if not chunks:
        raise RuntimeError("No readable Bhavcopy day shards.")
    ohlcv = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates(["date", "symbol"], keep="last")
        .sort_values(["date", "symbol"])
        .reset_index(drop=True)
    )
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    ohlcv.to_parquet(output_parquet, index=False, compression="snappy")
    logger.info("Saved %d rows to %s", len(ohlcv), output_parquet)
    return ohlcv


def fetch_static_nse_sector_map(symbols: list[str], ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Current NSE/Nifty sector classification mapped by symbol (STATIC fallback).

    Prefer archives.nseindia.com index constituent CSVs (Industry column). The
    www.nseindia.com JSON API is often blocked (403); CSV archives are reliable.
    Unmapped symbols get UNKNOWN / Unknown Macro so the PIT join still succeeds.
    """
    session = requests.Session()
    records: list[dict] = []
    csv_names = [
        "ind_niftytotalmarket_list.csv",
        "ind_nifty500list.csv",
        "ind_niftymidcap150list.csv",
        "ind_niftysmallcap250list.csv",
        "ind_nifty100list.csv",
        "ind_nifty50list.csv",
    ]
    for name in csv_names:
        for host in (
            "https://archives.nseindia.com/content/indices/",
            "https://nsearchives.nseindia.com/content/indices/",
        ):
            url = host + name
            try:
                resp = session.get(url, headers={**HEADERS, "Accept": "text/csv,*/*"}, timeout=30)
                if resp.status_code != 200 or "Company Name" not in resp.text[:200]:
                    continue
                frame = pd.read_csv(io.StringIO(resp.text))
                cols = {c.strip().lower(): c for c in frame.columns}
                sym_col = cols.get("symbol")
                ind_col = cols.get("industry")
                isin_col = cols.get("isin code") or cols.get("isin")
                if sym_col is None or ind_col is None:
                    continue
                for _, data in frame.iterrows():
                    sym = str(data.get(sym_col, "")).strip()
                    industry = str(data.get(ind_col, "")).strip() or "UNKNOWN"
                    isin = str(data.get(isin_col, "")).strip() if isin_col else None
                    if not sym or industry in {"", "UNKNOWN", "nan"}:
                        continue
                    records.append(
                        {
                            "symbol": sym,
                            "isin": isin or None,
                            "sector": industry,
                            "macro_sector": _macro_from_industry(industry),
                        }
                    )
                print(f"[sectors] loaded {name} rows={len(frame)}", flush=True)
                time.sleep(0.25)
                break
            except Exception as exc:
                logger.warning("Sector CSV %s failed: %s", url, exc)

    if not records:
        logger.warning("No sector CSVs loaded; all symbols will be UNKNOWN")

    # Later CSVs do not overwrite earlier richer Total Market mappings
    by_symbol: dict[str, dict] = {}
    for r in records:
        by_symbol.setdefault(r["symbol"], r)
    # Fill ISINs from OHLCV when API omitted them
    isin_map = (
        ohlcv.sort_values("date")
        .groupby("symbol")["isin"]
        .last()
        .to_dict()
        if "isin" in ohlcv.columns
        else {}
    )
    effective = pd.Timestamp(ohlcv["date"].min()) if len(ohlcv) else pd.Timestamp("2016-01-01")
    rows = []
    for sym in sorted(set(symbols)):
        hit = by_symbol.get(sym)
        if hit:
            rows.append(
                {
                    "effective_date": effective,
                    "symbol": sym,
                    "isin": hit.get("isin") or isin_map.get(sym),
                    "sector": hit["sector"],
                    "macro_sector": hit["macro_sector"],
                }
            )
        else:
            rows.append(
                {
                    "effective_date": effective,
                    "symbol": sym,
                    "isin": isin_map.get(sym),
                    "sector": "UNKNOWN",
                    "macro_sector": "Unknown Macro",
                }
            )
    return pd.DataFrame(rows)


def _macro_from_industry(industry: str) -> str:
    """Map NSE 22-industry labels onto ~12 macro buckets."""
    text = (industry or "").strip().lower()
    # Collapse NSE's ~22 industries into 12 macro buckets (distinct from 22-sector Option C).
    exact = {
        "financial services": "Financials",
        "capital goods": "Industrials",
        "information technology": "Technology",
        "healthcare": "Healthcare",
        "automobile and auto components": "Cyclicals",
        "oil gas & consumable fuels": "Energy",
        "oil, gas & consumable fuels": "Energy",
        "metals & mining": "Materials",
        "fast moving consumer goods": "Staples",
        "consumer durables": "Cyclicals",
        "consumer services": "Cyclicals",
        "realty": "Real Estate",
        "construction": "Industrials",
        "construction materials": "Materials",
        "power": "Utilities",
        "utilities": "Utilities",
        "telecommunication": "Communication",
        "media entertainment & publication": "Communication",
        "services": "Industrials",
        "chemicals": "Materials",
        "textiles": "Cyclicals",
        "forest materials": "Materials",
        "diversified": "Diversified",
    }
    if text in exact:
        return exact[text]
    rules = [
        (("bank", "financial", "nbfc", "insurance", "capital market"), "Financial Services"),
        (("information technology", "software", "it "), "Information Technology"),
        (("pharma", "healthcare", "hospital", "drug"), "Healthcare"),
        (("auto", "tyre", "automobile"), "Automobile and Auto Components"),
        (("oil", "gas", "petroleum", "refinery"), "Oil Gas & Consumable Fuels"),
        (("metal", "steel", "mining", "aluminium", "copper"), "Metals & Mining"),
        (("fmcg", "fast moving", "beverage", "food"), "Fast Moving Consumer Goods"),
        (("consumer durable",), "Consumer Durables"),
        (("consumer service", "retail", "hotel"), "Consumer Services"),
        (("realty", "real estate"), "Realty"),
        (("construction material", "cement"), "Construction Materials"),
        (("construction", "building"), "Construction"),
        (("power", "utility", "electric"), "Power"),
        (("telecom", "media"), "Media & Telecom"),
        (("chemical", "fertilizer"), "Chemicals"),
        (("capital good", "industrial"), "Capital Goods"),
        (("service",), "Services"),
    ]
    for keys, macro in rules:
        if any(k in text for k in keys):
            return macro
    return "Diversified"


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
