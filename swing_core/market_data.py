"""Bounded TradingView reads with one independent client per worker.
No Yahoo fallback in Lambda. Timeouts and stale prices are recorded, never padded.
"""

from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from tvDatafeed import TvDatafeed, Interval
from websocket import create_connection
from .india import clean_bars


class DeadlineSocket:
    def __init__(self, socket, deadline):
        self.socket = socket
        self.deadline = deadline

    def _check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("TradingView call deadline")

    def send(self, *a, **k):
        self._check()
        return self.socket.send(*a, **k)

    def recv(self):
        self._check()
        return self.socket.recv()

    def close(self):
        return self.socket.close()


class BoundedTv(TvDatafeed):
    def __init__(self, token=None, deadline=None):
        super().__init__()
        self.token = token or self.token
        self.deadline = deadline or time.monotonic() + 8

    def _TvDatafeed__create_connection(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("TradingView call deadline")
        socket = create_connection(
            "wss://data.tradingview.com/socket.io/websocket",
            headers=self._TvDatafeed__ws_headers,
            timeout=min(4, max(0.1, self.deadline - time.monotonic())),
        )
        self.ws = DeadlineSocket(socket, self.deadline)


def auth_token(username="", password=""):
    if not username or not password:
        return None
    response = requests.post(
        "https://www.tradingview.com/accounts/signin/",
        data={"username": username, "password": password, "remember": "on"},
        headers={"Referer": "https://www.tradingview.com"},
        timeout=(3, 6),
    )
    response.raise_for_status()
    return response.json()["user"]["auth_token"]


def fetch_bars(symbol, *, bars=400, token=None, budget_end=None, per_call_seconds=9):
    start = time.monotonic()
    deadline = min(start + per_call_seconds, budget_end or start + per_call_seconds)
    tv = None
    try:
        tv = BoundedTv(token, deadline)
        hist = tv.get_hist(
            symbol=symbol, exchange="NSE", interval=Interval.in_daily, n_bars=bars
        )
        if hist is None or len(hist) < 10:
            return None, {
                "symbol": symbol,
                "status": "EMPTY",
                "seconds": time.monotonic() - start,
            }
        data = clean_bars(hist)
        return data, {
            "symbol": symbol,
            "status": "OK",
            "bars": len(data),
            "last_date": str(data.index[-1].date()),
            "seconds": time.monotonic() - start,
        }
    except Exception as exc:
        return None, {
            "symbol": symbol,
            "status": "UNAVAILABLE",
            "error": type(exc).__name__,
            "seconds": time.monotonic() - start,
        }
    finally:
        if tv is not None and tv.ws is not None:
            try:
                tv.ws.close()
            except Exception:
                pass


def fetch_panel(symbols, *, bars=400, token=None, budget_seconds=220, workers=3):
    end = time.monotonic() + budget_seconds
    frames = {}
    health = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_bars, s, bars=bars, token=token, budget_end=end): s
            for s in dict.fromkeys(symbols)
        }
        for future in as_completed(futures):
            frame, report = future.result()
            health.append(report)
            if frame is not None:
                frames[report["symbol"]] = frame
    return frames, health
