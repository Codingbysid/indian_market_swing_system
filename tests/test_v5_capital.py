"""Cost-filter identity, resume audit, and one-scrip DP."""

from swing_v5.costs import fee_components, tariff_as_of
from swing_v5.feasibility import analytical_bound
from swing_v5.risk import is_latched, request_resume


def test_cost_filter_implies_minimum_stressed_loss():
    row = analytical_bound(200, 80, 4, 1)
    assert row["cost_filter"] is True
    assert row["R_at_least_4F"] is True
    assert row["stressed"] + 1e-6 >= row["minimum_stressed_if_filter_binds"]
    tight = analytical_bound(100, 10, 2, 1)
    assert tight["cost_filter"] is False
    assert tight["dp"] == 15.34


def test_dp_is_once_per_sale_and_slippage_is_not_in_the_price():
    one = fee_components(100, 110, 1, entry_day="2026-10-16", exit_day="2026-10-20")
    ten = fee_components(100, 110, 10, entry_day="2026-10-16", exit_day="2026-10-20")
    assert one["dp"] == ten["dp"] == 15.34
    assert ten["slippage"] == 10 * one["slippage"]
    assert ten["total"] < 10 * one["total"]


def test_old_dates_are_labeled_approximations():
    tariff = tariff_as_of("2024-06-01")
    assert tariff.approximation.startswith("present_cost_approximation")
    try:
        tariff_as_of("")
        assert False
    except ValueError:
        pass


def test_persistent_resume_flag_does_not_clear_a_breach():
    policy = {"risk": {"cumulative_net_loss_pause": 1500}}
    book = {
        "circuit_latched": True,
        "explicit_resume": True,
        "sleeve_nav": 4000,
        "launch_nav": 5748,
        "external_inflows": 0,
        "cash": 4000,
    }
    assert is_latched(book, policy) is True
    refused = request_resume(book, policy, actor="user", reason="deposit", now="2026-10-18")
    assert refused["accepted"] is False
    assert book["circuit_latched"] is True
    book["sleeve_nav"] = 8000
    book["external_inflows"] = 0
    accepted = request_resume(book, policy, actor="user", reason="reviewed", now="2026-10-19")
    assert accepted["accepted"] is True
    assert book["circuit_latched"] is False
    assert "explicit_resume" not in book
