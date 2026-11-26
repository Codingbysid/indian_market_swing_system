"""Production Lambda handler for the 5 Mon–Fri IST EventBridge slots."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.aws.engine import run_post_market, run_pre_market, run_signal_engine
from src.aws.schemas import JobType

IST = ZoneInfo("Asia/Kolkata")

# Slot clock → SIGNAL_RUN name
SLOT_MAP = {
    "09:45": "SLOT_1",
    "12:45": "SLOT_2",
    "15:10": "SLOT_3_PRE_CAS",
    "15:30": "SLOT_3",
}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_holiday_guard() -> bool:
    """Thin holiday guard; full calendar lives in swing_core.calendar_nse when available."""
    try:
        from swing_core.calendar_nse import is_trading_day, now_ist

        return not is_trading_day(now_ist().date())
    except Exception:
        # Weekends only if calendar import fails.
        return _now_ist().weekday() >= 5


def _format_email(payload: dict[str, Any]) -> tuple[str, str]:
    title = f"Swing | {payload.get('status', 'OK')} | {payload.get('slot_name', payload.get('timestamp_ist', ''))}"
    body = json.dumps(payload, indent=2, default=str)
    html = f"<html><body><h2>{title}</h2><pre>{body}</pre></body></html>"
    return title, html


def handle(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """Route EventBridge `job_type` (preferred) or legacy `action` payloads."""
    event = event or {}
    dry_run = bool(event.get("dry_run", False))
    suppress = bool(event.get("suppress_alerts", False))

    job: JobType | None = event.get("job_type")
    if job is None:
        action = str(event.get("action", "")).lower()
        if action in {"recommender", "monitor"}:
            job = "SIGNAL_RUN"
        elif action in {"scraper", "pre_market"}:
            job = "PRE_MARKET"
        elif action in {"post_market", "eod"}:
            job = "POST_MARKET"
        else:
            job = "SIGNAL_RUN"

    if _is_holiday_guard() and not event.get("ignore_holiday", False):
        result = {
            "status": "HOLIDAY",
            "timestamp_ist": _now_ist().isoformat(),
            "notes": "NSE cash market closed — slot skipped",
            "job_type": job,
        }
        return {"ok": True, "result": result, "email_sent": False}

    if job == "PRE_MARKET":
        result = run_pre_market()
    elif job == "POST_MARKET":
        result = run_post_market()
    else:
        slot = event.get("slot_name") or SLOT_MAP.get(str(event.get("slot_ist", "09:45")), "SLOT_1")
        result = run_signal_engine(slot)

    email_sent = False
    title, html = _format_email(dict(result))
    if not dry_run and not suppress and result.get("status") == "OK":
        # Delivery uses existing SMTP/ntfy path when credentials exist.
        if os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"):
            try:
                from lambda_function import send_push_and_email

                send_push_and_email(title, json.dumps(result, default=str), title, priority="default")
                email_sent = True
            except Exception as exc:  # pragma: no cover
                result = dict(result)
                result["email_error"] = str(exc)

    return {
        "ok": True,
        "job_type": job,
        "result": result,
        "email_sent": email_sent,
        "email_preview_html": html if dry_run or suppress else None,
    }


def lambda_handler(event, context):  # AWS entry alias
    return handle(event, context)
