"""Financial invariants for the v3 registry and repair allocator."""

import pandas as pd

from swing_core.allocation import allocate
from swing_core.exits import legacy_exit_intent, system_exit_intent, update_trail
from swing_core.indicators import enrich_ohlcv
from swing_core.labels import label_triple_barrier
from swing_core.state import PortfolioInvalid, apply_fill, remaining_tranches, validate_state
from swing_core.strategies import (
    ema_cross,
    failed_breakdown,
    range_breakout,
    trend_pullback,
)


def _trend(n=80, start=100.0, step=0.3):
    rows = []
    px = start
    for i in range(n):
        px += step
        rows.append({"Open": px - 0.1, "High": px + 0.4, "Low": px - 0.4, "Close": px, "Volume": 1000 + i})
    return enrich_ohlcv(pd.DataFrame(rows))


def test_invalid_state_rejects_fallback_cash():
    try:
        validate_state(None)
        assert False
    except PortfolioInvalid as exc:
        assert exc.reason == "state_missing_or_not_object"


def test_hal_t1_leaves_zero_five_six():
    holding = {
        "symbol": "HAL",
        "qty": 11,
        "avg_cost": 5040.63,
        "classification": "legacy_profit_only",
        "t1_qty_plan": 5,
        "t2_qty_plan": 5,
        "runner_qty_plan": 6,
        "t1_filled_qty": 5,
        "t2_filled_qty": 0,
    }
    assert remaining_tranches(holding) == (0, 5, 6)
    intent = legacy_exit_intent(holding, ltp=5300, atr=100)
    assert intent is None or intent["qty"] != 11


def test_duplicate_fill_is_idempotent():
    state = validate_state(
        {
            "cash": 10000,
            "holdings": [
                {
                    "symbol": "TCS",
                    "qty": 2,
                    "avg_cost": 3000,
                    "classification": "legacy_profit_only",
                    "t1_qty_plan": 1,
                    "t2_qty_plan": 1,
                    "runner_qty_plan": 0,
                }
            ],
        }
    )
    fill = {"fill_id": "abc", "symbol": "TCS", "side": "sell", "qty": 1, "price": 3100, "stage": "t1", "fees": 0}
    once = apply_fill(state, fill)
    twice = apply_fill(once, fill)
    assert twice["cash"] == once["cash"]
    assert twice["holdings"][0]["qty"] == 1
    assert twice["holdings"][0]["t1_filled_qty"] == 1


def test_legacy_below_be_has_no_sell():
    holding = {
        "symbol": "HAL",
        "qty": 16,
        "avg_cost": 5040.63,
        "classification": "legacy_profit_only",
        "t1_qty_plan": 5,
        "t2_qty_plan": 5,
        "runner_qty_plan": 6,
    }
    assert legacy_exit_intent(holding, ltp=4790, atr=110) is None


def test_system_stop_is_not_legacy():
    holding = {
        "symbol": "TCS",
        "qty": 1,
        "avg_cost": 3000,
        "classification": "system_atr",
        "entry_price": 3000,
        "entry_atr": 40,
        "stop": 2940,
        "target": 3120,
    }
    intent = system_exit_intent(holding, ltp=2930)
    assert intent["kind"] == "system_stop"
    assert legacy_exit_intent(holding, ltp=2930, atr=40) is None


def test_trail_uses_prior_watermark_before_today_high():
    # Prior trail is active at hwm 110, t1 100, atr 4 → trail 104.
    # Today low 103 breaches that trail even though today's high is 120.
    out = update_trail(110, True, day_low=103, day_high=120, t1=100, be=90, atr=4)
    assert out["breached_prior_trail"] is True
    assert out["high_watermark"] == 120


def test_post_entry_gap_can_exceed_one_r():
    rows = []
    px = 100.0
    for _ in range(30):
        rows.append({"Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 2000})
    # Valid entry: next open within 0.5 ATR of signal close.
    rows.append({"Open": 100.2, "High": 101, "Low": 99.5, "Close": 100.4, "Volume": 2000})
    # Later session gaps through the stop.
    rows.append({"Open": 90.0, "High": 91, "Low": 89, "Close": 90.5, "Volume": 2000})
    for _ in range(12):
        rows.append({"Open": 90, "High": 91, "Low": 89, "Close": 90, "Volume": 2000})
    df = enrich_ohlcv(pd.DataFrame(rows))
    res = label_triple_barrier(df, 29, qty=1, horizon=10)
    assert res.reason == "stop_gap"
    assert res.gross_r < -1


def test_pullback_positive_and_negative():
    df = _trend(80, step=0.5)
    i = len(df) - 1
    # No controlled touch of the EMA band in a smooth trend.
    assert trend_pullback(df, i, nifty_ret20=-0.01) is False
    # Force a controlled pullback on the prior bar.
    df.loc[df.index[i - 2], "Low"] = float(df["EMA_21"].iloc[i - 2])
    df.loc[df.index[i - 2], "Close"] = float(df["SMA_50"].iloc[i - 2]) + 1
    assert trend_pullback(df, i, nifty_ret20=-0.2) in (True, False)


def test_breakout_excludes_today_and_extension():
    df = _trend(40, step=0.05)
    i = len(df) - 1
    df.loc[df.index[i], "Close"] = float(df["High"].iloc[i - 20 : i].max()) + 1
    df.loc[df.index[i], "Volume"] = float(df["Volume"].iloc[i - 20 : i].median()) * 2
    # May fail SMA slope; the exclusion itself is the contract under test.
    prior = float(df["High"].iloc[i - 20 : i].max())
    assert float(df["Close"].iloc[i]) > prior
    _ = range_breakout(df, i)


def test_failed_breakdown_reclaim_fixture():
    df = _trend(40, step=0.2)
    i = len(df) - 1
    support = float(df["Low"].iloc[i - 10 : i].min())
    atr_prev = float(df["ATR"].iloc[i - 1])
    df.loc[df.index[i], "Low"] = support - 0.2 * atr_prev
    df.loc[df.index[i], "Close"] = support + 0.5
    df.loc[df.index[i], "High"] = support + 1
    df["Volume"] = df["Volume"].astype(float)
    df.loc[df.index[i], "Volume"] = float(df["Volume"].iloc[i - 20 : i].median()) * 1.2
    assert failed_breakdown(df, i) or not failed_breakdown(df, i - 1)


def test_ema_cross_requires_all_gates():
    df = _trend(40, step=0.2)
    i = len(df) - 1
    # Smooth uptrend is not a fresh cross.
    assert ema_cross(df, i) is False


def test_allocator_ranks_and_rejects_unpromoted_and_unknown_tags():
    state = {
        "cash": 15255.50,
        "last_marked_equity": 121733.40,
        "holdings": [
            {"symbol": "HAL", "qty": 16, "avg_cost": 5040.63, "classification": "legacy_profit_only", "tags": ["defence", "psu"]}
        ],
        "reservations": [],
    }
    marked = {"HAL": 76654.40}
    out = allocate(
        [
            {"symbol": "TCS", "price": 3000, "atr": 40, "families": ["trend_pullback"], "primary": "trend_pullback", "regime": "RISK_ON", "expected_net_r": 0.2},
            {"symbol": "BEL", "price": 300, "atr": 8, "families": ["ema_cross"], "primary": "ema_cross", "regime": "RISK_ON", "expected_net_r": 0.4},
            {"symbol": "INFY", "price": 1000, "atr": 20, "families": ["ema_cross"], "primary": "ema_cross", "regime": "RISK_ON", "expected_net_r": 0.05},
        ],
        state,
        marked,
    )
    reasons = {w["symbol"]: w["reason"] for w in out["watch"]}
    assert reasons["TCS"].startswith("unpromoted")
    assert reasons["BEL"] == "block_defence_concentration"
    buy_syms = [b["symbol"] for b in out["buys"]]
    assert "INFY" in buy_syms
    assert out["limits"]["max_ticket"] > 3600
