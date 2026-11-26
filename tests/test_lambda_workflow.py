"""Comprehensive test suite for the 5-slot automated swing trading workflow."""

import json
from datetime import datetime, date
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import lambda_function
from scripts.setup_eventbridge import RULES
from swing_core.calendar_nse import (
    IST,
    clock_for,
    holiday_dates,
    is_holiday,
    is_trading_day,
    is_weekend,
    now_ist,
)
from swing_core.stubs import (
    get_market_regime,
    run_post_market,
    run_pre_market,
    run_signal_engine,
)


@pytest.fixture(autouse=True)
def mock_aws_env(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    monkeypatch.setenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    monkeypatch.setenv("NTFY_TOPIC", "test-swing-topic")
    monkeypatch.setenv("GMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "testpass")


def test_calendar_and_holiday_guards():
    """Verify NSE calendar, weekend, and holiday calculations."""
    holidays = holiday_dates()
    assert isinstance(holidays, set)
    assert date(2026, 1, 26) in holidays  # Republic Day

    # Saturday/Sunday checks
    saturday = datetime(2026, 10, 17, 10, 0, tzinfo=IST)
    assert is_weekend(saturday)
    assert not is_trading_day(saturday, holidays=holidays)

    # Holiday check
    republic_day = datetime(2026, 1, 26, 10, 0, tzinfo=IST)
    assert is_holiday(republic_day, holidays=holidays)
    assert not is_trading_day(republic_day, holidays=holidays)

    # Trading weekday check (e.g. Wednesday 2026-10-21)
    trading_day = datetime(2026, 10, 21, 10, 0, tzinfo=IST)
    assert not is_weekend(trading_day)
    assert not is_holiday(trading_day, holidays=holidays)
    assert is_trading_day(trading_day, holidays=holidays)


def test_eventbridge_rules_specification():
    """Verify all 5 EventBridge rules have correct cron syntax and payload structure."""
    expected_rules = {
        "swing-pre-market": ("cron(35 3 ? * MON-FRI *)", "PRE_MARKET"),
        "swing-signal-slot-1": ("cron(15 4 ? * MON-FRI *)", "SIGNAL_RUN"),
        "swing-signal-slot-2": ("cron(15 7 ? * MON-FRI *)", "SIGNAL_RUN"),
        "swing-signal-slot-3": ("cron(0 10 ? * MON-FRI *)", "SIGNAL_RUN"),
        "swing-post-market": ("cron(35 10 ? * MON-FRI *)", "POST_MARKET"),
    }
    assert len(RULES) == 5
    for rule in RULES:
        name = rule["Name"]
        assert name in expected_rules
        expected_cron, expected_action = expected_rules[name]
        assert rule["ScheduleExpression"] == expected_cron
        assert rule["Input"]["action"] == expected_action


def test_pre_market_stub_execution():
    """Verify run_pre_market returns valid health payload and message."""
    state = {
        "cash": 75000.0,
        "holdings": [{"symbol": "RELIANCE", "qty": 10}],
        "status": "PORTFOLIO_CONFIRMED",
    }
    wednesday_morning = datetime(2026, 10, 21, 9, 5, tzinfo=IST)
    result = run_pre_market(tv=None, s3=None, state=state, now=wednesday_morning)

    assert result["action"] == "PRE_MARKET"
    assert result["is_trading_day"] is True
    assert result["status"] in ("HEALTHY", "DATA_INVALID")
    assert result["portfolio"]["cash"] == 75000.0
    assert "RELIANCE" in result["portfolio"]["held_symbols"]
    assert result["notice"].title.startswith("Pre-Market | System Health")
    assert "09:05 IST" in result["message_body"]


def test_signal_engine_stub_execution():
    """Verify run_signal_engine returns exact schema for scan_message & monitor_message."""
    state = {
        "cash": 50000.0,
        "holdings": [{"symbol": "INFY", "qty": 20, "ltp": 1500.0, "be": 1450.0}],
        "status": "PORTFOLIO_CONFIRMED",
    }
    wednesday_midday = datetime(2026, 10, 21, 12, 45, tzinfo=IST)
    result = run_signal_engine(
        slot_name="SLOT_2",
        tv=None,
        s3=None,
        state=state,
        now=wednesday_midday,
    )

    assert result["action"] == "SIGNAL_RUN"
    assert result["slot"] == "SLOT_2"
    assert isinstance(result["BUY"], list)
    assert isinstance(result["SELL"], list)
    assert isinstance(result["HOLD"], list)
    assert result["limits"]["cash"] == 50000.0
    assert result["notice"] is not None
    assert "Swing | No new buy" in result["scan_notice"].title
    assert "Holdings | No sell signal" in result["monitor_notice"].title


def test_post_market_stub_execution():
    """Verify run_post_market returns EOD reconciliation summary."""
    state = {
        "cash": 95000.0,
        "holdings": [],
        "status": "PORTFOLIO_CONFIRMED",
    }
    wednesday_evening = datetime(2026, 10, 21, 16, 5, tzinfo=IST)
    result = run_post_market(tv=None, s3=None, state=state, now=wednesday_evening)

    assert result["action"] == "POST_MARKET"
    assert result["status"] == "COMPLETED"
    assert result["portfolio"]["cash"] == 95000.0
    assert result["notice"].title.startswith("Post-Market | EOD Summary")


def test_lambda_handler_all_slots(monkeypatch):
    """Test full execution of all 5 slots via lambda_handler in dry-run mode."""
    # Force market open context
    monkeypatch.setattr(lambda_function, "is_weekend_ist", lambda now=None: (False, ""))

    slots_to_test = [
        {"action": "PRE_MARKET"},
        {"action": "SIGNAL_RUN", "slot_name": "SLOT_1"},
        {"action": "SIGNAL_RUN", "slot_name": "SLOT_2"},
        {"action": "SIGNAL_RUN", "slot_name": "SLOT_3"},
        {"action": "POST_MARKET"},
    ]

    for event in slots_to_test:
        response = lambda_function.lambda_handler(
            {**event, "dry_run": True, "suppress_alerts": True}, None
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["action"] == event["action"]
        if "slot_name" in event:
            assert body["slot"] == event["slot_name"]


def test_lambda_handler_legacy_action_mapping(monkeypatch):
    """Verify backward compatibility: recommender -> SIGNAL_RUN, monitor -> SIGNAL_RUN, scraper -> PRE_MARKET."""
    monkeypatch.setattr(lambda_function, "is_weekend_ist", lambda now=None: (False, ""))

    # Recommender -> SIGNAL_RUN SLOT_1
    res = lambda_function.lambda_handler(
        {"action": "recommender", "dry_run": True, "suppress_alerts": True}, None
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["action"] == "SIGNAL_RUN"
    assert body["slot"] == "SLOT_1"

    # Monitor -> SIGNAL_RUN SLOT_3
    res = lambda_function.lambda_handler(
        {"action": "monitor", "dry_run": True, "suppress_alerts": True}, None
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["action"] == "SIGNAL_RUN"
    assert body["slot"] == "SLOT_3"

    # Scraper -> PRE_MARKET
    res = lambda_function.lambda_handler(
        {"action": "scraper", "dry_run": True, "suppress_alerts": True}, None
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["action"] == "PRE_MARKET"


def test_lambda_handler_closed_market_skip(monkeypatch):
    """Verify Lambda skips execution when exchange is closed unless --force is specified."""
    monkeypatch.setattr(
        lambda_function,
        "is_weekend_ist",
        lambda now=None: (True, "Market closed: Saturday (IST weekend)."),
    )

    # Without force -> skipped
    res = lambda_function.lambda_handler({"action": "SIGNAL_RUN"}, None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["status"] == "SKIPPED"
    assert "weekend" in body["reason"]

    # With force -> proceeds
    res = lambda_function.lambda_handler(
        {"action": "SIGNAL_RUN", "ignore_weekend": True, "dry_run": True, "suppress_alerts": True},
        None,
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["action"] == "SIGNAL_RUN"


def test_notification_delivery_pipeline(monkeypatch):
    """Verify send_push_and_email invokes ntfy and smtplib cleanly."""
    mock_post = MagicMock()
    mock_smtp = MagicMock()
    monkeypatch.setattr(lambda_function.requests, "post", mock_post)
    monkeypatch.setattr(lambda_function.smtplib, "SMTP_SSL", mock_smtp)

    result = lambda_function.send_push_and_email(
        title="Test Alert",
        message_body="Sample notification body text",
        email_subject="Test Alert",
        priority="default",
    )

    assert result["ntfy"] == "sent"
    assert result["email"] == "sent"
    assert mock_post.call_count == 1
    assert mock_smtp.call_count == 1
