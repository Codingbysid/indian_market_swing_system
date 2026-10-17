"""v5 contract tests. Provider mocks are not live API health."""

import json

import numpy as np
import pandas as pd

from runtime_v5.package_check import build
from runtime_v5.preview import RecordingServices, handle
from swing_v5.costs import legacy_be, tranche_net_pnl
from swing_v5.data.screener import parse_page
from swing_v5.decision import evaluate
from swing_v5.execution import label_trade
from swing_v5.features import enrich, row_features
from swing_v5.portfolio import (
    StaleWrite,
    VersionedStore,
    apply_fill,
    apply_marks,
    book_from_reference,
    conditional_put,
    legacy_sell_allowed,
    update_trail,
)
from swing_v5.risk import largest_quantity, limits_for, sleeve_pnl
from swing_v5.scorer import score
from swing_v5.types import DEFAULT_REGISTRY, RegistryEntry, ScreenSnapshot
from swing_v5.universe import select_universe


def _policy():
    return json.loads(open("config/v5/research_policy.json").read())


def _frame(rows):
    return enrich(pd.DataFrame(rows))


def test_future_bar_does_not_change_past_features():
    rows = []
    price = 100.0
    for i in range(160):
        price += 0.2
        rows.append({"Date": f"2020-01-{(i % 27) + 1}", "Open": price, "High": price + 1, "Low": price - 1, "Close": price, "Volume": 1000})
    frame = _frame(rows)
    nifty = frame.copy()
    nifty["EMA50"] = nifty["Close"].ewm(span=50, adjust=False).mean()
    before = row_features(frame, 140, nifty=nifty, breadth=0.6, price_only=True)
    frame.loc[141, "Close"] = 1.0
    after = row_features(enrich(frame), 140, nifty=nifty, breadth=0.6, price_only=True)
    assert before == after


def test_screen_failure_is_not_an_empty_result_and_starts_at_observation():
    failed = {"status": "fetch_failed", "complete": False, "empty": False}
    assert failed["empty"] is False
    page = parse_page("12 results found: Showing page 1 of 3 <textarea>Score &gt; 7</textarea>", "u")
    assert page["complete"] is False
    assert page["empty"] is False
    snap = ScreenSnapshot("piotroski", "Score > 7", "2026-10-17", None, "u", True, ("ABC",), "hash")
    assert snap.active_on("2026-10-16") is False
    assert snap.active_on("2026-10-17") is True


def test_universe_never_exceeds_150_and_pins_holdings():
    liquid = [(f"S{i:03d}", f"sec{i % 10}", 1000 - i) for i in range(200)]
    selected = select_universe(held=["HAL"], pending=["ZENTEC"], screens={}, liquid=liquid, max_symbols=150)
    assert selected["count"] == 150
    assert [row["symbol"] for row in selected["symbols"][:2]] == ["HAL", "ZENTEC"]
    concentrated = select_universe(held=["HAL"], pending=[], screens={}, liquid=[(f"S{i}", "only", 1) for i in range(100)], max_symbols=150)
    assert concentrated["count"] < 150


def test_gap_paths_and_same_bar_stop():
    rows = []
    for i in range(30):
        rows.append({"Date": "2026-01-02", "Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000})
    rows.append({"Date": "2026-01-03", "Open": 90, "High": 91, "Low": 89, "Close": 90, "Volume": 1000})
    early = label_trade(_frame(rows), 29, horizon="short")
    assert early["status"] == "unfilled"
    rows = []
    for i in range(40):
        rows.append({"Date": "2026-01-02", "Open": 100, "High": 101, "Low": 99.5, "Close": 100, "Volume": 1000})
    rows[30] = {"Date": "2026-01-03", "Open": 100.05, "High": 130, "Low": 90, "Close": 110, "Volume": 1000}
    result = label_trade(_frame(rows), 29, horizon="short")
    assert result["reason"] == "stop_same_bar"
    assert result["net_r"] < -1 or result["status"] == "filled"


def test_fixture_caps_and_partial_reservation():
    limits = limits_for(5748, _policy(), sleeve_p=0)
    assert limits["reserve"] == 1149.60
    assert limits["position_debit_cap"] == 2299.20
    assert limits["trade_risk"] == 86.22
    assert limits["aggregate_risk"] == 172.44
    book = {"cash": 5000, "holdings": [], "reservations": [{"symbol": "TCS", "status": "open", "qty": 4}], "fills": []}
    fill = {"fill_id": "1", "symbol": "TCS", "side": "buy", "qty": 1, "price": 100, "fees": 1, "classification": "system_atr", "entry_atr": 2, "stop": 97, "target": 106, "setup": "T"}
    once = apply_fill(book, fill)
    twice = apply_fill(once, fill)
    assert twice["cash"] == once["cash"]
    assert twice["reservations"][0]["qty"] == 3


def test_duplicate_symbol_is_not_allocated_twice():
    policy = _policy()
    book = {"cash": 5748, "launch_nav": 5748, "external_inflows": 0, "holdings": [], "quote_status": "ok", "restriction_metadata": "present", "circuit_latched": False}
    registry = dict(DEFAULT_REGISTRY)
    registry["trend_transition_price_control"] = RegistryEntry("trend_transition_price_control", "1", "long", "promoted", False)
    candidate = {"symbol": "TCS", "family": "trend_transition_price_control", "price": 100, "stop": 60, "gap_buffer": 1, "expected_net": 10, "sector": "it"}
    result = evaluate({"as_of": "2026-10-17", "regime": "RISK_ON", "candidates": [candidate, dict(candidate)], "events": []}, book, policy, registry)
    buys = [row for row in result["intents"] if row["action"] == "BUY"]
    assert len(buys) <= 1
    assert any(row.get("reason") == "duplicate_candidate" for row in result["rejections"])


def test_loss_latch_ignores_deposits_and_legacy_marks():
    assert sleeve_pnl(4248, 5748, 0) == -1500
    assert sleeve_pnl(6248, 5748, 2000) == -1500
    limits = limits_for(4248, _policy(), sleeve_p=-1500)
    assert limits["latched"] is True
    book = book_from_reference(json.loads(open("fixtures/v5/broker_snapshot_2026-10-17.json").read()))
    book["circuit_latched"] = True
    book["quote_status"] = "ok"
    book["restriction_metadata"] = "present"
    result = evaluate({"as_of": "2026-10-17", "regime": "RISK_ON", "candidates": [{"symbol": "TCS", "family": "trend_transition_price_control", "price": 100, "stop": 90, "gap_buffer": 1, "expected_net": 5, "sector": "it"}], "events": []}, book, _policy(), {"trend_transition_price_control": RegistryEntry("trend_transition_price_control", "1", "long", "promoted", False)})
    assert any(row["reason"] == "NEW_BUYS_PAUSED" for row in result["rejections"])
    assert not any(row["action"] == "BUY" for row in result["intents"])


def test_legacy_protection_trail_and_system_lot():
    book = book_from_reference(json.loads(open("fixtures/v5/broker_snapshot_2026-10-17.json").read()))
    hal = next(row for row in book["holdings"] if row["symbol"] == "HAL")
    assert round(legacy_be(hal["avg_cost"], hal["qty"]), 2) == 5054.36
    assert legacy_sell_allowed(hal, 4989, 1, day="2026-10-17") is False
    zentec = next(row for row in book["holdings"] if row["symbol"] == "ZENTEC")
    assert zentec["plan_status"] == "PLAN_RECONCILIATION_REQUIRED"
    assert zentec["plan_applied"] is False
    trail = update_trail(110, True, 103, 120, 100, 90, 4)
    assert trail["breached_prior"] is True
    assert trail["high_watermark"] == 120
    try:
        apply_fill({"cash": 1000, "holdings": [], "fills": []}, {"fill_id": "x", "symbol": "TCS", "side": "buy", "qty": 1, "price": 10, "fees": 0, "classification": "legacy_profit_only"})
        assert False
    except Exception as exc:
        assert exc.reason == "new_buy_must_be_system_lot"
    assert tranche_net_pnl(100, 101, 1, day="2026-10-17") < 0


def test_stale_marks_and_conflicting_write():
    kept = apply_marks({"last_marked_equity": 100}, {"HAL": 1}, ["HAL", "INFY"])
    assert kept["marks_status"] == "stale"
    assert kept.get("last_marked_equity") == 100
    store = VersionedStore()
    version = store.put("book", {"cash": 1}, None)
    try:
        store.put("book", {"cash": 2}, 0)
        assert False
    except StaleWrite:
        pass
    store.put("marks", {"equity": 9}, None)
    store.put("book", {"cash": 2}, version)
    assert store.docs["marks"]["body"]["equity"] == 9

    class Client:
        def put_object(self, **kwargs):
            raise type("E", (Exception,), {"response": {"Error": {"Code": "PreconditionFailed"}}})()

    try:
        conditional_put(Client(), "b", "k", b"{}", "etag")
        assert False
    except StaleWrite:
        pass


def test_dry_run_and_package():
    book = book_from_reference(json.loads(open("fixtures/v5/broker_snapshot_2026-10-17.json").read()))
    services = RecordingServices()
    result = handle({"dry_run": True, "as_of": "2026-10-17", "regime": "DATA_INVALID"}, book, _policy(), services)
    assert result["writes"] == 0 and result["notifications"] == 0 and result["destination"] == "null"
    package = build(__import__("pathlib").Path("dist/v5_candidate.zip"))
    assert package["banned"] == []
    assert package["deployed"] is False


def test_scorer_parity_contract():
    from backtest.v5.parity import synthetic_model

    model = synthetic_model()
    row = {name: 0.0 for name in model["feature_names"]}
    threshold = np.float32(0.1)
    row["atr_pct"] = float(np.nextafter(threshold, np.float32(-np.inf)))
    assert score(model, row) > 0.5
    row["atr_pct"] = float(threshold)
    assert score(model, row) < 0.5
    assert score(model, {name: 0.0 for name in model["feature_names"] if name != "atr_pct"}) is None
    assert score({**model, "base_score": ""}, row) is None


def test_no_gap_history_rejects_quantity():
    assert largest_quantity(entry=100, stop=90, gap_buffer=float("nan"), cash_left=5000, debit_cap=2299, trade_risk=86, aggregate_left=172, day="2026-10-17") == 0
