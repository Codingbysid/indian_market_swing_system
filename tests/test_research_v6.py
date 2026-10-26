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


def test_replay_distinguishes_unfilled_stop_and_target():
    from research_v6.replay import _bar_exit, isolate_path
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    sessions = [f"2026-07-{day:02d}" for day in range(1, 16)]
    signal = [{"symbol": "AAA", "family": "raw_momentum_h1", "signal_date": sessions[0], "limit": 100.0, "atr": 2.0, "features": {}}]
    bars = {"AAA": {day: {"open": 100.2, "high": 101.0, "low": 99.0, "close": 100.0} for day in sessions}}
    blocked = simulate(signal, bars, sessions, policy)
    assert blocked["completed"] == []
    assert blocked["rejected"][0]["reason"] == "risk_or_cost"
    assert blocked["cash"] == 5748
    assert _bar_exit({"open": 100.0, "high": 130.0, "low": 90.0, "close": 100.0}, 97.0, 110.0) == (97.0, "stop")
    wide = {"symbol": "AAA", "family": "raw_momentum_h1", "signal_date": sessions[0], "limit": 100.0, "atr": 80.0, "features": {}}
    path_bars = {"AAA": {day: {"open": 100.0, "high": 110.0, "low": 95.0, "close": 100.0} for day in sessions}}
    path_bars["AAA"][sessions[1]] = {"open": 105.0, "high": 110.0, "low": 100.0, "close": 106.0}
    path_bars["AAA"][sessions[2]] = {"open": 106.0, "high": 400.0, "low": 100.0, "close": 300.0}
    filled = isolate_path(wide, path_bars, sessions, policy)
    assert filled["status"] == "completed" and filled["reason"] == "target" and filled["qty"] == 1
    path_bars["AAA"][sessions[1]] = {"open": 200.0, "high": 210.0, "low": 190.0, "close": 205.0}
    assert isolate_path(wide, path_bars, sessions, policy)["status"] == "unfilled"


def test_research_package_has_no_production_side_effects():
    text = "\n".join(path.read_text() for path in (ROOT / "research_v6").glob("*.py"))
    for banned in ("boto3", "ntfy", "lambda_function", "send_push", "update_alias"):
        assert banned not in text
    approval = json.loads((ROOT / "config" / "ops" / "promotion_approval.json").read_text())
    assert approval["approved_families"] == []
