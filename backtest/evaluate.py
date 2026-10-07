"""Count setup families on cached daily bars. Does not invent out-of-sample returns."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swing_core.indicators import enrich_ohlcv
from swing_core.strategies import detect, primary_family

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent / "artifacts" / "v3"


def _load(path: Path) -> pd.DataFrame | None:
    raw = pd.read_csv(path)
    cols = {c.lower(): c for c in raw.columns}
    need = ("open", "high", "low", "close", "volume")
    if not all(k in cols for k in need):
        return None
    out = pd.DataFrame(
        {
            "Open": raw[cols["open"]],
            "High": raw[cols["high"]],
            "Low": raw[cols["low"]],
            "Close": raw[cols["close"]],
            "Volume": raw[cols["volume"]],
        }
    )
    if "date" in cols:
        out["Date"] = pd.to_datetime(raw[cols["date"]])
    return enrich_ohlcv(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    nifty_path = DATA / "NSE_NIFTY.csv"
    nifty = _load(nifty_path) if nifty_path.exists() else None
    counts: Counter[str] = Counter()
    symbols = 0
    for path in sorted(DATA.glob("*.csv")):
        if path.name == "NSE_NIFTY.csv":
            continue
        df = _load(path)
        if df is None or len(df) < 80:
            continue
        symbols += 1
        nifty_ret = None
        for i in range(70, len(df) - 11):
            if nifty is not None and "Date" in df.columns and "Date" in nifty.columns:
                day = pd.Timestamp(df["Date"].iloc[i]).normalize()
                prior = pd.Timestamp(df["Date"].iloc[i - 20]).normalize()
                now = nifty.loc[nifty["Date"] == day, "Close"]
                then = nifty.loc[nifty["Date"] == prior, "Close"]
                if len(now) and len(then) and float(then.iloc[-1]) > 0:
                    nifty_ret = float(now.iloc[-1]) / float(then.iloc[-1]) - 1.0
            flags = detect(df, i, nifty_ret20=nifty_ret)
            primary = primary_family(flags)
            if primary:
                counts[primary] += 1
    payload = {
        "universe_bias": "current_cached_screener_names_not_point_in_time_nifty_200",
        "symbols_scanned": symbols,
        "raw_primary_events": dict(counts),
        "oos_promoted": False,
        "reason": "No purged walk-forward model was fit. Counts are in-sample detector hits, not validated edge.",
        "nifty_200_history": "not_fetched",
    }
    (OUT / "coverage.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
