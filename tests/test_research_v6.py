"""Mechanics for v6 research. Fixtures are not empirical results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from research_v6.earnings import reaction_measure, reaction_session, validate_archive
from research_v6.hmm_runtime import forward_filter, load_model
from research_v6.hmm_state import advance, bootstrap
from research_v6.ou import fit_ou
from research_v6.replay import simulate
from research_v6.residual_momentum import raw_momentum, residual_score, window_bounds
from research_v6.shrinkage import family_payoffs, shrink_mean
from swing_core.exits import legacy_exit_intent
from swing_core.fundamental_screen import evaluate_screen
from swing_v5.costs import fee_components

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]


def _series(seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    market = rng.normal(0.0004, 0.01, 320)
    sector = 0.4 * market + rng.normal(0, 0.008, 320)
    stock = 0.2 * market + 0.3 * sector + rng.normal(0.0002, 0.012, 320)
    return stock, market, sector


def _model() -> dict:
    return {
        "schema": "gaussian_hmm_forward_v1",
        "observation_names": [
            "nifty_log_return", "log_high_low_range", "breadth_above_sma50", "cross_sectional_return_dispersion",
        ],
        "covariance": "diagonal",
        "pi": [0.5, 0.3, 0.2],
        "transition": [[0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.1, 0.1, 0.8]],
        "means": [[0, 0, 0, 0], [0.5, 0.2, 0.1, 0.1], [-0.4, 0.3, -0.2, 0.2]],
        "variances": [[0.2, 0.2, 0.2, 0.2], [0.3, 0.3, 0.3, 0.3], [0.4, 0.4, 0.4, 0.4]],
        "scaling": {"mean": [0, 0, 0, 0], "std": [1, 1, 1, 1]},
        "model_hash": "fixture-hmm",
    }


def test_future_prices_do_not_change_a_stored_residual_score():
    stock, market, sector = _series()
    index = 260
    original = residual_score(stock, market, sector, index, 1.0)
    future = stock.copy()
    future[-1] += 3
    assert residual_score(future, market, sector, index, 1.0) == original
    decision_day = stock.copy()
    decision_day[index] += 1
    assert residual_score(decision_day, market, sector, index, 1.0) == original
    inside = stock.copy()
    inside[index - 10] += 0.05
    assert residual_score(inside, market, sector, index, 1.0) != original
    fit_start, fit_stop, rank_start, rank_stop = window_bounds(index)
    assert rank_stop - rank_start == 60
    assert fit_stop - fit_start == 120
    assert rank_start == fit_stop
    assert raw_momentum(stock, index) != original


def test_missing_sector_is_excluded_and_market_only_is_named_separately():
    stock, market, sector = _series()
    index = 260
    assert residual_score(stock, market, None, index, 1.0) is not None
    broken = sector.copy()
    broken[index - 20] = np.nan
    assert residual_score(stock, market, broken, index, 1.0) is None


def test_hmm_restart_matches_batch_and_duplicates_do_not_advance():
    model = _model()
    rng = np.random.default_rng(1)
    observations = rng.normal(size=(6, 4))
    batch = forward_filter(model, observations)
    calendar = [f"2026-01-0{day}" for day in range(1, 7)]
    state = bootstrap(model)
    for index, day in enumerate(calendar):
        state = advance(state, model, day, observations[index], calendar)
        assert np.allclose(state["posterior"], batch[index])
    repeated = advance(state, model, calendar[-1], observations[-1], calendar)
    assert repeated["duplicate"] is True
    assert repeated["updates"] == state["updates"]
    with pytest.raises(ValueError):
        advance(bootstrap(model), model, calendar[2], observations[2], calendar)


def test_hmm_failures_are_closed():
    model = _model()
    model["variances"][0][0] = 0
    with pytest.raises(ValueError):
        load_model(model)
    broken = _model()
    del broken["pi"]
    with pytest.raises((ValueError, KeyError)):
        load_model(broken)
    with pytest.raises(json.JSONDecodeError):
        json.loads("{")
    with pytest.raises(ValueError):
        forward_filter(_model(), [[1, 2, 3]])


def test_earnings_session_follows_availability():
    sessions = ["2026-10-16", "2026-10-19"]
    assert reaction_session(datetime(2026, 10, 16, 8, 0, tzinfo=IST), sessions) == "2026-10-16"
    assert reaction_session(datetime(2026, 10, 16, 11, 0, tzinfo=IST), sessions) == "2026-10-19"
    assert reaction_session(datetime(2026, 10, 16, 16, 30, tzinfo=IST), sessions) == "2026-10-19"
    assert reaction_session(datetime(2026, 10, 17, 10, 0, tzinfo=IST), sessions) == "2026-10-19"
    assert reaction_measure(0.02, 0.01, 0.8, 2.0, 100.0) == pytest.approx((0.02 - 0.8 * 0.01) / 0.02)
    assert validate_archive([])["status"] == "BLOCKED"


def test_publication_after_the_decision_is_not_usable(fundamental_payload):
    payload = fundamental_payload(published_at="2026-10-24T09:00:00+05:30")
    assert evaluate_screen("EXAMPLE", 100, "2026-10-23T09:25:00+05:30", payload)["status"] == "UNVERIFIED"


def test_ou_rejects_invalid_parameters():
    rng = np.random.default_rng(2)
    assert fit_ou(np.cumsum(rng.normal(size=300)))["valid"] is False
    assert fit_ou(np.cumprod(np.full(220, 1.02)))["valid"] is False
    assert fit_ou(np.ones(50))["reason"] == "insufficient_or_nonfinite"


def test_shrinkage_keeps_win_and_loss_counts_separate():
    assert shrink_mean(0, 99, 30, 1.5) == 1.5
    assert shrink_mean(10, 2.0, 30, 1.0) == pytest.approx((20 + 30) / 40)
    table = family_payoffs([1.0, 2.0, -1.0, -2.0], ["alpha", "alpha", "alpha", "beta"], 30)
    assert table["families"]["beta"]["n_win"] == 0
    assert table["families"]["beta"]["mu_win"] == table["pooled_win"]
    assert table["families"]["beta"]["n_loss"] == 1
    assert table["families"]["alpha"]["mu_loss"] != table["families"]["beta"]["mu_loss"]


def test_target_distance_does_not_change_initial_risk_and_fees_follow_quantity():
    entry, stop, qty = 100.0, 90.0, 4
    assert qty * (entry - stop) == 40
    near = fee_components(entry, 110, qty, entry_day="2026-07-01", exit_day="2026-07-08")
    far = fee_components(entry, 140, qty, entry_day="2026-07-01", exit_day="2026-07-08")
    assert near["dp"] == far["dp"] == fee_components(entry, 140, qty * 3, entry_day="2026-07-01", exit_day="2026-07-08")["dp"]
    assert fee_components(entry, 110, qty * 2, entry_day="2026-07-01", exit_day="2026-07-08")["stt"] > near["stt"]


def test_legacy_loss_does_not_become_a_sell():
    holding = {"classification": "legacy_profit_only", "qty": 10, "avg_cost": 100, "t1_qty_plan": 4, "t2_qty_plan": 3, "runner_qty_plan": 3}
    assert legacy_exit_intent(holding, 90, 2) is None
    assert legacy_exit_intent({**holding, "classification": "system_atr"}, 150, 2) is None


def _wide_policy():
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    risk = dict(policy["risk"])
    risk.update({
        "trade_risk_rupee_cap": 100000, "aggregate_risk_rupee_cap": 100000,
        "trade_risk_fraction": 1, "aggregate_risk_fraction": 1,
        "cash_reserve_fraction": 0, "position_debit_fraction": 1,
        "max_cost_fraction_initial_price_risk": 1, "loss_headroom_trade_divisor": 1,
        "loss_headroom_aggregate_fraction": 1, "cumulative_net_loss_pause": 500,
        "max_new_and_pending_positions": 2,
    })
    policy["risk"] = risk
    return policy


def _book(sessions, opens, highs, lows, closes=None):
    closes = closes or [100.0] * len(sessions)
    return {"AAA": {day: {"open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i]} for i, day in enumerate(sessions)}}


def test_gap_through_target_exits_at_the_open_and_intrabar_stop_is_first():
    from research_v6.replay import _bar_exit, gross_path
    assert _bar_exit({"open": 100.0, "high": 130.0, "low": 90.0, "close": 100.0}, 97.0, 110.0) == (97.0, "stop")
    assert _bar_exit({"open": 120.0, "high": 125.0, "low": 80.0, "close": 90.0}, 97.0, 110.0) == (120.0, "target")
    sessions = [f"2026-07-{day:02d}" for day in range(1, 8)]
    signal = {"symbol": "AAA", "family": "raw_momentum_h1", "signal_date": sessions[0], "limit": 100.0, "atr": 4.0}
    bars = _book(sessions, [100, 101, 120, 100, 100, 100, 100], [101, 102, 125, 101, 101, 101, 101], [99, 100, 80, 99, 99, 99, 99])
    path = gross_path(signal, bars, sessions)
    assert path["status"] == "completed"
    assert path["reason"] == "target"
    assert path["exit"] == 120.0


def test_admitted_orders_reserve_cash_reject_duplicates_and_keep_the_latch():
    policy = _wide_policy()
    policy["risk"]["trade_risk_rupee_cap"] = 200
    policy["risk"]["cumulative_net_loss_pause"] = 50
    policy["risk"]["loss_headroom_trade_divisor"] = 0.01
    policy["risk"]["loss_headroom_aggregate_fraction"] = 100
    sessions = [f"2026-07-{day:02d}" for day in range(1, 12)]
    flat = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    bars = {"AAA": {day: dict(flat) for day in sessions}, "BBB": {day: dict(flat) for day in sessions}}
    bars["AAA"][sessions[1]] = {"open": 100.0, "high": 101.0, "low": 70.0, "close": 80.0}
    bars["BBB"][sessions[1]] = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
    bars["BBB"][sessions[4]] = {"open": 100.0, "high": 140.0, "low": 99.0, "close": 130.0}
    first = {"symbol": "AAA", "family": "raw", "signal_date": sessions[0], "limit": 100.0, "atr": 4.0}
    second = {"symbol": "BBB", "family": "raw", "signal_date": sessions[0], "limit": 100.0, "atr": 4.0}
    duplicate = {"symbol": "AAA", "family": "raw", "signal_date": sessions[0], "limit": 100.0, "atr": 4.0}
    later = {"symbol": "CCC", "family": "raw", "signal_date": sessions[5], "limit": 100.0, "atr": 4.0}
    bars["CCC"] = {day: dict(flat) for day in sessions}
    result = simulate([first, duplicate, second, later], bars, sessions, policy)
    assert len(result["admitted"]) == 2
    assert any(row.get("reason") == "duplicate_symbol" and row["symbol"] == "AAA" for row in result["rejected"])
    assert result["latched"] is True
    assert any(row.get("reason") == "loss_latch" and row["symbol"] == "CCC" for row in result["rejected"])
    assert result["completed"]
    frozen = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    blocked = simulate([{"symbol": "AAA", "family": "raw", "signal_date": sessions[0], "limit": 100.0, "atr": 2.0}], bars, sessions, frozen)
    assert blocked["admitted"] == []
    assert blocked["cash"] == 5748


def test_pending_debit_blocks_the_next_order():
    policy = _wide_policy()
    sessions = ["2026-07-01", "2026-07-02"]
    bar = {"open": 4000.0, "high": 4010.0, "low": 3990.0, "close": 4000.0}
    bars = {"AAA": {day: dict(bar) for day in sessions}, "BBB": {day: dict(bar) for day in sessions}}
    signals = [
        {"symbol": "AAA", "family": "raw", "signal_date": sessions[0], "limit": 4000.0, "atr": 200.0},
        {"symbol": "BBB", "family": "raw", "signal_date": sessions[0], "limit": 4000.0, "atr": 200.0},
    ]
    result = simulate(signals, bars, sessions, policy)
    assert [row["symbol"] for row in result["admitted"]] == ["AAA"]
    assert result["rejected"][0]["reason"] == "risk_or_cost"


def test_reference_quantity_is_costed_without_the_account_filter():
    from research_v6.replay import reference_trade
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    sessions = [f"2026-07-{day:02d}" for day in range(1, 16)]
    signal = {"symbol": "AAA", "family": "raw", "signal_date": sessions[0], "limit": 100.0, "atr": 10.0}
    bars = _book(sessions, [100.0] * len(sessions), [101.0] * len(sessions), [99.0] * len(sessions))
    bars["AAA"][sessions[1]] = {"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.0}
    bars["AAA"][sessions[3]] = {"open": 101.0, "high": 160.0, "low": 100.0, "close": 150.0}
    trade = reference_trade(signal, bars, sessions, policy)
    assert trade["population"] == "reference_risk_budget_5000_not_admitted_fills"
    assert trade["status"] == "completed"
    assert trade["qty"] == int(5000 // (1.5 * 10))
    assert trade["fees"] > 0
    assert trade["account_failed"]


def test_ridge_lambda_changes_the_slope_and_hmm_seeds_differ():
    from research_v6.hmm_fit import _initialize
    from research_v6.residual_momentum import _ridge
    rng = np.random.default_rng(4)
    factor = rng.normal(size=120)
    target = 2.0 * factor + rng.normal(scale=0.01, size=120)
    small = _ridge(target, factor, 0.1)
    large = _ridge(target, factor, 100.0)
    assert abs(small[0][1]) > abs(large[0][1])
    scaled = rng.normal(size=(40, 4))
    first = _initialize(scaled, 0)
    second = _initialize(scaled, 1)
    again = _initialize(scaled, 0)
    assert not np.allclose(first[2], second[2])
    assert np.allclose(first[2], again[2])


def test_price_bars_are_stored_by_symbol():
    panels = {"AAA": {"close": np.array([1.0, 2.0]), "open": np.array([1.0, 2.0]), "high": np.array([1.0, 2.0]), "low": np.array([1.0, 2.0])}}
    dates = ["2026-07-01", "2026-07-02"]
    bars = {
        symbol: {day: {"close": float(panel["close"][index])} for index, day in enumerate(dates) if np.isfinite(panel["close"][index])}
        for symbol, panel in panels.items()
    }
    assert "AAA" in bars and bars["AAA"]["2026-07-02"]["close"] == 2.0


def test_research_package_has_no_production_side_effects():
    text = "\n".join(path.read_text() for path in (ROOT / "research_v6").glob("*.py"))
    for banned in ("boto3", "ntfy", "lambda_function", "send_push", "update_alias"):
        assert banned not in text
    approval = json.loads((ROOT / "config" / "ops" / "promotion_approval.json").read_text())
    assert approval["approved_families"] == []
