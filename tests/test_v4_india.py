from datetime import datetime, timezone
from copy import deepcopy
from io import BytesIO
import importlib, json, math
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from swing_core.india import build_panel, FEATURE_NAMES, clean_bars, regime_series
from swing_core.risk_v4 import (
    size_trade,
    kelly_evidence,
    exposure_block,
    pair_correlation,
)
from swing_core.engine_v4 import (
    evaluate_snapshot,
    add_reservations,
    active_reservations,
)
from swing_core.scoring import score_model
from swing_core.sentiment import parse_feed, tone, fetch_overlay
from swing_core.state import validate_state, apply_fill, PortfolioInvalid, StaleWrite
from swing_core.s3_state import write_state
from swing_core.costs import entry_charges, round_trip_charges

ROOT = Path(__file__).resolve().parents[1]


def bars(seed=1, n=390, base=100):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(end="2026-10-13", periods=n)
    c = base * np.exp(np.cumsum(rng.normal(0.0002, 0.009, n)))
    return pd.DataFrame(
        {"Open": c, "High": c * 1.008, "Low": c * 0.992, "Close": c, "Volume": 1e7},
        index=index,
    )


def test_features_and_regime_are_invariant_to_future_bars():
    frames = {s: bars(i) for i, s in enumerate(["A", "B", "C", "D", "E", "F"])}
    meta = pd.DataFrame({"Symbol": list(frames), "Sector": ["it"] * 6})
    nifty = bars(9, base=22000)
    full, market = build_panel(frames, nifty, meta)
    prefix = {s: d.iloc[:350] for s, d in frames.items()}
    cut, cut_market = build_panel(prefix, nifty.iloc[:350], meta)
    names = [n for n in FEATURE_NAMES if n != "cost_R"]
    for s in frames:
        np.testing.assert_allclose(
            full[s].iloc[349][names].astype(float),
            cut[s].iloc[-1][names].astype(float),
            equal_nan=True,
            rtol=1e-12,
        )
    assert market.regime.iloc[349] == cut_market.regime.iloc[-1]


def test_regime_requires_breadth_coverage_and_trend():
    n = bars(5)
    n.Close = np.linspace(100, 140, len(n))
    idx = n.index
    healthy = regime_series(n, pd.Series(0.7, index=idx), pd.Series(1.0, index=idx))
    assert healthy.regime.iloc[-1] == "RISK_ON"
    assert (
        regime_series(
            n, pd.Series(0.3, index=idx), pd.Series(1.0, index=idx)
        ).regime.iloc[-1]
        == "RISK_OFF"
    )
    assert (
        regime_series(
            n, pd.Series(0.7, index=idx), pd.Series(0.8, index=idx)
        ).regime.iloc[-1]
        == "DATA_INVALID"
    )


@pytest.mark.parametrize(
    "price,atr", [(float("nan"), 2), (100, float("inf")), (-1, 2), (100, 0)]
)
def test_nonfinite_risk_never_sizes(price, atr):
    assert size_trade(price, atr, 15255.5, 121733.4)["qty"] == 0


def test_stressed_size_includes_both_sides_costs_and_reserve():
    trade = size_trade(500, 10, 15255.5, 121733.4)
    assert (
        trade["qty"] > 0
        and trade["debit"] <= 3652.01
        and trade["stress_risk"] <= 152.56
    )
    assert trade["stress_risk"] > trade["qty"] * 20
    assert size_trade(500, 10, 15255.5, 121733.4, risk_fraction=0)["qty"] == 0


def test_negative_empirical_edge_makes_kelly_zero():
    r = [1.8] * 70 + [-1.2] * 130
    e = kelly_evidence(r, weeks=np.repeat(np.arange(40), 5))
    assert e["full_kelly"] == 0 and e["quarter_kelly"] == 0


def test_insufficient_support_never_enables_kelly():
    e = kelly_evidence([2] * 15 + [-1] * 5)
    assert e["full_kelly"] > 0 and e["quarter_kelly"] == 0


def test_unknown_class_and_invalid_system_expiry_block_state():
    base = {"cash": 1000, "holdings": [{"symbol": "A", "qty": 1, "avg_cost": 100}]}
    with pytest.raises(PortfolioInvalid):
        validate_state(base)
    base["holdings"][0].update(
        classification="system_atr", entry_price=100, entry_atr=2, stop=97, target=106
    )
    with pytest.raises(PortfolioInvalid):
        validate_state(base)


@pytest.mark.parametrize(
    "fees,price", [(float("nan"), 100), (-1, 100), (0, float("inf"))]
)
def test_fill_invalid_financial_values_are_rejected(fees, price):
    with pytest.raises(PortfolioInvalid):
        apply_fill(
            {"cash": 1000, "holdings": []},
            {
                "fill_id": "f",
                "symbol": "A",
                "side": "buy",
                "qty": 1,
                "price": price,
                "fees": fees,
            },
        )


def test_confirmed_fill_is_immutable_idempotent_and_keeps_atr_class():
    state = {"cash": 1000, "holdings": []}
    original = deepcopy(state)
    fill = {
        "fill_id": "broker-1",
        "symbol": "A",
        "side": "buy",
        "qty": 2,
        "price": 100,
        "fees": 1,
        "entry_atr": 2,
        "stop": 97,
        "target": 106,
        "entry_date": "2026-10-14",
        "max_sessions": 10,
    }
    once = apply_fill(state, fill)
    twice = apply_fill(once, fill)
    assert state == original and once == twice and once["cash"] == 799
    with pytest.raises(PortfolioInvalid):
        apply_fill(once, {**fill, "price": 101})
    loss = apply_fill(
        once,
        {
            "fill_id": "broker-2",
            "symbol": "A",
            "side": "sell",
            "qty": 2,
            "price": 96,
            "fees": 2,
            "stage": "stop",
        },
    )
    assert (
        loss["closed_trades"][0]["classification"] == "system_atr"
        and loss["closed_trades"][0]["pnl"] < 0
    )


def test_json_logistic_calibration_and_missing_features():
    model = {
        "type": "logistic",
        "feature_names": ["x"],
        "coefficients": [2.0],
        "intercept": -0.5,
        "mean": [1.0],
        "scale": [2.0],
        "calibrator": {"type": "platt_logit", "a": 0.8, "b": 0.1},
    }
    expected = 1 / (1 + math.exp(-(0.8 * 1.5 + 0.1)))
    assert abs(score_model(model, {"x": 3}) - expected) < 1e-12
    assert (
        score_model(model, {}) is None
        and score_model(model, {"x": float("nan")}) is None
    )


class Conflict(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class S3Conflict:
    def put_object(self, **kw):
        assert kw["IfMatch"] == '"old-etag"'
        raise Conflict()


def test_s3_fill_race_cancels_stale_write():
    with pytest.raises(StaleWrite):
        write_state(
            S3Conflict(),
            "bucket",
            "portfolio.json",
            {"cash": 1000, "holdings": []},
            '"old-etag"',
        )


@pytest.fixture
def prepared_snapshot(monkeypatch):
    from swing_core import engine_v4 as engine

    syms = ["HAL", "ZENTEC", "CDSL", "INFY", "BPCL"] + [f"S{i:03}" for i in range(145)]
    bases = {"HAL": 5000, "ZENTEC": 1700, "CDSL": 1300, "INFY": 1030, "BPCL": 318}
    frames = {s: bars(i, base=bases.get(s, 100)) for i, s in enumerate(syms)}
    nifty = bars(201, base=22000)
    meta = pd.DataFrame(
        {
            "Symbol": syms,
            "Name": syms,
            "Sector": ["Legacy"] * 5 + ["Consumer"] * 145,
            "Tags": ["defence|psu", "defence", "", "", "psu"] + [""] * 145,
        }
    )
    state = json.loads((ROOT / "data/portfolio_state.json").read_text())
    # Testing orchestration with a controlled qualifying market snapshot; detectors tested separately above.
    p = {s: d.copy() for s, d in frames.items()}
    for s, d in p.items():
        for n in FEATURE_NAMES:
            d[n] = 1.0
        d["ATR"] = 4.0
        d["Close"] = bases.get(s, 100)
        d["family"] = None
        d["sector"] = "Consumer"
        d["downside_gap95_60"] = 0.5
    p["S140"]["family"] = "trend_pullback"
    market = pd.DataFrame(
        {
            "regime": "RISK_ON",
            "coverage": 1.0,
            "nifty_ema50": 21000.0,
            "breadth50": 0.7,
            "volatility_ratio": 1.0,
        },
        index=nifty.index,
    )
    monkeypatch.setattr(engine, "build_panel", lambda *a, **k: (p, market))
    monkeypatch.setattr(engine, "pair_correlation", lambda *a, **k: 0.1)
    model = {
        "type": "logistic",
        "feature_names": FEATURE_NAMES,
        "schema_id": "india_v4_20",
        "promoted": True,
        "live_data_parity_verified": True,
        "promotion_evidence": {
            "trend_pullback": {
                "outer_folds": 3,
                "unique_oos_portfolio_fills": 200,
                "net_wins": 110,
                "net_losses": 90,
                "names": 25,
                "mean_net_R_lower_95": 0.05,
            }
        },
        "allowed_live_families": ["trend_pullback"],
        "calibration_cutoff": "2026-09-01",
        "horizon_sessions": 10,
        "stop_atr": 1.5,
        "target_atr": 3.0,
        "coefficients": [0.0] * 20,
        "intercept": 3.0,
        "mean": [0.0] * 20,
        "scale": [1.0] * 20,
        "kelly": {"trend_pullback": {"status": "supported", "quarter_kelly": 0.05}},
        "payoff": {"trend_pullback": {"win_R": 1.8, "loss_R": 1.2}},
    }
    now = datetime(2026, 10, 14, 3, 55, tzinfo=timezone.utc)
    return state, frames, nifty, meta, model, now, p, market


def evaluate_fixture(f, **kw):
    state, frames, nifty, meta, model, now, _, _ = f
    return evaluate_snapshot(state, frames, nifty, meta, model, as_of=now, **kw)


def test_real_allocator_path_promoted_buy_and_book_cash_unchanged(prepared_snapshot):
    state = prepared_snapshot[0]
    old = deepcopy(state)
    result = evaluate_fixture(prepared_snapshot)
    assert len(result["BUY"]) == 1 and state == old, result
    b = result["BUY"][0]
    assert b["qty"] > 0 and b["stress_risk"] <= result["limits"]["risk_per_trade"]
    reserved = add_reservations(state, result["BUY"])
    assert reserved["cash"] == state["cash"]
    with pytest.raises(ValueError):
        add_reservations(reserved, result["BUY"])


def test_unpromoted_or_missing_model_cannot_buy(prepared_snapshot):
    f = list(prepared_snapshot)
    f[4] = None
    assert evaluate_fixture(f)["BUY"] == []
    f[4] = deepcopy(prepared_snapshot[4])
    f[4]["promoted"] = False
    assert evaluate_fixture(f)["BUY"] == []


def test_regime_missing_quote_and_sentiment_veto(prepared_snapshot):
    assert evaluate_fixture(prepared_snapshot, live_quotes={})["BUY"] == []
    assert (
        evaluate_fixture(
            prepared_snapshot,
            sentiment={"symbols": {"S140": {"review_required": True}}},
        )["BUY"]
        == []
    )
    prepared_snapshot[-1]["regime"] = "RISK_OFF"
    assert evaluate_fixture(prepared_snapshot)["BUY"] == []


def test_stale_book_and_pending_reservations_use_slots(prepared_snapshot):
    prepared_snapshot[0]["broker_snapshot"]["date"] = "2026-09-01"
    assert evaluate_fixture(prepared_snapshot)["BUY"] == []
    prepared_snapshot[0]["broker_snapshot"]["date"] = "2026-10-07"
    prepared_snapshot[0]["reservations"] = [
        {
            "reservation_id": str(i),
            "symbol": s,
            "qty": 1,
            "debit": 100,
            "stress_risk": 30,
            "expires_at": "2026-10-14T09:45:00+05:30",
            "status": "open",
        }
        for i, s in enumerate(["S001", "S002"])
    ]
    assert evaluate_fixture(prepared_snapshot)["BUY"] == []


def test_news_future_and_ambiguous_ticker_are_ignored():
    feed = "<rss><channel><item><title>HAL wins contract</title><link>https://example.com</link><pubDate>Wed, 14 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"
    now = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
    assert (
        parse_feed(
            feed,
            [{"Symbol": "HAL", "Name": "Hindustan Aeronautics Ltd."}],
            now,
            source="nse",
        )
        == []
    )
    assert tone("No fraud or default on debt")[0] == 0
    assert tone("Company wins contract")[0] == 1


def test_feed_failure_reports_unknown_instead_of_positive():
    import requests

    class Failing:
        def get(self, *a, **k):
            raise requests.Timeout()

    result = fetch_overlay(
        [{"Symbol": "HAL", "Name": "Hindustan Aeronautics Ltd."}],
        ["HAL"],
        include_news=False,
        session=Failing(),
    )
    assert (
        result["health"][0]["status"] == "UNAVAILABLE"
        and result["symbols"]["HAL"]["headline_tone"] is None
    )


def test_monitor_full_legacy_and_system_paths(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    bot = importlib.import_module("lambda_function")
    state = json.loads((ROOT / "data/portfolio_state.json").read_text())
    d = bars(3, base=4700)
    from swing_core.indicators import calculate_atr, calculate_rsi

    atr = lambda x: calculate_atr(x)
    rsi = lambda x: calculate_rsi(x.Close)
    legacy = bot.compute_holding_levels(state["holdings"][0], d, {}, rsi, atr)
    assert "trail_active" in legacy and legacy["qty"] == 16
    h = {
        "symbol": "A",
        "qty": 2,
        "avg_cost": 100,
        "classification": "system_atr",
        "entry_price": 100,
        "entry_atr": 2,
        "stop": 97,
        "target": 106,
        "entry_date": "2026-10-01",
        "max_sessions": 10,
    }
    low = bars(4, base=60)
    out = bot.compute_holding_levels(h, low, {}, rsi, atr)
    assert (
        out["classification"] == "system_atr"
        and out["actionable_intents"][0]["kind"] == "system_stop"
    )


def test_partial_fill_keeps_cash_reserved_and_includes_entry_fees_in_pnl():
    state = {
        "cash": 1000,
        "holdings": [],
        "reservations": [
            {
                "symbol": "A",
                "qty": 4,
                "debit": 404,
                "stress_risk": 50,
                "status": "open",
                "predicted_probability": 0.65,
                "model_id": "m",
            }
        ],
    }
    first = apply_fill(
        state,
        {
            "fill_id": "p1",
            "symbol": "A",
            "side": "buy",
            "qty": 2,
            "price": 100,
            "fees": 2,
            "entry_atr": 2,
            "stop": 97,
            "target": 106,
            "entry_date": "2026-10-14",
        },
    )
    assert (
        first["reservations"][0]["qty"] == 2
        and first["reservations"][0]["status"] == "open"
    )
    final = apply_fill(
        first,
        {
            "fill_id": "p2",
            "symbol": "A",
            "side": "sell",
            "qty": 2,
            "price": 101,
            "fees": 1,
            "stage": "target",
        },
    )
    assert (
        final["closed_trades"][0]["pnl"] == -1
        and final["closed_trades"][0]["predicted_probability"] == 0.65
    )


def test_model_decay_uses_confirmed_system_outcomes_only():
    from swing_core.monitoring import model_health

    rows = [
        {
            "classification": "system_atr",
            "model_id": "m",
            "predicted_probability": 0.7,
            "net_r": -1,
        }
    ] * 30
    assert model_health(rows, model_id="m")["pause"] is True
    assert (
        model_health([{**r, "classification": "legacy_profit_only"} for r in rows])["n"]
        == 0
    )


def test_transport_deadline_blocks_continuous_messages(monkeypatch):
    from swing_core.market_data import DeadlineSocket

    class Socket:
        def recv(self):
            return "heartbeat"

    sock = DeadlineSocket(Socket(), 10)
    monkeypatch.setattr("swing_core.market_data.time.monotonic", lambda: 11)
    with pytest.raises(TimeoutError):
        sock.recv()


def test_legacy_trail_cannot_loosen_when_atr_rises():
    from swing_core.exits import update_trail

    out = update_trail(
        110,
        True,
        day_low=103,
        day_high=112,
        t1=100,
        be=90,
        atr=20,
        saved_prior_trail=104,
    )
    assert (
        out["prior_trail"] == 104
        and out["trail"] >= 104
        and out["breached_prior_trail"]
    )


def test_one_session_stale_nifty_is_blocked(prepared_snapshot):
    f = list(prepared_snapshot)
    f[2] = f[2].iloc[:-1]
    result = evaluate_fixture(f)
    assert result["BUY"] == [] and result["reason"] == "nifty_stale"


def test_journal_deduplicates_actionable_intent_and_rejects_watch():
    from swing_core.signal_journal import record_once

    class Store:
        def __init__(self):
            self.keys = set()

        def put_object(self, **kw):
            assert kw["IfNoneMatch"] == "*"
            if kw["Key"] in self.keys:
                raise Conflict()
            self.keys.add(kw["Key"])

    s = Store()
    intent = {"side": "SELL", "signal_id": "HAL:t1:5"}
    assert record_once(s, "b", intent, observed_at="2026-10-14") is True
    assert record_once(s, "b", intent, observed_at="2026-10-15") is False
    with pytest.raises(ValueError):
        record_once(s, "b", {"side": "WATCH", "signal_id": "fake"}, observed_at="now")


def test_promoted_flag_without_oos_portfolio_proof_cannot_buy(prepared_snapshot):
    prepared_snapshot[4].pop("promotion_evidence")
    assert evaluate_fixture(prepared_snapshot)["BUY"] == []


def test_new_buy_cannot_be_classified_as_legacy_to_avoid_stop():
    with pytest.raises(PortfolioInvalid):
        apply_fill(
            {"cash": 1000, "holdings": []},
            {
                "fill_id": "bad",
                "symbol": "A",
                "side": "buy",
                "qty": 1,
                "price": 100,
                "classification": "legacy_profit_only",
            },
        )
