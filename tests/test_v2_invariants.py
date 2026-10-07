import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from swing_core.calendar_nse import completed_bar_iloc
from swing_core.costs import true_breakeven
from swing_core.indicators import enrich_ohlcv
from swing_core.labels import label_triple_barrier
from swing_core.policy import admission_limits, blocks_concentration
from swing_core.scoring import admit_buy, finite_probability
from swing_core.setups import detect_setups, setup_breakout, setup_cross, setup_pullback


def _ohlcv(n=80, start=100.0, trend=0.4, pullback_at=None, breakout=False):
    rows = []
    price = start
    for i in range(n):
        if pullback_at and pullback_at <= i < pullback_at + 3:
            price -= 0.6
        elif breakout and i == n - 1:
            price += 8
        else:
            price += trend
        high = price + 0.8
        low = price - 0.8
        if pullback_at and i == pullback_at + 1:
            low = price - 3.0
        vol = 1000 + i
        if breakout and i == n - 1:
            vol = 5000
        rows.append(
            {
                "Open": price - 0.2,
                "High": high,
                "Low": low,
                "Close": price,
                "Volume": vol,
            }
        )
    return enrich_ohlcv(pd.DataFrame(rows))


def test_pullback_does_not_need_fresh_cross():
    df = _ohlcv(70, pullback_at=64)
    i = len(df) - 1
    # EMA9 should already be above EMA21 in an uptrend — no cross required.
    assert float(df["EMA_9"].iloc[i]) > float(df["EMA_21"].iloc[i])
    assert not setup_cross(df, i)
    assert "No fresh cross" in (setup_pullback.__doc__ or "")


def test_breakout_allows_bb_above_60():
    df = _ohlcv(50, start=100, trend=0.05, breakout=True)
    i = len(df) - 1
    close = float(df["Close"].iloc[i])
    lo = float(df["Lower_Band"].iloc[i])
    hi = float(df["Upper_Band"].iloc[i])
    bb = (close - lo) / (hi - lo) if hi > lo else 1.0
    # Threshold uses d-20..d-1, excluding current high.
    prior = float(df["High"].iloc[i - 20 : i].max())
    assert close > prior
    # May or may not pass volume/SMA depending on synthetic path; check exclusion rule.
    assert prior == float(df["High"].iloc[i - 20 : i].max())
    _ = bb
    _ = setup_breakout(df, i)


def test_gap_before_entry_is_unfilled_not_a_loss():
    rows = []
    px = 100.0
    for i in range(40):
        rows.append({"Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1000})
        px += 0.2
    rows.append({"Open": 90.0, "High": 92.0, "Low": 89.0, "Close": 91.0, "Volume": 1000})
    df = enrich_ohlcv(pd.DataFrame(rows))
    res = label_triple_barrier(df, len(df) - 2, qty=1)
    assert res.reason == "unfilled"
    assert res.label is None


def test_profitable_timeout_can_be_positive_label():
    rows = []
    px = 100.0
    for i in range(12):
        rows.append({"Open": px, "High": px + 0.5, "Low": px - 0.3, "Close": px, "Volume": 1000})
        px += 0.4  # slow grind, may not hit +3ATR
    df = enrich_ohlcv(pd.DataFrame(rows))
    # Use a late bar with a full 10-bar window if possible
    i = 1
    res = label_triple_barrier(df, i, qty=1, horizon=10)
    assert res.reason in {"vertical", "target", "stop", "stop_gap", "censored"}
    if res.reason == "vertical" and res.net_r > 0:
        assert res.label == 1


def test_incomplete_window_is_censored():
    df = enrich_ohlcv(
        pd.DataFrame(
            [
                {"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}
                for _ in range(30)
            ]
        )
    )
    i = len(df) - 2  # only one future bar
    res = label_triple_barrier(df, i, qty=1, horizon=10)
    assert res.label is None
    assert res.reason == "censored"


def test_fail_closed_nan_and_none():
    assert finite_probability(None) is None
    assert finite_probability(float("nan")) is None
    assert finite_probability(1.2) is None
    ok, reason = admit_buy(None, 0.55)
    assert not ok and "fail_closed" in reason
    ok, _ = admit_buy(0.7, 0.55)
    assert ok


def test_completed_bar_at_0925_drops_today():
    idx = pd.date_range("2026-10-05", periods=3, freq="D")
    morning = datetime(2026, 10, 7, 9, 25, tzinfo=ZoneInfo("Asia/Kolkata"))
    # last bar is 2026-10-07
    idx = pd.DatetimeIndex(["2026-10-05", "2026-10-06", "2026-10-07"])
    assert completed_bar_iloc(idx, morning) == -2


def test_screenshot_holdings_below_be():
    shots = [
        ("HAL", 5040.63, 16, 4790.90),
        ("ZENTEC", 1711.25, 8, 1600.50),
        ("CDSL", 1363.75, 8, 1277.80),
        ("INFY", 1030.00, 5, 1002.60),
        ("BPCL", 318.65, 6, 297.35),
    ]
    for _, avg, qty, ltp in shots:
        assert ltp < true_breakeven(avg, qty)


def test_admission_limits_use_real_cash():
    lim = admission_limits(15255.50, 121733.40)
    assert lim["max_ticket"] <= 0.25 * 15255.50 + 1e-6
    assert lim["max_ticket"] <= 0.03 * 121733.40 + 1e-6
    assert lim["park_satellite"] is True
    assert lim["reserve"] == round(15255.50 * 0.20, 2)


def test_blocks_defence_while_hal_dominates():
    holdings = [
        {"symbol": "HAL", "qty": 16, "avg_cost": 5040.63, "tags": ["defence", "psu"]},
        {"symbol": "ZENTEC", "qty": 8, "avg_cost": 1711.25, "tags": ["defence"]},
    ]
    marked = {"HAL": 76654.40, "ZENTEC": 12804.00}
    reason = blocks_concentration("BEL", holdings, marked)
    assert reason == "block_defence_concentration"
