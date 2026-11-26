"""AWS Lambda entrypoint for the automated Indian equities swing trading pipeline.

5-slot Mon-Fri IST schedule:
  09:05 IST: PRE_MARKET  - system health check, Nifty pre-open context, load state
  09:45 IST: SIGNAL_RUN  - Slot 1 universe evaluation & Buy/Sell signals
  12:45 IST: SIGNAL_RUN  - Slot 2 universe evaluation & Buy/Sell signals
  15:30 IST: SIGNAL_RUN  - Slot 3 universe evaluation & Buy/Sell signals
  16:05 IST: POST_MARKET - ingest EOD data, update Nifty regime, prepare state

Core infrastructure:
  - AWS Lambda handler & EventBridge routing
  - S3 read/write utilities with optimistic concurrency
  - TradingView market data & Nifty 50-EMA regime check
  - Multi-channel notification pipeline (Gmail SMTP + ntfy.sh)
  - Official NSE cash market holiday & weekend guards
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import pandas as pd
import requests
from tvDatafeed import Interval, TvDatafeed

from swing_core.calendar_nse import (
    IST,
    clock_for,
    completed_bar_iloc,
    is_trading_day,
    is_weekend,
    now_ist,
)
from swing_core.messages import (
    Message,
    email_html,
    header_text,
    monitor_message,
    push_parts,
    scan_message,
)
from swing_core.stubs import (
    get_market_regime,
    run_post_market,
    run_pre_market,
    run_signal_engine,
)

# Silence tvDatafeed logging noise
tv_logger = logging.getLogger("tvDatafeed")
tv_logger.setLevel(logging.CRITICAL)
tv_logger.propagate = False
logging.getLogger("tvDatafeed.main").setLevel(logging.CRITICAL)
logging.getLogger("tvDatafeed.main").propagate = False

# Global S3 client
s3_client = boto3.client("s3")

# Lazily initialized guest-mode TradingView client (reused across container invocations)
_tv = None


def get_tv() -> TvDatafeed:
    """Lazily initialize and return the TvDatafeed client."""
    global _tv
    if _tv is None:
        username = os.getenv("TV_USERNAME", "").strip()
        password = os.getenv("TV_PASSWORD", "").strip()
        if username and password:
            print("Initializing TvDatafeed with TradingView credentials...")
            _tv = TvDatafeed(username=username, password=password)
        else:
            print("TV_USERNAME/TV_PASSWORD missing; falling back to guest mode.")
            _tv = TvDatafeed()
    return _tv


def slice_completed(hist: pd.DataFrame | None) -> pd.DataFrame | None:
    """Drop the still-forming cash session so indicators use a completed bar."""
    if hist is None or hist.empty:
        return None
    loc = completed_bar_iloc(hist.index)
    if loc is None:
        return None
    if loc == -2:
        if len(hist) < 2:
            return None
        return hist.iloc[:-1].copy()
    return hist


# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
BUCKET_NAME = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "msu_swing_alerts_2026")
PORTFOLIO_STATE_KEY = os.getenv("PORTFOLIO_STATE_KEY", "portfolio_state.json")
HOLDINGS_LEVELS_KEY = os.getenv("HOLDINGS_LEVELS_KEY", "holdings_levels.json")
EQUITY_CURVE_KEY = os.getenv("EQUITY_CURVE_KEY", "equity_curve.json")


# ==========================================
# S3 READ / WRITE UTILITIES
# ==========================================
def s3_uri(key: str) -> str:
    return f"s3://{BUCKET_NAME}/{key}"


def write_df_to_s3(df: pd.DataFrame, key: str) -> None:
    buf = StringIO()
    df.to_csv(buf, index=False)
    s3_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )
    print(f"Uploaded {key} to S3 ({len(df)} rows).")


def read_df_from_s3(key: str) -> pd.DataFrame | None:
    try:
        obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return pd.read_csv(obj["Body"])
    except Exception as e:
        print(f"Error reading s3://{BUCKET_NAME}/{key}: {e}")
        return None


def append_log_to_s3(text: str, key: str = "execution_log.txt") -> None:
    try:
        existing = ""
        try:
            obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
            existing = obj["Body"].read().decode("utf-8")
        except Exception:
            existing = ""
        updated = existing + text
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=updated.encode("utf-8"),
            ContentType="text/plain",
        )
    except Exception as e:
        print(f"Failed to append to s3://{BUCKET_NAME}/{key}: {e}")


def write_execution_log_json(payload: dict, key: str = "execution_log.json") -> None:
    write_json_to_s3(payload, key)


def read_json_from_s3(key: str) -> dict | None:
    try:
        obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as e:
        print(f"Error reading s3://{BUCKET_NAME}/{key}: {e}")
        return None


def write_json_to_s3(payload: dict, key: str) -> None:
    try:
        s3_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json.dumps(payload, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as e:
        print(f"Failed writing s3://{BUCKET_NAME}/{key}: {e}")


def load_portfolio_state() -> dict:
    state = read_json_from_s3(PORTFOLIO_STATE_KEY)
    if state and isinstance(state, dict):
        return state
    # Local fallback
    local_path = Path("data") / "portfolio_state.json"
    if local_path.exists():
        try:
            return json.loads(local_path.read_text())
        except Exception:
            pass
    return {
        "cash": 100000.0,
        "holdings": [],
        "status": "PORTFOLIO_CONFIRMED",
        "updated": datetime.now(timezone.utc).isoformat(),
    }


def held_symbol_set(state: dict) -> set[str]:
    holdings = state.get("holdings") if isinstance(state, dict) else []
    symbols = set()
    for h in holdings or []:
        sym = (h.get("symbol") or h.get("ticker") or "").strip()
        if sym:
            symbols.add(sym.upper())
    return symbols


# ==========================================
# NOTIFICATION PIPELINE (SES/SMTP & NTFY)
# ==========================================
def send_push_and_email(
    title: str,
    message_body: str,
    email_subject: str,
    priority: str = "default",
    tags: str = "grey_question",
) -> dict:
    """Send push notifications via ntfy.sh and email alerts via Gmail SMTP."""
    ntfy_ok = True
    parts = push_parts(message_body)
    for index, part in enumerate(parts, 1):
        suffix = f" ({index}/{len(parts)})" if len(parts) > 1 else ""
        try:
            response = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=part.encode("utf-8"),
                headers={
                    "Title": header_text(title) + suffix,
                    "Priority": priority,
                    "Tags": tags,
                    "Content-Type": "text/plain; charset=utf-8",
                },
                timeout=30,
            )
            response.raise_for_status()
            print(f"Push notification part {index}/{len(parts)} sent.")
        except Exception as e:
            ntfy_ok = False
            print(f"Failed to send push part {index}/{len(parts)}: {e}")

    email_status = "failed"
    try:
        sender_email = os.getenv("GMAIL_ADDRESS", "")
        sender_app_password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
        receiver_email = os.getenv("ALERT_EMAIL_RECEIVER", sender_email)

        if not sender_email or sender_email.startswith("YOUR_") or not sender_app_password:
            print("Email skipped: set GMAIL_ADDRESS / GMAIL_APP_PASSWORD env vars.")
            email_status = "skipped"
        else:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(message_body, "plain", "utf-8"))
            msg.attach(MIMEText(email_html(email_subject, message_body), "html", "utf-8"))
            msg["Subject"] = email_subject
            msg["From"] = sender_email
            msg["To"] = receiver_email
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(sender_email, sender_app_password)
                server.send_message(msg)
            print("Email alert sent successfully.")
            email_status = "sent"
    except Exception as e:
        print(f"Failed to send email: {e}")
        email_status = "failed"

    return {"ntfy": "sent" if ntfy_ok else "failed", "email": email_status}


# ==========================================
# NSE MARKET-HOLIDAY & WEEKEND GUARDS
# ==========================================
def is_weekend_ist(now: datetime | None = None) -> tuple[bool, str]:
    current = now_ist(now)
    if is_weekend(current):
        return True, f"Market closed: {current.strftime('%A')} (IST weekend)."
    clock = clock_for(current)
    if not clock.is_trading_day(current.date()):
        return True, f"Market closed: {current.strftime('%Y-%m-%d')} is an official NSE holiday."
    return False, ""


# ==========================================
# THE AWS LAMBDA ENTRY POINT
# ==========================================
def lambda_handler(event: dict | None, context: Any = None) -> dict[str, Any]:
    """AWS Lambda entrypoint triggered by EventBridge across the 5 daily slots.

    Supported event actions:
      - PRE_MARKET (09:05 IST)
      - SIGNAL_RUN (09:45, 12:45, 15:30 IST) with slot_name: SLOT_1 | SLOT_2 | SLOT_3
      - POST_MARKET (16:05 IST)

    Backwards compatibility:
      - recommender -> SIGNAL_RUN (SLOT_1)
      - monitor -> SIGNAL_RUN (SLOT_3)
      - scraper -> PRE_MARKET
    """
    event = event or {}
    raw_action = str(event.get("action", "PRE_MARKET")).upper()

    # Map legacy actions
    if raw_action in ("RECOMMENDER", "SCAN"):
        action = "SIGNAL_RUN"
        slot_name = event.get("slot_name", "SLOT_1")
    elif raw_action in ("MONITOR", "EXIT"):
        action = "SIGNAL_RUN"
        slot_name = event.get("slot_name", "SLOT_3")
    elif raw_action == "SCRAPER":
        action = "PRE_MARKET"
        slot_name = None
    else:
        action = raw_action
        slot_name = str(event.get("slot_name", "SLOT_1")).upper()

    ignore_weekend = bool(
        event.get("ignore_weekend") or event.get("force") or event.get("force_weekend")
    )
    suppress_alerts = bool(event.get("suppress_alerts") or event.get("dry_run"))
    dry_run = bool(event.get("dry_run"))

    now = now_ist()
    closed, closed_reason = is_weekend_ist(now)

    print(
        f"lambda_handler action={action} slot={slot_name} "
        f"suppress_alerts={suppress_alerts} dry_run={dry_run} "
        f"market_closed={closed}"
    )

    if closed and not ignore_weekend:
        print(f"Skipping execution: {closed_reason}")
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "SKIPPED", "reason": closed_reason}),
        }

    # Load shared state
    try:
        portfolio_state = load_portfolio_state()
    except Exception as exc:
        print(f"Warning: could not load portfolio state ({exc}); using fallback.")
        portfolio_state = {"cash": 100000.0, "holdings": [], "status": "PORTFOLIO_CONFIRMED"}

    # Initialize market data client (TradingView)
    try:
        tv = get_tv()
    except Exception as exc:
        print(f"Warning: TradingView initialization failed ({exc}); running in stub mode.")
        tv = None

    result: dict[str, Any]

    if action == "PRE_MARKET":
        result = run_pre_market(
            tv=tv,
            s3=s3_client,
            bucket=BUCKET_NAME,
            state=portfolio_state,
            now=now,
        )
        if not suppress_alerts and not dry_run:
            notice: Message = result["notice"]
            send_push_and_email(
                title=notice.title,
                message_body=notice.body,
                email_subject=notice.title,
                priority=notice.priority,
                tags="information_source",
            )
            append_log_to_s3(f"[{now.isoformat()}] PRE_MARKET executed: {result.get('status')}\n")

    elif action == "SIGNAL_RUN":
        result = run_signal_engine(
            slot_name=slot_name,
            tv=tv,
            s3=s3_client,
            bucket=BUCKET_NAME,
            state=portfolio_state,
            now=now,
        )
        if not suppress_alerts and not dry_run:
            notice = result["notice"]
            send_push_and_email(
                title=notice.title,
                message_body=notice.body,
                email_subject=notice.title,
                priority=notice.priority,
                tags="moneybag" if result.get("BUY") else "information_source",
            )
            write_execution_log_json(result)
            append_log_to_s3(f"[{now.isoformat()}] SIGNAL_RUN {slot_name} executed.\n")

    elif action == "POST_MARKET":
        result = run_post_market(
            tv=tv,
            s3=s3_client,
            bucket=BUCKET_NAME,
            state=portfolio_state,
            now=now,
        )
        if not suppress_alerts and not dry_run:
            notice = result["notice"]
            send_push_and_email(
                title=notice.title,
                message_body=notice.body,
                email_subject=notice.title,
                priority=notice.priority,
                tags="bar_chart",
            )
            write_execution_log_json(result)
            append_log_to_s3(f"[{now.isoformat()}] POST_MARKET executed: {result.get('status')}\n")

    else:
        return {
            "statusCode": 400,
            "body": f"Unknown action: {action}. Supported: PRE_MARKET, SIGNAL_RUN, POST_MARKET.",
        }

    return {
        "statusCode": 200,
        "body": json.dumps(result, default=str),
    }


if __name__ == "__main__":
    import sys

    chosen_action = sys.argv[1].upper() if len(sys.argv) > 1 else "PRE_MARKET"
    chosen_slot = sys.argv[2].upper() if len(sys.argv) > 2 else "SLOT_1"
    force_run = "--force" in sys.argv or "--ignore-weekend" in sys.argv
    print(
        lambda_handler(
            {
                "action": chosen_action,
                "slot_name": chosen_slot,
                "ignore_weekend": force_run,
                "suppress_alerts": True,
                "dry_run": True,
            },
            None,
        )
    )
