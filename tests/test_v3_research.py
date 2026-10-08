"""Detector fixtures, purged folds, and the control checks that gate a release candidate."""

import pandas as pd

from backtest.splits import fold_windows, session_map, split_fold
from swing_core.allocation import allocate, repair_quote
from swing_core.calendar_nse import is_fresh_bar
from swing_core.exits import system_exit_intent
from swing_core.migration import migration_dry_run
from swing_core.scoring import output_policy, score_dump
from swing_core.state import StaleWrite, VersionedStore, apply_mark_snapshot
from swing_core.strategies import (
    ema_cross,
    failed_breakdown,
    range_breakout,
    relative_strength_signal,
    trend_pullback,
)


def _frame(n=40):
    rows = []
    for i in range(n):
        px = 100 + i * 0.2
        rows.append({"Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1000})
    return pd.DataFrame(rows)


def test_pullback_positive_and_near_miss():
    df = _frame(40)
    i = 30
    for k in range(i + 1):
        df.loc[k, "Close"] = 100 + k
        df.loc[k, "SMA_50"] = 50 + 0.2 * k
        df.loc[k, "EMA_9"] = 80 + k
        df.loc[k, "EMA_21"] = 70 + k
        df.loc[k, "ATR"] = 2.0
        df.loc[k, "High"] = float(df.loc[k, "Close"]) + 0.4
        df.loc[k, "Low"] = float(df.loc[k, "EMA_21"])
    df.loc[i, "Close"] = float(df.loc[i - 1, "High"]) + 0.2
    df.loc[i, "EMA_9"] = float(df.loc[i, "Close"]) - 0.1
    df.loc[i, "EMA_21"] = float(df.loc[i, "Close"]) - 1.0
    assert trend_pullback(df, i, nifty_ret20=-0.01) is True
    assert trend_pullback(df, i, nifty_ret20=0.50) is False


def test_other_family_fixtures():
    df = _frame(40)
    i = len(df) - 1
    df.loc[i - 1, "EMA_9"] = 10
    df.loc[i - 1, "EMA_21"] = 11
    df.loc[i, "EMA_9"] = 12
    df.loc[i, "EMA_21"] = 11
    df.loc[i, "RSI"] = 50
    df.loc[i, "Close"] = 100
    df.loc[i, "Lower_Band"] = 90
    df.loc[i, "Upper_Band"] = 120
    assert ema_cross(df, i) is True
    df.loc[i, "Upper_Band"] = 101
    assert ema_cross(df, i) is False

    df = _frame(40)
    i = 30
    for k in range(i + 1):
        df.loc[k, "Close"] = 100 + k
        df.loc[k, "SMA_50"] = 40 + 0.1 * k
        df.loc[k, "EMA_21"] = 90
        df.loc[k, "ATR"] = 2
        df.loc[k, "High"] = 100
        df.loc[k, "Volume"] = 100
    df.loc[i, "Close"] = 130
    df.loc[i, "High"] = 131
    df.loc[i, "Volume"] = 200
    df.loc[i, "EMA_21"] = 128
    df.loc[i, "ATR"] = 2
    assert range_breakout(df, i) is True
    df.loc[i, "Volume"] = 100
    assert range_breakout(df, i) is False

    df = _frame(40)
    i = 30
    for k in range(i + 1):
        df.loc[k, "Close"] = 120
        df.loc[k, "Low"] = 110
        df.loc[k, "High"] = 121
        df.loc[k, "SMA_50"] = 100
        df.loc[k, "ATR"] = 4
        df.loc[k, "Volume"] = 100
    df.loc[i - 10, "SMA_50"] = 90
    df.loc[i, "Low"] = 108.5
    df.loc[i, "Close"] = 112
    df.loc[i - 1, "Close"] = 111
    df.loc[i, "High"] = 113
    df.loc[i, "Volume"] = 150
    assert failed_breakdown(df, i) is True
    df.loc[i, "Low"] = 100
    assert failed_breakdown(df, i) is False


def test_relative_strength_needs_a_cross_into_the_quintile():
    df = _frame(80)
    i = 70
    for k in range(i + 1):
        df.loc[k, "Close"] = 100 + k
        df.loc[k, "SMA_50"] = 50 + 0.1 * k
        df.loc[k, "EMA_21"] = float(df.loc[k, "Close"]) - 1
        df.loc[k, "ATR"] = 2
    assert relative_strength_signal(df, i, prev_rank=0.70, curr_rank=0.85, residual20=0.02) is True
    assert relative_strength_signal(df, i, prev_rank=0.86, curr_rank=0.90, residual20=0.02) is False
    assert relative_strength_signal(df, i, prev_rank=None, curr_rank=0.90, residual20=0.02) is False


def test_pending_risk_and_displayed_cap():
    quote = repair_quote(100, 2, allocation_cap=3652, envelope=8136, risk_left=152.56, agg_left=305.11)
    assert quote["allocation_cap"] == 3652
    state = {
        "cash": 15255.50,
        "last_marked_equity": 121733.40,
        "as_of": "2026-10-07",
        "holdings": [],
        "reservations": [
            {"status": "open", "notional": 2000, "stressed_risk": 280, "expires_on": "2026-10-08"}
        ],
    }
    out = allocate(
        [
            {
                "symbol": "TCS",
                "price": 1000,
                "atr": 40,
                "primary": "ema_cross",
                "families": ["ema_cross"],
                "regime": "RISK_ON",
                "expected_net_r": 0.2,
            }
        ],
        state,
        {},
    )
    assert out["buys"] == []
    state["reservations"][0]["expires_on"] = "2026-10-01"
    out = allocate(
        [
            {
                "symbol": "TCS",
                "price": 100,
                "atr": 1,
                "primary": "ema_cross",
                "families": ["ema_cross"],
                "regime": "RISK_ON",
                "expected_net_r": 0.2,
            }
        ],
        state,
        {},
    )
    assert out["buys"]


def test_stale_marks_and_schema_block_buys():
    state = {"cash": 10000, "last_marked_equity": 50000, "marks_status": "stale", "holdings": [], "reservations": []}
    out = allocate(
        [{"symbol": "TCS", "price": 100, "atr": 2, "primary": "ema_cross", "regime": "RISK_ON", "expected_net_r": 0.2}],
        state,
        {},
    )
    assert out["status"] == "STALE_EQUITY"
    state["marks_status"] = "ok"
    out = allocate(
        [{
            "symbol": "TCS", "price": 100, "atr": 2, "primary": "ema_cross", "regime": "RISK_ON",
            "expected_net_r": 0.2, "model_schema_ok": False,
        }],
        state,
        {},
    )
    assert out["watch"][0]["reason"] == "schema_mismatch"


def test_versioned_writes_and_incomplete_marks():
    store = VersionedStore()
    version = store.put("portfolio", {"cash": 1}, expected_version=None)
    store.put("marks", {"equity": 10}, expected_version=None)
    try:
        store.put("portfolio", {"cash": 2}, expected_version=0)
        assert False
    except StaleWrite:
        pass
    store.put("portfolio", {"cash": 2}, expected_version=version)
    assert store.docs["marks"]["body"]["equity"] == 10
    kept = apply_mark_snapshot({"cash": 100, "qty": {"HAL": 16}, "last_marked_equity": 120000}, {"HAL": 1}, ["HAL", "INFY"])
    assert kept["marks_status"] == "stale"
    assert kept["last_marked_equity"] == 120000


def test_migration_dry_run_does_not_restore_timex():
    state = {
        "cash": 15255.50,
        "last_marked_equity": 121733.40,
        "holdings": [
            {"symbol": "HAL", "qty": 16, "avg_cost": 5040.63, "classification": "legacy_profit_only", "t1_qty_plan": 5, "t2_qty_plan": 5, "runner_qty_plan": 6},
            {"symbol": "ZENTEC", "qty": 8, "avg_cost": 1711.25, "classification": "legacy_profit_only", "t1_qty_plan": 3, "t2_qty_plan": 3, "runner_qty_plan": 2},
            {"symbol": "CDSL", "qty": 8, "avg_cost": 1363.75, "classification": "legacy_profit_only", "t1_qty_plan": 3, "t2_qty_plan": 3, "runner_qty_plan": 2},
            {"symbol": "INFY", "qty": 5, "avg_cost": 1030.0, "classification": "legacy_profit_only", "t1_qty_plan": 2, "t2_qty_plan": 1, "runner_qty_plan": 2},
            {"symbol": "BPCL", "qty": 6, "avg_cost": 318.65, "classification": "legacy_profit_only", "t1_qty_plan": 2, "t2_qty_plan": 2, "runner_qty_plan": 2},
        ],
    }
    report = migration_dry_run(state)
    assert report["wrote"] is False
    assert report["destination"] == "null"
    assert report["limits"]["max_ticket"] > 3600
    assert "block_defence_concentration" in report["defence_probe_reasons"]


def test_system_time_exit_and_dry_run_policy():
    holding = {
        "symbol": "TCS", "qty": 2, "avg_cost": 1000, "classification": "system_atr",
        "entry_price": 1000, "entry_atr": 20, "stop": 970, "target": 1060, "max_sessions": 10,
        "protective_order_status": "not_placed",
    }
    intent = system_exit_intent(holding, ltp=1010, sessions_held=10)
    assert intent["kind"] == "system_vertical"
    assert intent["order_status"] == "session_expiry_instruction"
    assert output_policy({"dry_run": True}) == {"emit_alerts": False, "write_state": False, "destination": "null"}


def test_stale_candle_is_not_fresh_just_because_it_is_before_today():
    assert is_fresh_bar("2026-09-01", "2026-10-07") is False
    assert is_fresh_bar("2026-10-06", "2026-10-07") is True


def test_purged_fold_drops_overlapping_exit():
    sessions = list(pd.bdate_range("2018-01-01", "2024-12-31"))
    smap = session_map(sessions)
    events = pd.DataFrame(
        [
            {"date": "2020-12-15", "exit_date": "2021-01-20", "label": 1, "status": "filled", "symbol": "AAA"},
            {"date": "2020-06-01", "exit_date": "2020-06-15", "label": 0, "status": "filled", "symbol": "BBB"},
        ]
    )
    window = fold_windows("2018-01-02", "2024-12-31")[0]
    parts = split_fold(events, window, smap, embargo_sessions=10)
    assert "AAA" not in set(parts["train"]["symbol"])
    assert "BBB" in set(parts["train"]["symbol"])


def test_missing_base_score_does_not_default():
    assert score_dump({"trees": [{"leaf": 0.1, "nodeid": 0}], "feature_names": ["rsi14"], "base_score": ""}, {"rsi14": 1}) is None
