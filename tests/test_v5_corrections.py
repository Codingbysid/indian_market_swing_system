"""Regressions for the defects in the first v5 research pass."""

import pandas as pd

from swing_v5.execution import label_trade, reprice
from swing_v5.ledger import replay_ledger
from swing_v5.risk import is_latched, open_stressed_risk
from swing_v5.strategies import improving_value, trend_transition


def _levels(n=30):
    rows = []
    for i in range(n):
        rows.append({"SMA50": 10.0, "SMA200": 12.0, "Close": 100.0, "High": 100.0, "Low": 99.0, "Volume": 100.0, "ATR": 1.0})
    return pd.DataFrame(rows)


def test_cross_uses_previous_slow_average_and_keeps_window_start():
    frame = _levels(230)
    # The only cross is index 211, the first bar of the ten-session window ending at 220.
    frame.loc[210, ["SMA50", "SMA200"]] = [11, 12]
    frame.loc[211, ["SMA50", "SMA200"]] = [13, 12]
    frame.loc[220, "Close"] = 130
    frame.loc[220, "High"] = 130
    frame.loc[:219, "High"] = 101
    frame["Volume"] = 1000
    frame.loc[220, "Volume"] = 5000
    frame["SMA200"] = 12
    frame.loc[211, "SMA200"] = 12
    frame.loc[210, "SMA200"] = 12
    assert trend_transition(frame, 220, rel63=0.1) is True


def test_value_family_blocks_missing_piotroski_and_future_snapshot():
    frame = _levels(60)
    frame["Close"] = 100
    frame["High"] = 90
    frame.loc[59, "Close"] = 100
    frame.loc[59, "High"] = 100
    frame["SMA50"] = 90
    frame["ATR"] = 10
    common = dict(member=True, financial=False, rel63=0.2, ebit=1.0, equity=1.0, snapshot_date="2026-01-01", signal_date="2026-01-02")
    assert improving_value(frame, 59, piotroski=None, **common) is False
    assert improving_value(frame, 59, piotroski=6, snapshot_date="2026-02-01", signal_date="2026-01-02", member=True, financial=False, rel63=0.2, ebit=1, equity=1) is False
    assert improving_value(frame, 59, piotroski=6, **common) is True


def test_reprice_does_not_scale_one_share_r_and_ledger_waits_for_exit():
    one = reprice(100, 110, 1, 10, entry_day="2026-10-16", exit_day="2026-10-20")
    two = reprice(100, 110, 2, 10, entry_day="2026-10-16", exit_day="2026-10-20")
    assert two["fees"] != 2 * one["fees"]
    events = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "date": "2026-10-16",
                "fill_date": "2026-10-16",
                "exit_date": "2026-10-20",
                "long_status": "filled",
                "entry": 100.0,
                "exit": 110.0,
                "stop": 90.0,
                "atr": 4.0,
                "downside_gap95_63_atr": 0.2,
            }
        ]
    )
    # Active caps reject this wide ticket; the ledger must not invent a profit anyway.
    result = replay_ledger(events, cash=5748, reserve=1149.6, debit_cap=2299.2, trade_risk=86.22, aggregate_cap=172.44)
    assert result["fills"] == 0
    assert result["final_cash"] == 5748


def test_latch_survives_rebound_and_open_risk_is_reserved():
    book = {"cash": 7000, "launch_nav": 5748, "external_inflows": 2000, "circuit_latched": True, "sleeve_nav": 7000}
    policy = {"risk": {"cumulative_net_loss_pause": 1500}}
    assert is_latched(book, policy) is True
    used = open_stressed_risk(
        {
            "holdings": [{"classification": "system_atr", "stressed_risk": 80}],
            "reservations": [{"status": "open", "stressed_risk": 50}],
        }
    )
    assert used == 130


def test_circuit_lock_does_not_invent_a_fill_and_unknown_horizon_raises():
    rows = []
    for _ in range(5):
        rows.append({"Date": "2026-10-16", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 10, "ATR": 1, "Tradable": True})
    rows.append({"Date": "2026-10-17", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 10, "ATR": 1, "Tradable": False})
    result = label_trade(pd.DataFrame(rows), 4, horizon="short")
    assert result["status"] == "pending_untradable"
    assert result.get("exit") is None
    try:
        label_trade(pd.DataFrame(rows), 4, horizon="intraday")
        assert False
    except ValueError:
        pass


def test_missing_date_is_invalid_rather_than_a_default():
    frame = pd.DataFrame([{"Open": 100, "High": 101, "Low": 99, "Close": 100, "ATR": 1}, {"Open": 100, "High": 101, "Low": 99, "Close": 100, "ATR": 1}])
    result = label_trade(frame, 0, horizon="short")
    assert result["reason"] == "missing_transaction_date"
