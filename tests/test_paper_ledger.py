"""Comprehensive unit tests for shadow paper trading portfolio ledger and report generator."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from src.aws.calendar_nse import IST
from src.aws.paper_ledger import (
    BUY_COST_BPS,
    MAX_NEW_PER_SECTOR_WEEKLY,
    MAX_OPEN_PER_SECTOR,
    MAX_OPEN_POSITIONS,
    MAX_WEEKLY_ENTRIES,
    SELL_COST_BPS,
    append_paper_daily_nav,
    append_paper_trade,
    check_week_rollover,
    compute_paper_summary,
    execute_paper_buys,
    execute_paper_sells,
    get_iso_week_id,
    init_paper_state,
    load_paper_state,
    ratchet_trailing_stops,
    read_paper_daily_nav,
    read_paper_trades,
    save_paper_state,
    validate_paper_state,
)
from scripts.generate_paper_report import calculate_metrics, render_markdown_report


def test_paper_state_auto_init_and_validation(tmp_path, monkeypatch):
    """Verify clean initialization and validation of paper state."""
    state = init_paper_state(500000.0)
    assert state["initial_capital"] == 500000.0
    assert state["cash"] == 500000.0
    assert state["weekly_entries_count"] == 0
    assert state["weekly_sectors_entered"] == []
    assert state["positions"] == []
    assert state["status"] == "PAPER_ACTIVE"

    validated = validate_paper_state(state)
    assert validated["cash"] == 500000.0

    with pytest.raises(ValueError):
        validate_paper_state({"not_a_valid_state": True})


def test_paper_cost_accounting_buy_and_sell():
    """Verify exact 16.90 bps on BUY and 15.40 bps on SELL (32.30 bps RT)."""
    state = init_paper_state(100000.0)
    cands = [{"symbol": "TESTSYM", "sector": "Technology", "price": 1000.0, "rank": 1}]
    prices = {"TESTSYM": 1000.0}

    # BUY execution
    buys = execute_paper_buys(state, cands, prices, "09:45_IST")
    assert len(buys) == 1
    trade_b = buys[0]
    assert trade_b["action"] == "BUY"
    assert trade_b["cost_bps"] == 16.90
    expected_notional = trade_b["qty"] * 1000.0
    expected_cost = round(expected_notional * (16.90 / 10000.0), 2)
    assert trade_b["cost_inr"] == expected_cost
    assert trade_b["net_cash_delta"] == -(expected_notional + expected_cost)
    assert state["cash"] == round(100000.0 - (expected_notional + expected_cost), 2)

    # SELL execution with profit
    prices["TESTSYM"] = 1100.0
    sells = execute_paper_sells(state, [{"symbol": "TESTSYM", "trigger": "trailing_stop"}], prices, "15:10_IST")
    assert len(sells) == 1
    trade_s = sells[0]
    assert trade_s["action"] == "SELL"
    assert trade_s["cost_bps"] == 15.40
    sell_gross = trade_s["qty"] * 1100.0
    sell_cost = round(sell_gross * (15.40 / 10000.0), 2)
    assert trade_s["cost_inr"] == sell_cost
    assert trade_s["net_cash_delta"] == round(sell_gross - sell_cost, 2)

    # Realized PnL net of both entry and exit costs
    gross_pnl = sell_gross - expected_notional
    expected_realized = round(gross_pnl - (expected_cost + sell_cost), 2)
    assert trade_s["realized_pnl_inr"] == expected_realized


def test_cross_slot_weekly_entry_deduplication():
    """Verify 09:45 fill increments weekly entries so 12:45 and 15:10 do not double-buy."""
    state = init_paper_state(500000.0)
    candidates_slot1 = [
        {"symbol": "SYM_A", "sector": "Auto", "price": 500.0, "rank": 1},
        {"symbol": "SYM_B", "sector": "Banking", "price": 1000.0, "rank": 2},
    ]
    prices = {"SYM_A": 500.0, "SYM_B": 1000.0, "SYM_C": 750.0}

    # Slot 1 fills 2 positions (hitting MAX_WEEKLY_ENTRIES=2)
    buys_slot1 = execute_paper_buys(state, candidates_slot1, prices, "09:45_IST")
    assert len(buys_slot1) == 2
    assert state["weekly_entries_count"] == 2

    # Slot 2 arrives with new candidate SYM_C
    candidates_slot2 = [{"symbol": "SYM_C", "sector": "Energy", "price": 750.0, "rank": 1}]
    buys_slot2 = execute_paper_buys(state, candidates_slot2, prices, "12:45_IST")
    # Must be 0 because weekly limit of 2 is already reached!
    assert len(buys_slot2) == 0

    # Slot 3 arrives
    buys_slot3 = execute_paper_buys(state, candidates_slot2, prices, "15:10_IST")
    assert len(buys_slot3) == 0


def test_sector_cap_enforcement():
    """Verify max 2 open per sector and max 1 new per sector per week."""
    state = init_paper_state(500000.0)
    cands = [
        {"symbol": "BANK_1", "sector": "Banking", "price": 500.0, "rank": 1},
        {"symbol": "BANK_2", "sector": "Banking", "price": 600.0, "rank": 2},  # Same sector
    ]
    prices = {"BANK_1": 500.0, "BANK_2": 600.0}

    buys = execute_paper_buys(state, cands, prices, "09:45_IST")
    # Only 1 buy allowed in Banking this week (max 1 new per sector per week)
    assert len(buys) == 1
    assert buys[0]["symbol"] == "BANK_1"
    assert state["weekly_sectors_entered"] == ["Banking"]


def test_trailing_stop_ratchet():
    """Verify peak_price and stop_price ratchet upward as price rises."""
    state = init_paper_state(100000.0)
    cands = [{"symbol": "RATCHET_SYM", "sector": "Pharma", "price": 100.0, "rank": 1}]
    prices = {"RATCHET_SYM": 100.0}
    execute_paper_buys(state, cands, prices, "09:45_IST")

    pos = state["positions"][0]
    assert pos["peak_price"] == 100.0
    assert pos["stop_price"] == 88.0

    # Price rises to 120
    prices["RATCHET_SYM"] = 120.0
    ratchet_trailing_stops(state["positions"], prices)
    assert pos["peak_price"] == 120.0
    assert pos["stop_price"] == round(120.0 * 0.88, 2)  # 105.60

    # Price dips to 110 (peak and stop must NOT decrease)
    prices["RATCHET_SYM"] = 110.0
    ratchet_trailing_stops(state["positions"], prices)
    assert pos["peak_price"] == 120.0
    assert pos["stop_price"] == 105.60


def test_week_rollover_resets_counters():
    """Verify week rollover resets weekly entries and weekly sectors."""
    state = init_paper_state(500000.0)
    state["weekly_entries_count"] = 2
    state["weekly_sectors_entered"] = ["Banking", "IT"]
    state["week_id"] = "2026-W48"

    # Simulated next week (2026-W49)
    next_week_dt = datetime(2026, 12, 1, 10, 0, tzinfo=IST)
    rolled = check_week_rollover(state, next_week_dt)
    assert rolled is True
    assert state["week_id"] == "2026-W49"
    assert state["weekly_entries_count"] == 0
    assert state["weekly_sectors_entered"] == []


def test_2week_report_generator_scorecard():
    """Verify 2-week performance report calculation and markdown rendering."""
    state = {"initial_capital": 500000.0, "cash": 450000.0}
    sample_navs = [
        {"date": "2026-11-30", "total_equity": 500000.0, "invested_value": 0.0, "nifty_close": 24000.0, "stale_quote_count": 0},
        {"date": "2026-12-05", "total_equity": 503500.0, "invested_value": 100000.0, "nifty_close": 24100.0, "stale_quote_count": 0},
        {"date": "2026-12-11", "total_equity": 505000.0, "invested_value": 90000.0, "nifty_close": 24200.0, "stale_quote_count": 0},
    ]
    sample_trades = [
        {
            "date": "2026-11-30", "timestamp_ist": "2026-11-30T09:45:00+05:30", "slot_name": "09:45_IST",
            "action": "BUY", "symbol": "TEST1", "sector": "Banking", "qty": 100, "fill_price": 500.0,
            "gross_notional": 50000.0, "cost_inr": 84.50, "trigger_reason": "TOP_V3_RANK",
        },
        {
            "date": "2026-12-05", "timestamp_ist": "2026-12-05T15:10:00+05:30", "slot_name": "15:10_IST",
            "action": "SELL", "symbol": "TEST1", "sector": "Banking", "qty": 100, "fill_price": 550.0,
            "gross_notional": 55000.0, "cost_inr": 84.70, "trigger_reason": "RANK_BUFFER_TOP40",
            "realized_pnl_inr": 4830.80, "realized_pnl_bps": 966.2,
        },
    ]

    metrics = calculate_metrics(state, sample_trades, sample_navs, "2026-11-30", "2026-12-11")
    assert metrics["starting_equity"] == 500000.0
    assert metrics["ending_equity"] == 505000.0
    assert metrics["net_return_pct"] == 1.00
    assert metrics["completed_round_trips"] == 1
    assert metrics["win_rate_pct"] == 100.0
    assert metrics["sector_violations"] == 0

    md = render_markdown_report(metrics)
    assert "# Shadow Paper-Trading 2-Week Performance Evaluation Report" in md
    assert "₹505,000.00" in md
    assert "GO (APPROVED FOR LIVE BROKER PLUMBING)" in md
