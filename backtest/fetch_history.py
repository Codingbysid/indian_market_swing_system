"""
Pull daily bars for an explicit universe via authenticated tvDatafeed.

python -m backtest.fetch_history --universe universes/nifty_200.csv --bars 2000 --refresh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from tvDatafeed import Interval, TvDatafeed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from swing_core.symbols import SYMBOL_MAP  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
N_BARS = 2000
SLEEP_SEC = 0.35


class _FetchTimeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _FetchTimeout()


def load_universe(data_dir: Path | None = None, universe_file: Path | None = None) -> pd.DataFrame:
    if universe_file is not None:
        frame = pd.read_csv(universe_file)
        if "Symbol" not in frame.columns:
            raise SystemExit(f"{universe_file} has no Symbol column")
        out = frame.dropna(subset=["Symbol"]).copy()
        out["Symbol"] = out["Symbol"].astype(str).str.strip().str.upper()
        return out.drop_duplicates("Symbol").reset_index(drop=True)
    data_dir = data_dir or (ROOT / "data")
    frames = []
    for csv in sorted(data_dir.glob("*.csv")):
        frame = pd.read_csv(csv)
        if "Symbol" not in frame.columns:
            continue
        cols = ["Symbol"] + (["Name"] if "Name" in frame.columns else [])
        frames.append(frame[cols])
    if not frames:
        raise FileNotFoundError(f"No screen CSVs with Symbol column in {data_dir}. Pass --universe.")
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["Symbol"])
    df = df.dropna(subset=["Symbol"])
    df["Symbol"] = df["Symbol"].astype(str).str.strip()
    return df.reset_index(drop=True)


def resolve_tv(raw_symbol: str) -> tuple[str, str, str]:
    exchange = "BSE" if raw_symbol.isdigit() else "NSE"
    tv_symbol = SYMBOL_MAP.get(raw_symbol, raw_symbol)
    return exchange, tv_symbol, f"{exchange}:{tv_symbol}"


def get_tv() -> TvDatafeed:
    username = os.getenv("TV_USERNAME", "").strip()
    password = os.getenv("TV_PASSWORD", "").strip()
    if username and password:
        print("TvDatafeed: authenticated login")
        return TvDatafeed(username=username, password=password)
    print("TvDatafeed: guest mode (set TV_USERNAME/TV_PASSWORD for better limits)")
    return TvDatafeed()


def fetch_one(tv: TvDatafeed, exchange: str, tv_symbol: str, n_bars: int = N_BARS, timeout: int = 45) -> pd.DataFrame | None:
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)
    try:
        hist = tv.get_hist(
            symbol=tv_symbol,
            exchange=exchange,
            interval=Interval.in_daily,
            n_bars=n_bars,
        )
    finally:
        signal.alarm(0)
    if hist is None or hist.empty:
        return None
    out = hist.reset_index()
    # Normalize column names
    rename = {
        "datetime": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
        "symbol": "tv_symbol",
    }
    out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})
    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in out.columns]
    return out[keep]


def _history_stats(path: Path) -> dict:
    frame = pd.read_csv(path)
    frame["Date"] = pd.to_datetime(frame["Date"])
    dupes = int(frame["Date"].duplicated().sum())
    ordered = frame.drop_duplicates("Date").sort_values("Date")
    days = pd.bdate_range(ordered["Date"].min(), ordered["Date"].max())
    missing = int(len(set(days.date) - set(ordered["Date"].dt.date)))
    return {
        "bars": int(len(ordered)),
        "first": str(ordered["Date"].min().date()),
        "last": str(ordered["Date"].max().date()),
        "duplicate_dates": dupes,
        "missing_weekdays": missing,
    }


def fetch_yahoo(symbol: str, n_bars: int) -> pd.DataFrame | None:
    """Public chart fallback. OHLC is the vendor's split-adjusted series; adjclose is not used."""
    yahoo_symbol = "^NSEI" if symbol == "NIFTY" else f"{symbol}.NS"
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(yahoo_symbol)
        + "?interval=1d&range=10y"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-research"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    result = (payload.get("chart") or {}).get("result") or []
    if not result or not result[0].get("timestamp"):
        return None
    block = result[0]
    quote = block["indicators"]["quote"][0]
    frame = pd.DataFrame(
        {
            "Date": pd.to_datetime(block["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None).normalize(),
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        }
    )
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).drop_duplicates("Date").sort_values("Date")
    if len(frame) < 50:
        return None
    return frame.tail(n_bars).reset_index(drop=True)


def tv_available() -> tuple[bool, str]:
    script = """
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.env'))
from tvDatafeed import Interval, TvDatafeed
user = os.getenv('TV_USERNAME', '').strip()
password = os.getenv('TV_PASSWORD', '').strip()
client = TvDatafeed(username=user, password=password) if user and password else TvDatafeed()
hist = client.get_hist(symbol='NIFTY', exchange='NSE', interval=Interval.in_daily, n_bars=30)
print('TV_OK' if hist is not None and len(hist) >= 10 else 'TV_EMPTY')
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            timeout=25,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return False, "tvDatafeed sign-in failed and the nologin client did not return within 25s"
    if "TV_OK" in proc.stdout:
        return True, "tvDatafeed"
    if "error while signin" in (proc.stdout + proc.stderr):
        return False, "tvDatafeed rejected the saved login and the guest client did not return bars"
    return False, "tvDatafeed returned no NIFTY bars"


def _write_manifest(universe_file: Path | None, requested: list[str], ok: list[str], fail: list[str], n_bars: int, source: str, source_note: str) -> None:
    out = ROOT / "backtest" / "artifacts" / "v3"
    out.mkdir(parents=True, exist_ok=True)
    raw = universe_file.read_bytes() if universe_file else b""
    histories = {}
    for symbol in ok:
        path = DATA_DIR / f"NSE_{symbol}.csv"
        if path.exists():
            histories[symbol] = _history_stats(path)
    nifty_path = DATA_DIR / "NSE_NIFTY.csv"
    nifty_stats = _history_stats(nifty_path) if nifty_path.exists() else None
    lengths = [row["bars"] for row in histories.values()]
    manifest = {
        "fetched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe_file": str(universe_file.relative_to(ROOT)) if universe_file else None,
        "universe_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "price_source": source,
        "price_source_note": source_note,
        "adjustment_mode": "yahoo_split_adjusted_ohlc_adjclose_not_applied" if source == "yahoo_chart" else "tvdatafeed_as_returned",
        "requested_bars": n_bars,
        "requested_symbols": len(requested),
        "successful_symbols": len(ok),
        "failed": fail,
        "bar_count_min": min(lengths) if lengths else 0,
        "bar_count_median": float(pd.Series(lengths).median()) if lengths else 0,
        "histories": histories,
        "nifty": nifty_stats,
        "freshness_note": "last session is the newest cached daily bar, not an intraday quote",
        "survivorship": "current index membership; not point-in-time constituent history",
        "holiday_calendar": "weekday gaps only; NSE holidays are not in this manifest",
        "market_cap_filter": "unavailable_no_timestamped_source",
    }
    (out / "data_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {out / 'data_manifest.json'} successful={len(ok)} failed={len(fail)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=None)
    parser.add_argument("--bars", type=int, default=N_BARS)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    universe_file = (ROOT / args.universe) if args.universe else None
    universe = load_universe(universe_file=universe_file)
    print(f"Universe size: {len(universe)} symbols; bars={args.bars}; refresh={args.refresh}", flush=True)
    print(f"tv_username_set={bool(os.getenv('TV_USERNAME', '').strip())}", flush=True)
    tv_ok, tv_note = tv_available()
    source = "tvDatafeed" if tv_ok else "yahoo_chart"
    print(f"price_source={source} note={tv_note}", flush=True)
    ok, fail = [], []
    nifty_path = DATA_DIR / "NSE_NIFTY.csv"

    def save_hist(path: Path, hist: pd.DataFrame) -> None:
        hist.to_csv(path, index=False)

    if source == "tvDatafeed":
        tv = get_tv()
        if args.refresh or not nifty_path.exists():
            try:
                nifty = fetch_one(tv, "NSE", "NIFTY", n_bars=args.bars)
                if nifty is not None:
                    save_hist(nifty_path, nifty)
                    print(f"Cached NIFTY bars={len(nifty)}", flush=True)
            except Exception as exc:
                print(f"NIFTY cache failed: {type(exc).__name__}", flush=True)
        for i, row in universe.iterrows():
            raw = str(row["Symbol"]).strip().upper()
            exchange, tv_symbol, key = resolve_tv(raw)
            out_path = DATA_DIR / f"{exchange}_{tv_symbol}.csv"
            if out_path.exists() and out_path.stat().st_size > 100 and not args.refresh:
                ok.append(tv_symbol)
                continue
            try:
                hist = fetch_one(tv, exchange, tv_symbol, n_bars=args.bars)
                time.sleep(SLEEP_SEC)
                if hist is None or len(hist) < 50:
                    fail.append({"symbol": key, "reason": "empty_or_short"})
                    continue
                save_hist(out_path, hist)
                ok.append(tv_symbol)
                print(f"[{i+1}/{len(universe)}] ok {key} bars={len(hist)}", flush=True)
            except Exception as exc:
                fail.append({"symbol": key, "reason": type(exc).__name__})
                print(f"[{i+1}/{len(universe)}] ERR {key}: {type(exc).__name__}", flush=True)
    else:
        try:
            nifty = fetch_yahoo("NIFTY", args.bars)
            if nifty is not None:
                save_hist(nifty_path, nifty)
                print(f"Cached NIFTY bars={len(nifty)}", flush=True)
            else:
                fail.append({"symbol": "NIFTY", "reason": "empty_or_short"})
        except Exception as exc:
            fail.append({"symbol": "NIFTY", "reason": type(exc).__name__})
            print(f"NIFTY cache failed: {type(exc).__name__}", flush=True)
        for i, row in universe.iterrows():
            raw = str(row["Symbol"]).strip().upper()
            out_path = DATA_DIR / f"NSE_{raw}.csv"
            if out_path.exists() and out_path.stat().st_size > 100 and not args.refresh:
                ok.append(raw)
                continue
            try:
                hist = fetch_yahoo(raw, args.bars)
                time.sleep(0.15)
                if hist is None or len(hist) < 50:
                    fail.append({"symbol": raw, "reason": "empty_or_short"})
                    print(f"[{i+1}/{len(universe)}] FAIL {raw}", flush=True)
                    continue
                save_hist(out_path, hist)
                ok.append(raw)
                if (i + 1) % 20 == 0 or i == 0:
                    print(f"[{i+1}/{len(universe)}] ok {raw} bars={len(hist)}", flush=True)
            except Exception as exc:
                fail.append({"symbol": raw, "reason": type(exc).__name__})
                print(f"[{i+1}/{len(universe)}] ERR {raw}: {type(exc).__name__}", flush=True)
                time.sleep(0.5)
    _write_manifest(universe_file, universe["Symbol"].tolist(), ok, fail, args.bars, source, tv_note)
    print(f"Done. ok={len(ok)} failed={len(fail)}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
