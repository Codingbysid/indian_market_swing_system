"""Production Lambda handler for the 5 Mon–Fri IST EventBridge slots.

100% self-contained in src.aws.* with ZERO dependencies on legacy modules.
Routes EventBridge payloads to V3 Pre-Market, Intraday Signals (09:45, 12:45, 15:10),
and Post-Market EOD ingestion. Delivers rich HTML & ntfy notifications in SHADOW mode.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.aws.calendar_nse import IST, is_trading_day, now_ist
from src.aws.engine import run_post_market, run_pre_market, run_signal_engine
from src.aws.notifications import (
    Message,
    post_market_message,
    pre_market_message,
    scan_message,
    send_push_and_email,
)
from src.aws.schemas import JobType

logger = logging.getLogger(__name__)

# Slot clock → SIGNAL_RUN name
SLOT_MAP = {
    "09:45": "09:45_IST",
    "12:45": "12:45_IST",
    "15:10": "15:10_IST",
    "15:30": "15:30_IST",
}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_holiday_guard() -> bool:
    """NSE holiday & weekend guard using self-contained calendar."""
    try:
        return not is_trading_day(now_ist().date())
    except Exception as exc:
        logger.warning("Calendar check exception: %s; falling back to weekend check.", exc)
        return _now_ist().weekday() >= 5


def _build_message(job: JobType, result: dict[str, Any]) -> Message:
    if job == "PRE_MARKET":
        return pre_market_message(result)
    elif job == "POST_MARKET":
        return post_market_message(result)
    else:
        return scan_message(result)


def handle(event: dict[str, Any] | None, context: Any = None) -> dict[str, Any]:
    """Route EventBridge `job_type` (preferred) or legacy `action` payloads."""
    event = event or {}
    dry_run = bool(event.get("dry_run", False))
    suppress = bool(event.get("suppress_alerts", False))
    # Propagate dry-run to artifact loaders (S3-first skipped; local container files used)
    if dry_run:
        os.environ["ENGINE_DRY_RUN"] = "1"
    else:
        os.environ.pop("ENGINE_DRY_RUN", None)

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
        slot = event.get("slot_name") or SLOT_MAP.get(str(event.get("slot_ist", "09:45")), "09:45_IST")
        result = run_signal_engine(slot)

    msg = _build_message(job, dict(result))
    email_sent = False
    delivery_status: dict[str, str] = {}

    if not dry_run and not suppress and result.get("status") == "OK":
        try:
            delivery_status = send_push_and_email(
                title=msg.title,
                message_body=msg.body,
                email_subject=msg.title,
                priority=msg.priority,
                tags=msg.tags,
            )
            email_sent = delivery_status.get("email") in ("sent_ses", "sent_smtp")
        except Exception as exc:
            logger.error("Notification delivery failed: %s", exc)
            delivery_status = {"error": str(exc)}

    return {
        "ok": True,
        "job_type": job,
        "result": result,
        "email_sent": email_sent,
        "delivery": delivery_status if delivery_status else None,
        "email_preview_html": msg.html if (dry_run or suppress) else None,
    }


def lambda_handler(event, context):  # AWS Lambda entrypoint
    return handle(event, context)
