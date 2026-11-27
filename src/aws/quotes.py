"""Intraday / latest NSE quote fetch with timeout and EOD fallback."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

logger = logging.getLogger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _yahoo_last_price(symbol: str, timeout: float = 2.5) -> float | None:
    url = YAHOO_CHART.format(symbol=symbol)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, params={"range": "1d", "interval": "1m"})
        resp.raise_for_status()
        payload = resp.json()
        meta = (payload.get("chart") or {}).get("result") or []
        if not meta:
            return None
        price = meta[0].get("meta", {}).get("regularMarketPrice")
        if price is None:
            return None
        px = float(price)
        return px if px > 0 else None
    except Exception as exc:
        logger.debug("quote miss %s: %s", symbol, exc)
        return None


def fetch_live_quotes(
    symbols: list[str],
    *,
    eod_fallback: dict[str, float] | None = None,
    timeout_sec: float = 2.5,
    max_workers: int = 8,
) -> tuple[dict[str, float], bool]:
    """Return ({symbol: price}, stale_flag).

    Tries Yahoo Finance ``.NS`` last prices in parallel. Any miss falls back to
    ``eod_fallback`` (typically yesterday's close from ``latest_features.json``).
    ``stale_flag`` is True when any requested symbol used the EOD fallback or
    the live fetch failed entirely.
    """
    uniq = sorted({str(s).strip().upper() for s in symbols if s})
    eod = {str(k).upper(): float(v) for k, v in (eod_fallback or {}).items() if v is not None}
    if not uniq:
        return {}, False

    live: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_yahoo_last_price, sym, timeout_sec): sym for sym in uniq}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                px = fut.result()
            except Exception:
                px = None
            if px is not None:
                live[sym] = px

    out: dict[str, float] = {}
    stale = False
    for sym in uniq:
        if sym in live:
            out[sym] = live[sym]
        elif sym in eod:
            out[sym] = eod[sym]
            stale = True
        else:
            stale = True
    if not live:
        stale = True
    return out, stale
