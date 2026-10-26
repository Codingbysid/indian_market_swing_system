"""Local research cache loader. Missing sector history is an exclusion, not a market-only fill-in."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "backtest" / "data"
UNIVERSE = ROOT / "universes" / "india_150.csv"
DENOMINATOR = 150
SECTOR_ATTEMPTS = {
    "Automobile and Auto Components": ["^CNXAUTO"],
    "Healthcare": ["^CNXPHARMA"],
    "Fast Moving Consumer Goods": ["^CNXFMCG"],
    "Metals & Mining": ["^CNXMETAL"],
    "Oil Gas & Consumable Fuels": ["^CNXENERGY"],
    "Information Technology": ["^CNXIT"],
    "Realty": ["^CNXREALTY"],
    "Financial Services": ["^CNXFIN", "NIFTY_FIN_SERVICE.NS"],
}


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    frame = frame.drop_duplicates("Date", keep="last").sort_values("Date")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def _align(frame: pd.DataFrame, dates: list[str]) -> dict[str, np.ndarray]:
    lookup = {day: index for index, day in enumerate(frame["Date"])}
    out = {name: np.full(len(dates), np.nan) for name in ("open", "high", "low", "close", "volume")}
    columns = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    for index, day in enumerate(dates):
        row = lookup.get(day)
        if row is None:
            continue
        for name, source in columns.items():
            out[name][index] = frame.iloc[row][source]
    return out


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    finite = np.isfinite(values)
    total = np.cumsum(np.where(finite, values, 0.0))
    count = np.cumsum(finite.astype(int))
    for index in range(window - 1, len(values)):
        start = index - window
        seen = count[index] - (count[start] if start >= 0 else 0)
        if seen == window:
            summed = total[index] - (total[start] if start >= 0 else 0.0)
            out[index] = summed / window
    return out


def _prior_median(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for index in range(window, len(values)):
        sample = values[index - window:index]
        if np.isfinite(sample).all():
            out[index] = float(np.median(sample))
    return out


def _gap_tail(open_: np.ndarray, close: np.ndarray, atr: np.ndarray, window: int = 60) -> np.ndarray:
    out = np.full(len(close), np.nan)
    gap = np.full(len(close), np.nan)
    gap[1:] = (open_[1:] - close[:-1]) / np.where(atr[:-1] > 0, atr[:-1], np.nan)
    adverse = np.minimum(gap, 0.0)
    for index in range(window, len(close)):
        sample = adverse[index - window:index]
        if np.isfinite(sample).sum() == window:
            out[index] = float(np.quantile(sample, 0.05))
    return out


def enrich(arrays: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    close = arrays["close"]
    previous = np.r_[np.nan, close[:-1]]
    true_range = np.maximum(arrays["high"] - arrays["low"], np.maximum(np.abs(arrays["high"] - previous), np.abs(arrays["low"] - previous)))
    atr5 = _rolling_mean(true_range, 5)
    atr20 = _rolling_mean(true_range, 20)
    returns = np.full(len(close), np.nan)
    returns[1:] = close[1:] / previous[1:] - 1.0
    volume_base = _prior_median(arrays["volume"], 20)
    turnover = _prior_median(close * arrays["volume"], 20)
    alpha = 2.0 / 21.0
    ema20 = np.full(len(close), np.nan)
    current = np.nan
    for index, price in enumerate(close):
        if not np.isfinite(price):
            current = np.nan
            continue
        current = price if not np.isfinite(current) else alpha * price + (1.0 - alpha) * current
        ema20[index] = current
    arrays = dict(arrays)
    arrays.update({
        "return": returns,
        "atr5": atr5,
        "atr20": atr20,
        "atr_compression": atr5 / np.where(atr20 > 0, atr20, np.nan),
        "volume_impulse": arrays["volume"] / np.where(volume_base > 0, volume_base, np.nan),
        "turnover_prior": turnover,
        "ema20": ema20,
        "gap_tail": _gap_tail(arrays["open"], close, atr20),
    })
    return arrays


def fetch_chart(symbol: str, timeout: int = 20) -> tuple[dict, pd.DataFrame | None]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol) + "?interval=1d&range=10y"
    request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-research-v6"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except Exception as exc:
        return {"symbol": symbol, "url": url, "error": type(exc).__name__, "parsed": False}, None
    try:
        payload = json.loads(body)
        block = payload["chart"]["result"][0]
        quote = block["indicators"]["quote"][0]
        frame = pd.DataFrame({
            "Date": pd.to_datetime(block["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").strftime("%Y-%m-%d"),
            "Close": quote["close"],
        }).dropna().drop_duplicates("Date").reset_index(drop=True)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return {"symbol": symbol, "url": url, "sha256": hashlib.sha256(body).hexdigest(), "parsed": False, "bytes": len(body)}, None
    record = {"symbol": symbol, "url": url, "sha256": hashlib.sha256(body).hexdigest(), "parsed": len(frame) > 200, "bytes": len(body), "rows": int(len(frame))}
    return record, frame if record["parsed"] else None


def load_sectors(dates: list[str], cache: Path) -> tuple[dict[str, np.ndarray], list[dict]]:
    cache.mkdir(parents=True, exist_ok=True)
    series = {}
    attempts = []
    for sector, tickers in SECTOR_ATTEMPTS.items():
        accepted = None
        for ticker in tickers:
            record, frame = fetch_chart(ticker)
            record["sector"] = sector
            attempts.append(record)
            if frame is not None:
                destination = cache / f"{sector.replace(' ', '_')}.csv"
                frame.to_csv(destination, index=False)
                aligned = _align(frame.rename(columns={"Close": "Close"}).assign(Open=frame["Close"], High=frame["Close"], Low=frame["Close"], Volume=0), dates)
                close = aligned["close"]
                returns = np.full(len(close), np.nan)
                returns[1:] = close[1:] / np.r_[np.nan, close[:-1]][1:] - 1.0
                accepted = returns
                record["archive"] = str(destination)
                break
        if accepted is not None:
            series[sector] = accepted
    return series, attempts


def load_study(sector_cache: Path | None = None) -> dict:
    universe = pd.read_csv(UNIVERSE)
    nifty = _frame(DATA_DIR / "NSE_NIFTY.csv")
    dates = nifty["Date"].tolist()
    panels = {}
    missing = []
    duplicates = []
    hashes = {"NSE_NIFTY.csv": file_sha(DATA_DIR / "NSE_NIFTY.csv")}
    for symbol in universe["Symbol"].astype(str).str.upper():
        path = DATA_DIR / f"NSE_{symbol}.csv"
        if not path.exists():
            missing.append(symbol)
            continue
        frame = _frame(path)
        raw = pd.read_csv(path)
        raw_dates = pd.to_datetime(raw["Date"]).dt.strftime("%Y-%m-%d")
        if int(raw_dates.duplicated().sum()):
            duplicates.append(symbol)
            continue
        panels[symbol] = enrich(_align(frame, dates))
        hashes[path.name] = file_sha(path)
    sectors, attempts = ({}, [])
    if sector_cache is not None:
        sectors, attempts = load_sectors(dates, sector_cache)
    sector_of = dict(zip(universe["Symbol"].astype(str).str.upper(), universe["Sector"].astype(str)))
    return {
        "dates": dates,
        "nifty": enrich(_align(nifty, dates)),
        "panels": panels,
        "sector_of": sector_of,
        "sector_returns": sectors,
        "sector_attempts": attempts,
        "missing": missing,
        "duplicate_dates": duplicates,
        "denominator": DENOMINATOR,
        "hashes": hashes,
        "provider": "tvDatafeed_cache",
        "adjustment": "tvdatafeed_as_returned",
        "universe_as_of": "2026-10-13",
        "bars": {
            symbol: {
                day: {"open": float(panel["open"][index]), "high": float(panel["high"][index]), "low": float(panel["low"][index]), "close": float(panel["close"][index])}
                for index, day in enumerate(dates) if np.isfinite(panel["close"][index])
            }
            for symbol, panel in panels.items()
        },
    }
