"""One-vendor daily bars for an already verified symbol. Failures stay failures."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from swing_v5.sessions import IST


def yahoo_bars(symbol: str, *, timeout: float = 12) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?interval=1d&range=10y"
    request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-ops-research"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    rows = []
    for stamp, open_, high, low, close, volume in zip(
        result["timestamp"], quote["open"], quote["high"], quote["low"], quote["close"], quote["volume"]
    ):
        if None in (open_, high, low, close, volume):
            continue
        rows.append({
            "Date": datetime.fromtimestamp(stamp, IST).date().isoformat(),
            "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume,
        })
    return {
        "vendor": "yahoo_chart",
        "adjustment": "yahoo_split_adjusted_ohlc",
        "symbol": symbol,
        "retrieved_at": datetime.now(IST).isoformat(timespec="seconds"),
        "rows": rows,
    }


def yahoo_index_close(*, timeout: float = 12) -> list[dict]:
    request = urllib.request.Request(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=10y",
        headers={"User-Agent": "indian-swing-ops-research"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode())
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    rows = []
    for stamp, close in zip(result["timestamp"], quote["close"]):
        if close is None:
            continue
        rows.append({"Date": datetime.fromtimestamp(stamp, IST).date().isoformat(), "Close": close})
    return rows
