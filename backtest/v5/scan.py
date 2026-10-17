"""Price-only trend-transition control. Fundamental Q/V history is not backfilled."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from swing_v5.execution import label_trade
from swing_v5.features import FEATURES_PRICE, enrich, row_features
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "backtest" / "data"


def _load(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.tz_localize(None).dt.normalize()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).drop_duplicates("Date").sort_values("Date")
    return enrich(frame.reset_index(drop=True))


def price_only_events(limit: int | None = None) -> tuple[pd.DataFrame, dict]:
    universe = pd.read_csv(ROOT / "universes" / "india_150.csv")
    symbols = universe["Symbol"].astype(str).str.upper().tolist()
    if limit:
        symbols = symbols[:limit]
    nifty_path = DATA / "NSE_NIFTY.csv"
    blocked = {
        "quality_continuation": "blocked_no_point_in_time_fundamental_snapshots",
        "improving_value": "blocked_no_point_in_time_fundamental_snapshots",
        "delivery": "unavailable_not_zero",
        "sentiment_history": "unavailable_not_backfilled",
        "restriction_metadata": "missing_blocks_live_admission",
        "holiday_calendar": "weekday_only_not_official_nse",
    }
    if not nifty_path.exists():
        return pd.DataFrame(), {**blocked, "prices": "blocked_missing_nifty"}
    nifty = _load(nifty_path)
    nifty["EMA50"] = nifty["Close"].ewm(span=50, adjust=False).mean()
    rows = []
    frames = {}
    above = []
    coverage = {"requested": len(symbols), "loaded": 0, "missing": [], "short": []}
    for symbol in symbols:
        path = DATA / f"NSE_{symbol}.csv"
        if not path.exists():
            coverage["missing"].append(symbol)
            continue
        frame = _load(path)
        if len(frame) < 300:
            coverage["short"].append(symbol)
            continue
        coverage["loaded"] += 1
        frames[symbol] = frame
        flag = (frame["Close"] > frame["SMA50"]).astype(float)
        flag.index = frame["Date"]
        above.append(flag.rename(symbol))
    breadth_frame = pd.concat(above, axis=1) if above else pd.DataFrame()
    if len(breadth_frame):
        coverage_ratio = breadth_frame.notna().mean(axis=1)
        breadth = breadth_frame.mean(axis=1).where(coverage_ratio >= 0.90)
    else:
        breadth = pd.Series(dtype=float)
    for symbol, frame in frames.items():
        aligned = nifty.set_index("Date").reindex(frame["Date"]).reset_index(drop=True)
        turn20 = (frame["Close"] * frame["Volume"]).rolling(20).median()
        turn60 = (frame["Close"] * frame["Volume"]).rolling(60).median()
        stock_r = frame["Close"] / frame["Close"].shift(63) - 1
        nifty_r = aligned["Close"] / aligned["Close"].shift(63) - 1
        rel = stock_r - nifty_r
        cross = (frame["SMA50"] > frame["SMA200"]) & (frame["SMA50"].shift(1) <= frame["SMA200"].shift(1))
        recent = cross.rolling(10, min_periods=1).max().fillna(0).astype(bool)
        mask = (
            recent
            & (frame["SMA200"] >= frame["SMA200"].shift(20))
            & (rel > 0)
            & (frame["Close"] > frame["High"].shift(1).rolling(20).max())
            & (frame["Volume"] >= 1.2 * frame["Volume"].shift(1).rolling(20).median())
            & (turn60 >= 100_000_000)
            & (turn20 >= 50_000_000)
        )
        indices = np.flatnonzero(mask.fillna(False).to_numpy())
        for i in indices:
            if i < 300 or i >= len(frame) - 41:
                continue
            long = label_trade(frame, i, horizon="long")
            short = label_trade(frame, i, horizon="short")
            day = frame["Date"].iloc[i]
            breadth_value = float(breadth.get(day, np.nan)) if len(breadth) else float("nan")
            feats = row_features(frame, i, nifty=aligned, breadth=breadth_value, price_only=True)
            # breadth is attached by the caller after the panel exists; 0 is replaced below when known.
            rows.append(
                {
                    "symbol": symbol,
                    "date": str(pd.Timestamp(frame["Date"].iloc[i]).date()),
                    "family": "trend_transition_price_control",
                    "horizon_primary": "long",
                    "long_status": long["status"],
                    "long_label": long.get("label"),
                    "long_net_r": long.get("net_r"),
                    "long_net_pnl": long.get("net_pnl"),
                    "long_reason": long.get("reason"),
                    "short_status": short["status"],
                    "short_label": short.get("label"),
                    "short_net_r": short.get("net_r"),
                    "exit_date": str(pd.Timestamp(frame["Date"].iloc[min(len(frame) - 1, i + int(long.get("sessions") or 1))]).date()) if long.get("sessions") else None,
                    "entry": long.get("entry"),
                    "atr": float(frame["ATR"].iloc[i]),
                    "close": float(frame["Close"].iloc[i]),
                    "features_ok": feats is not None,
                    **(feats or {name: None for name in FEATURES_PRICE}),
                }
            )
    events = pd.DataFrame(rows)
    coverage["events"] = int(len(events))
    coverage.update(blocked)
    return events, coverage
