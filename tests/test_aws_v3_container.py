"""Dedicated verification tests for self-contained src.aws.* V3 production container."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from src.aws.calendar_nse import IST, is_holiday, is_trading_day, is_weekend, now_ist
from src.aws.engine import (
    evaluate_regime,
    get_model_dir,
    get_universe,
    run_post_market,
    run_pre_market,
    run_signal_engine,
    score_universe_candidates,
)
from src.aws.lambda_handler import handle, lambda_handler
from src.aws.notifications import (
    buy_card,
    email_html,
    post_market_message,
    pre_market_message,
    push_parts,
    rupees,
    scan_message,
    sell_card,
)
from src.aws.state import (
    PortfolioInvalid,
    apply_fill,
    load_portfolio_state,
    validate_state,
)


def test_aws_calendar_guards():
    """Verify NSE calendar and holiday rules inside src.aws.calendar_nse."""
    now = now_ist()
    assert now.tzinfo is not None

    # Republic Day 2026 is an official NSE holiday
    republic_day = datetime(2026, 1, 26, 10, 0, tzinfo=IST)
    assert is_holiday(republic_day)
    assert not is_trading_day(republic_day)

    # Saturday
    saturday = datetime(2026, 10, 17, 10, 0, tzinfo=IST)
    assert is_weekend(saturday)
    assert not is_trading_day(saturday)

    # Normal Wednesday
    wednesday = datetime(2026, 10, 21, 10, 0, tzinfo=IST)
    assert not is_weekend(wednesday)
    assert not is_holiday(wednesday)
    assert is_trading_day(wednesday)


def test_aws_notifications_formatting():
    """Verify buy card, sell card, and rich scan message formatting."""
    buy = {
        "symbol": "FEDERALBNK",
        "qty": 10,
        "price": 350.50,
        "stop": 308.44,
        "rank": 1,
        "sector": "Financial Services",
        "debit": 3505.00,
    }
    b_card = buy_card(buy)
    assert "BUY · FEDERALBNK · 10 shares" in b_card
    assert "₹350.50" in b_card
    assert "₹3,505.00" in b_card
    assert "#1" in b_card

    sell = {
        "symbol": "INFY",
        "qty": 5,
        "ltp": 1520.0,
        "pnl_pct": 12.5,
        "trigger": "rank_buffer_exit",
    }
    s_card = sell_card(sell)
    assert "SELL · INFY · 5 shares" in s_card
    assert "Rank buffer exit" in s_card
    assert "₹1,520.00 (+12.5%)" in s_card

    result = {
        "slot_name": "09:45_IST",
        "status": "OK",
        "timestamp_ist": "2026-11-27T09:45:00+05:30",
        "regime_state": "BULL",
        "BUY": [buy],
        "SELL": [sell],
        "HOLD": [],
        "limits": {"cash": 50000.0},
    }
    msg = scan_message(result)
    assert "V3 Signals | 1 BUY, 1 SELL · 09:45_IST" in msg.title
    assert msg.priority == "high"
    assert "SHADOW / ALERT-ONLY MODE" in msg.body
    assert "₹50,000.00" in msg.body

    html = msg.html
    assert "<!doctype html>" in html
    assert "FEDERALBNK" in html


def test_aws_state_validation_and_accounting():
    """Verify S3 state schema validation and buy/sell cash accounting."""
    valid = {"cash": 100000.0, "holdings": []}
    clean = validate_state(valid)
    assert clean["status"] == "PORTFOLIO_CONFIRMED"

    with pytest.raises(PortfolioInvalid):
        validate_state({"cash": -50.0})

    with pytest.raises(PortfolioInvalid):
        validate_state({"cash": 1000.0, "holdings": [{"symbol": "TCS"}]})  # missing qty/cost

    # Apply BUY fill
    fill_buy = {
        "side": "BUY",
        "symbol": "RELIANCE",
        "qty": 10,
        "price": 2500.0,
        "fees": 20.0,
        "sector": "Energy",
    }
    state_after_buy = apply_fill(clean, fill_buy)
    assert state_after_buy["cash"] == 100000.0 - (25000.0 + 20.0)
    assert len(state_after_buy["holdings"]) == 1
    assert state_after_buy["holdings"][0]["symbol"] == "RELIANCE"
    assert state_after_buy["holdings"][0]["high_watermark"] == 2500.0

    # Apply SELL fill
    fill_sell = {
        "side": "SELL",
        "symbol": "RELIANCE",
        "qty": 10,
        "price": 2700.0,
        "fees": 20.0,
        "trigger": "rank_buffer_exit",
    }
    state_after_sell = apply_fill(state_after_buy, fill_sell)
    assert len(state_after_sell["holdings"]) == 0
    assert len(state_after_sell["closed_trades"]) == 1
    assert state_after_sell["closed_trades"][0]["pnl"] == round(10 * (2700.0 - 2500.0) - 20.0, 2)


def test_aws_engine_v3_inference():
    """Verify V3 models, pre-market, signal evaluation, and post-market execution."""
    pre = run_pre_market()
    assert pre["status"] == "OK"
    assert pre["universe_count"] == 150
    assert pre["regime_state"] in ("BULL", "SIDEWAYS", "BEAR_HIGH_VOL")

    sig = run_signal_engine("09:45_IST")
    assert sig["status"] == "OK"
    assert sig["slot_name"] == "09:45_IST"
    assert isinstance(sig["signals"], list)
    assert isinstance(sig["BUY"], list)
    assert isinstance(sig["SELL"], list)
    assert isinstance(sig["HOLD"], list)

    # Option A+ HardCap constraint assertions
    # Max 2 new buys per week
    assert len(sig["BUY"]) <= 2

    # Sector constraint: no two buys in the exact same sector
    buy_sectors = [b["sector"] for b in sig["BUY"]]
    assert len(buy_sectors) == len(set(buy_sectors))

    post = run_post_market()
    assert post["status"] == "OK"
    assert "v2/state/latest.json" in post["s3_artifact_uri"]


def test_aws_lambda_handler_all_slots():
    """Verify all 5 scheduled slots execute cleanly in self-contained AWS Lambda handler."""
    slots = [
        {"job_type": "PRE_MARKET", "slot_name": "09:05_IST", "dry_run": True, "ignore_holiday": True},
        {"job_type": "SIGNAL_RUN", "slot_name": "09:45_IST", "dry_run": True, "ignore_holiday": True},
        {"job_type": "SIGNAL_RUN", "slot_name": "12:45_IST", "dry_run": True, "ignore_holiday": True},
        {"job_type": "SIGNAL_RUN", "slot_name": "15:10_IST", "dry_run": True, "ignore_holiday": True},
        {"job_type": "POST_MARKET", "slot_name": "16:05_IST", "dry_run": True, "ignore_holiday": True},
    ]

    for event in slots:
        response = lambda_handler(event, None)
        assert response["ok"] is True
        assert response["result"]["status"] == "OK"
        assert response["email_preview_html"] is not None
        assert "<!doctype html>" in response["email_preview_html"]
