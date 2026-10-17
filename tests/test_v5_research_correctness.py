"""Regression tests for the v5 research defects. These fail on the 2509408 behavior."""

import json

import pandas as pd

from swing_v5.costs import round_trip
from swing_v5.decision import evaluate
from swing_v5.execution import label_trade, reprice
from swing_v5.features import enrich
from swing_v5.ledger import replay_ledger
from swing_v5.risk import is_latched, largest_quantity, open_stressed_risk
from swing_v5.strategies import golden_cross_series, improving_value, trend_transition
from swing_v5.types import RegistryEntry


def _policy():
    return json.loads(open("config/v5/research_policy.json").read())


def _prices(n=240, close=100.0):
    rows = []
    for i in range(n):
        price = close + i * 0.1
        rows.append(
            {
                "Date": pd.Timestamp("2024-01-02") + pd.offsets.BDay(i),
                "Open": price,
                "High": price + 1,
                "Low": price - 1,
                "Close": price,
                "Volume": 1000 + i,
            }
        )
    return enrich(pd.DataFrame(rows))


def test_golden_cross_uses_previous_slow_average_and_keeps_window_start():
    frame = _prices()
    # A real cross only at the first bar of the ten-session window.
    i = 230
    frame.loc[: i - 10, "SMA50"] = 90
    frame.loc[: i - 10, "SMA200"] = 100
    frame.loc[i - 9 :, "SMA50"] = 110
    frame.loc[i - 9 :, "SMA200"] = 105
    # Previous session of the window start: 90 <= 100 and 110 > 105.
    assert bool(golden_cross_series(frame).iloc[i - 9])
    # The defective comparison used previous SMA50 against the current SMA200
    # and dropped the first shifted row of a sliced window.
    window = frame.iloc[i - 9 : i + 1]
    defective = (window["SMA50"] > window["SMA200"]) & (window["SMA50"].shift(1) <= window["SMA200"])
    assert not bool(defective.fillna(False).any())
    frame["High"] = frame["Close"] + 1
    frame.loc[i, "Close"] = float(frame["High"].iloc[i - 20 : i].max()) + 5
    frame.loc[i, "High"] = frame.loc[i, "Close"] + 1
    frame.loc[i, "Volume"] = 10_000
    assert trend_transition(frame, i, rel63=0.1)


def test_missing_piotroski_blocks_improving_value():
    frame = _prices(80, 50)
    frame.loc[70, "Close"] = float(frame["High"].iloc[50:70].max()) + 1
    common = dict(member=True, financial=False, rel63=0.2, ebit=1.0, equity=1.0, snapshot_date="2024-01-01", signal_date="2024-04-01")
    assert improving_value(frame, 70, piotroski=None, **common) is False
    assert improving_value(frame, 70, piotroski=float("nan"), **common) is False
    assert improving_value(frame, 70, piotroski=5, **common) is False
    assert improving_value(frame, 70, piotroski=6, ebit=-1, equity=1, snapshot_date="2024-01-01", signal_date="2024-04-01", member=True, financial=False, rel63=0.2) is False
    assert improving_value(frame, 70, piotroski=6, financial=True, **{k: v for k, v in common.items() if k != "financial"}) is False
    assert improving_value(frame, 70, piotroski=6, snapshot_date="2024-05-01", signal_date="2024-04-01", member=True, financial=False, rel63=0.2, ebit=1, equity=1) is False


def test_reprice_does_not_scale_one_share_r_by_quantity():
    one = reprice(100, 110, 1, 5, entry_day="2026-10-16", exit_day="2026-10-17")
    ten = reprice(100, 110, 10, 5, entry_day="2026-10-16", exit_day="2026-10-17")
    scaled = one["net_r"] * 10 * 5
    assert ten["net_pnl"] != scaled
    assert ten["fees"] > one["fees"]
    assert round_trip(100, 110, 10, day="2026-10-16") != 10 * round_trip(100, 110, 1, day="2026-10-16")


def test_missing_date_is_invalid_and_unknown_horizon_raises():
    frame = _prices(40)
    frame = frame.drop(columns=["Date"])
    result = label_trade(enrich(frame) if "ATR" not in frame else frame, 30, horizon="short")
    assert result["status"] == "invalid"
    assert result.get("reason") == "missing_transaction_date"
    assert "2026-10-01" not in json.dumps(result)
    try:
        label_trade(_prices(40), 30, horizon="intraday")
        assert False
    except ValueError as exc:
        assert "unknown horizon" in str(exc)


def test_structural_fields_and_circuit_do_not_invent_a_fill():
    frame = _prices(40)
    frame.loc[31, "Open"] = frame.loc[30, "Close"] + 0.05
    result = label_trade(frame, 30, horizon="short")
    if result["status"] == "filled":
        assert result["stop"] < result["entry"]
        assert result["fill_date"]
        assert result["signal_date"]
        assert result["qty"] == 1
    frame["Tradable"] = True
    frame.loc[31, "Tradable"] = False
    locked = label_trade(frame, 30, horizon="short")
    assert locked["status"] == "pending_untradable"
    assert locked.get("exit") is None


def test_nan_and_negative_inputs_do_not_pass_risk():
    assert largest_quantity(entry=100, stop=float("nan"), gap_buffer=1, cash_left=5000, debit_cap=2299, trade_risk=86, aggregate_left=172, day="2026-10-16") == 0
    assert largest_quantity(entry=100, stop=90, gap_buffer=1, cash_left=-1, debit_cap=2299, trade_risk=86, aggregate_left=172, day="2026-10-16") == 0
    assert largest_quantity(entry=100, stop=90, gap_buffer=1, cash_left=5000, debit_cap=2299, trade_risk=86, aggregate_left=-5, day="2026-10-16") == 0
    book = {"holdings": [{"classification": "system_atr", "stressed_risk": float("nan")}], "reservations": []}
    assert open_stressed_risk(book) != open_stressed_risk(book)


def test_latch_survives_rebound_and_deposit():
    policy = _policy()
    book = {"cash": 5748, "launch_nav": 5748, "external_inflows": 0, "sleeve_nav": 4000, "circuit_latched": False}
    assert is_latched(book, policy) is True
    book["circuit_latched"] = True
    book["sleeve_nav"] = 7000
    book["external_inflows"] = 3000
    assert is_latched(book, policy) is True
    book["explicit_resume"] = True
    assert is_latched(book, policy) is False


def test_open_risk_reduces_the_next_admission():
    policy = _policy()
    registry = {"trend_transition_price_control": RegistryEntry("trend_transition_price_control", "1", "long", "promoted", False)}
    book = {
        "cash": 5748,
        "launch_nav": 5748,
        "external_inflows": 0,
        "holdings": [{"symbol": "INFY", "classification": "system_atr", "qty": 1, "stressed_risk": 172.44, "mark": 100}],
        "reservations": [],
        "quote_status": "ok",
        "restriction_metadata": "present",
        "circuit_latched": False,
    }
    candidate = {"symbol": "TCS", "family": "trend_transition_price_control", "price": 100, "stop": 90, "gap_buffer": 1, "expected_net": 5, "sector": "it"}
    result = evaluate({"as_of": "2026-10-16", "regime": "RISK_ON", "candidates": [candidate], "events": []}, book, policy, registry)
    assert not any(row["action"] == "BUY" for row in result["intents"])


def test_ledger_debits_entry_and_does_not_scale_one_share_r():
    events = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "date": "2024-01-02",
                "long_status": "filled",
                "entry": 100.0,
                "exit": 110.0,
                "stop": 95.0,
                "atr": 2.0,
                "downside_gap95_63_atr": 0.2,
                "fill_date": "2024-01-03",
                "exit_date": "2024-01-10",
                "long_net_r": 5.0,
            }
        ]
    )
    result = replay_ledger(events)
    assert result["status"] == "chronological_ledger"
    if result["fills"]:
        admitted = result["admitted"][0]
        scaled = 5.0 * admitted["qty"] * (100 - 95)
        assert admitted["net_pnl"] != scaled
        assert result["equity"][0]["cash"] < 5748
    else:
        assert result["rejections"].get("zero_quantity", 0) >= 1


def test_saturday_is_not_a_session():
    import tempfile
    from pathlib import Path

    from swing_v5.data.nse import fetch_udiff

    with tempfile.TemporaryDirectory() as folder:
        result = fetch_udiff("2026-10-17", Path(folder))
    assert result["parsed"] is False
    assert result["reason"] == "not_an_exchange_session"
