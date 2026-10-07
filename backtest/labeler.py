"""
Replay the live entry rule over cached history and label 1:2 R:R outcomes.

Entry: EMA 9/21 cross + RSI 40–65 + BB position < 0.60
Label 1 if +3.0 ATR TP is hit before −1.5 ATR SL within 10 sessions, else 0.

Also reports rule-only win rate / payoff for Kelly assumption validation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from features import (
    FEATURE_NAMES,
    build_nifty_regime_series,
    enrich_indicators,
    features_at_index,
)
from swing_core.labels import label_triple_barrier

DATA_DIR = Path(__file__).resolve().parent / "data"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
HORIZON = 10
SL_MULT = 1.5
TP_MULT = 3.0


def is_entry(df: pd.DataFrame, i: int) -> bool:
    if i < 1:
        return False
    prev_ema9 = float(df["EMA_9"].iloc[i - 1])
    prev_ema21 = float(df["EMA_21"].iloc[i - 1])
    curr_ema9 = float(df["EMA_9"].iloc[i])
    curr_ema21 = float(df["EMA_21"].iloc[i])
    rsi = float(df["RSI"].iloc[i])
    close = float(df["Close"].iloc[i])
    lower = float(df["Lower_Band"].iloc[i])
    upper = float(df["Upper_Band"].iloc[i])
    width = upper - lower
    bb = (close - lower) / width if width > 0 else 1.0
    return (
        prev_ema9 <= prev_ema21
        and curr_ema9 > curr_ema21
        and 40 <= rsi <= 65
        and bb < 0.60
    )


def label_outcome(df: pd.DataFrame, i: int) -> tuple[int, str, float]:
    """
    Returns (label, exit_reason, r_multiple).
    Uses subsequent bars' High/Low to detect TP/SL hits.
    """
    entry = float(df["Close"].iloc[i])
    atr = float(df["ATR"].iloc[i])
    if atr <= 0 or np.isnan(atr):
        return 0, "bad_atr", 0.0
    stop = entry - SL_MULT * atr
    target = entry + TP_MULT * atr
    end = min(len(df) - 1, i + HORIZON)
    for j in range(i + 1, end + 1):
        low = float(df["Low"].iloc[j])
        high = float(df["High"].iloc[j])
        # Conservative: if both hit same bar, count as stop first
        if low <= stop:
            return 0, "stop", -1.0
        if high >= target:
            return 1, "target", 2.0
    # Time stop: mark-to-market vs entry in R units
    last = float(df["Close"].iloc[end])
    r = (last - entry) / (SL_MULT * atr)
    return (1 if last >= target else 0), "timeout", float(r)


def load_regime() -> pd.Series | None:
    path = DATA_DIR / "NSE_NIFTY.csv"
    if not path.exists():
        print("NSE_NIFTY.csv missing — regime_on defaults to 1.0")
        return None
    nifty = pd.read_csv(path)
    return build_nifty_regime_series(nifty)


def regime_at(regime: pd.Series | None, date) -> float:
    if regime is None:
        return 1.0
    d = pd.Timestamp(date).tz_localize(None).normalize()
    if d in regime.index:
        return float(regime.loc[d])
    # nearest prior date
    prior = regime.loc[:d]
    if prior.empty:
        return 1.0
    return float(prior.iloc[-1])


def process_file(path: Path, regime: pd.Series | None) -> list[dict]:
    raw = pd.read_csv(path)
    if len(raw) < 60:
        return []
    if "Date" in raw.columns:
        raw["Date"] = pd.to_datetime(raw["Date"])
    df = enrich_indicators(raw)
    events = []
    # Need room for indicators + forward horizon
    for i in range(55, len(df) - 1):
        if not is_entry(df, i):
            continue
        barrier = label_triple_barrier(df, i, qty=1, horizon=HORIZON)
        if barrier.label is None:
            continue  # censored / incomplete / unfilled
        label, reason, r_mult = barrier.label, barrier.reason, barrier.net_r
        date = df["Date"].iloc[i] if "Date" in df.columns else i
        reg = regime_at(regime, date)
        feats = features_at_index(df, i, regime_on=reg)
        events.append(
            {
                "symbol": path.stem,
                "date": str(pd.Timestamp(date).date()) if not isinstance(date, int) else date,
                "entry_price": float(df["Close"].iloc[i]),
                "atr": float(df["ATR"].iloc[i]),
                "label": label,
                "exit_reason": reason,
                "r_multiple": r_mult,
                **feats,
            }
        )
    return events


def main() -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p
        for p in DATA_DIR.glob("*.csv")
        if p.name != "NSE_NIFTY.csv"
    )
    if not files:
        raise SystemExit("No cached CSVs in backtest/data — run fetch_history.py first")

    regime = load_regime()
    all_events: list[dict] = []
    for path in files:
        ev = process_file(path, regime)
        print(f"{path.name}: {len(ev)} entries")
        all_events.extend(ev)

    if not all_events:
        raise SystemExit("No entry events found across universe")

    events = pd.DataFrame(all_events)
    out_path = ARTIFACTS / "events.csv"
    events.to_csv(out_path, index=False)

    wins = int(events["label"].sum())
    n = len(events)
    win_rate = wins / n
    # Payoff among resolved target/stop only
    resolved = events[events["exit_reason"].isin(["target", "stop"])]
    if len(resolved):
        avg_win = 2.0  # fixed R:R definition
        avg_loss = 1.0
        payoff = avg_win / avg_loss
        resolved_wr = resolved["label"].mean()
    else:
        payoff = float("nan")
        resolved_wr = float("nan")

    expect_r = events["r_multiple"].mean()
    print("=" * 50)
    print(f"Events: {n}")
    print(f"Rule win rate (TP before SL / all): {win_rate:.3f} ({wins}/{n})")
    print(f"Resolved-only win rate: {resolved_wr:.3f} (n={len(resolved)})")
    print(f"Assumed payoff_ratio (TP/SL R): {payoff}")
    print(f"Mean R-multiple (incl timeouts): {expect_r:.3f}")
    print(f"Exit reasons:\n{events['exit_reason'].value_counts().to_string()}")
    print(f"Saved {out_path}")
    print(
        "Kelly assumptions in QuantRiskManager were win_rate=0.50, payoff_ratio=2.0 — "
        f"empirical win_rate={win_rate:.2f}."
    )


if __name__ == "__main__":
    main()
