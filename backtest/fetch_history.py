"""
Pull ~750 daily bars for the Screener universe via authenticated tvDatafeed.
Caches one CSV per symbol under backtest/data/ (gitignored).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from tvDatafeed import Interval, TvDatafeed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lambda_function import SYMBOL_MAP  # noqa: E402

load_dotenv(ROOT / ".env")

DATA_DIR = Path(__file__).resolve().parent / "data"
N_BARS = 750
SLEEP_SEC = 0.35


def load_universe(data_dir: Path | None = None) -> pd.DataFrame:
    data_dir = data_dir or (ROOT / "data")
    frames = []
    for csv in sorted(data_dir.glob("*.csv")):
        frame = pd.read_csv(csv)
        if "Symbol" not in frame.columns:
            continue
        cols = ["Symbol"] + (["Name"] if "Name" in frame.columns else [])
        frames.append(frame[cols])
    if not frames:
        raise FileNotFoundError(f"No screen CSVs with Symbol column in {data_dir}")
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


def fetch_one(tv: TvDatafeed, exchange: str, tv_symbol: str) -> pd.DataFrame | None:
    hist = tv.get_hist(
        symbol=tv_symbol,
        exchange=exchange,
        interval=Interval.in_daily,
        n_bars=N_BARS,
    )
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


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    universe = load_universe()
    print(f"Universe size: {len(universe)} symbols")
    tv = get_tv()

    ok, fail = 0, []
    for i, row in universe.iterrows():
        raw = str(row["Symbol"]).strip()
        exchange, tv_symbol, key = resolve_tv(raw)
        out_path = DATA_DIR / f"{exchange}_{tv_symbol}.csv"
        if out_path.exists() and out_path.stat().st_size > 100:
            print(f"[{i+1}/{len(universe)}] cache hit {key}")
            ok += 1
            continue
        try:
            hist = fetch_one(tv, exchange, tv_symbol)
            time.sleep(SLEEP_SEC)
            if hist is None or len(hist) < 50:
                fail.append(key)
                print(f"[{i+1}/{len(universe)}] FAIL {key}")
                continue
            hist.to_csv(out_path, index=False)
            ok += 1
            print(f"[{i+1}/{len(universe)}] ok {key} bars={len(hist)}")
        except Exception as exc:
            fail.append(f"{key} ({exc})")
            print(f"[{i+1}/{len(universe)}] ERR {key}: {exc}")
            time.sleep(SLEEP_SEC)

    # Also cache Nifty for regime features
    nifty_path = DATA_DIR / "NSE_NIFTY.csv"
    if not nifty_path.exists():
        try:
            nifty = fetch_one(tv, "NSE", "NIFTY")
            if nifty is not None:
                nifty.to_csv(nifty_path, index=False)
                print(f"Cached NIFTY bars={len(nifty)}")
        except Exception as exc:
            print(f"NIFTY cache failed: {exc}")

    print("=" * 50)
    print(f"Done. ok={ok} failed={len(fail)}")
    if fail:
        print("Failed:", fail)


if __name__ == "__main__":
    main()
