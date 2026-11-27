"""Self-contained email (SES / Gmail SMTP) and ntfy.sh notification pipeline for AWS container."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
import json
import math
import os
import re
import smtplib
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import requests

from src.aws.calendar_nse import IST

REASONS = {
    "trailing_stop": "Trailing stop (12% drop from watermark)",
    "rank_buffer_exit": "Rank buffer exit (dropped below Top 40)",
    "max_hold_weeks": "Time limit (12-week maximum holding period)",
    "hard_sector_cap": "Blocked: sector limit reached (max 2 open/sector)",
    "new_sector_weekly_cap": "Blocked: max 1 new position per sector per week",
    "weekly_entry_cap": "Blocked: max 2 new entries per week",
    "portfolio_full": "Blocked: max 10 open positions reached",
    "RISK_ON": "Market regime: BULL (long entries active)",
    "RISK_OFF": "Market regime: BEAR (long entries restricted)",
    "DATA_INVALID": "Market data unverified",
    "MARKET_CLOSED": "Exchange is closed today",
}


def clean(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else "Unavailable"


def reason(value: Any) -> str:
    text = clean(value)
    return REASONS.get(text, REASONS.get(text.lower(), text.replace("_", " ")))


def number(value: Any) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def rupees(value: Any) -> str:
    n = number(value)
    if n is None:
        return "Unavailable"
    whole, paise = f"{abs(n):.2f}".split(".")
    tail, lead = whole[-3:], whole[:-3]
    groups = []
    while lead:
        groups.insert(0, lead[-2:])
        lead = lead[:-2]
    amount = ",".join(groups + [tail]) + "." + paise
    return ("−" if n < 0 else "") + "₹" + amount


def when(value: Any) -> str:
    if value is None:
        return "Time unavailable"
    try:
        dt = datetime.fromisoformat(str(value).replace(" UTC", "+00:00").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return clean(value) + " (timezone unavailable)"
        return dt.astimezone(IST).strftime("%d %b %Y · %H:%M IST")
    except (ValueError, TypeError):
        return clean(value)


def header_text(value: str) -> str:
    return re.sub(r"[^\x20-\x7e]", "", clean(value))[:150]


def email_html(title: str, body: str) -> str:
    """Format alert message as a responsive, modern HTML email card."""
    cards = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        heading = escape(lines[0])
        details = "<br>".join(escape(line) for line in lines[1:])
        cards.append(
            '<div style="margin:0 0 12px;padding:16px;background:#ffffff;border:1px solid #dce3eb;border-radius:10px">'
            f'<div style="font-size:16px;font-weight:700;color:#142b45;margin-bottom:8px">{heading}</div>'
            f'<div style="font-size:14px;line-height:1.65;color:#293b50;overflow-wrap:anywhere">{details}</div>'
            "</div>"
        )
    return (
        "<!doctype html><html><head><meta name="
        '"viewport" content="width=device-width,initial-scale=1">'
        f"<meta charset=\"utf-8\"><title>{escape(title)}</title></head>"
        '<body style="margin:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif">'
        '<main style="max-width:600px;margin:auto;padding:20px 14px">'
        '<div style="font-size:12px;font-weight:600;letter-spacing:1px;color:#526477;margin:0 0 14px">'
        "NSE QUANT SWING SYSTEM · V2 PRODUCTION SHADOW"
        "</div>"
        + "".join(cards)
        + "</main></body></html>"
    )


def push_parts(body: str, max_bytes: int = 3800) -> list[str]:
    """Chunk message body into UTF-8 parts without breaking cards."""
    chunks: list[str] = []
    current = ""
    for block in body.split("\n\n"):
        pieces = [block] if len(block.encode("utf-8")) <= max_bytes else block.splitlines()
        for piece in pieces:
            separator = "\n\n" if current else ""
            if len((current + separator + piece).encode("utf-8")) <= max_bytes:
                current += separator + piece
                continue
            if current:
                chunks.append(current)
                current = ""
            while len(piece.encode("utf-8")) > max_bytes:
                prefix = piece.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
                chunks.append(prefix)
                piece = piece[len(prefix) :]
            current = piece
    if current:
        chunks.append(current)
    return chunks or ["No message content available."]


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: str = "default"
    tags: str = "information_source"

    @property
    def html(self) -> str:
        return email_html(self.title, self.body)


def buy_card(b: dict) -> str:
    symbol = clean(b.get("symbol", b.get("ticker")))
    qty = clean(b.get("qty", b.get("shares", 0)))
    price = rupees(b.get("price", b.get("entry_price", 0.0)))
    stop = rupees(b.get("stop", b.get("stop_loss", 0.0)))
    rank = clean(b.get("rank", b.get("v3_rank")))
    sector = clean(b.get("sector"))
    lines = [
        f"BUY · {symbol} · {qty} shares",
        f"Entry: {price} | Stop: {stop}",
        f"V3 Rank: #{rank} | Sector: {sector}",
        f"Max Debit: {rupees(b.get('debit', b.get('investment', 0.0)))}",
    ]
    if b.get("execution_note"):
        lines.append(f"Note: {clean(b['execution_note'])}")
    return "\n".join(lines)


def sell_card(s: dict) -> str:
    symbol = clean(s.get("symbol"))
    qty = clean(s.get("qty"))
    trigger = reason(s.get("trigger", s.get("kind", "exit_signal")))
    last_p = rupees(s.get("ltp", s.get("price")))
    pnl = s.get("pnl_pct")
    pnl_str = f" ({pnl:+.1f}%)" if pnl is not None else ""
    lines = [
        f"SELL · {symbol} · {qty} shares",
        f"Trigger: {trigger}",
        f"Last Price: {last_p}{pnl_str}",
    ]
    return "\n".join(lines)


def scan_message(result: dict) -> Message:
    signals = result.get("signals") or []
    buys = result.get("BUY") or [s for s in signals if s.get("action") == "BUY"]
    sells = result.get("SELL") or [s for s in signals if s.get("action") == "SELL"]
    holds = result.get("HOLD") or [s for s in signals if s.get("action") == "HOLD"]
    slot = clean(result.get("slot", result.get("slot_name", "SIGNAL_RUN")))

    if buys and sells:
        title = f"V3 Signals | {len(buys)} BUY, {len(sells)} SELL · {slot}"
        priority = "high"
        tag = "bell"
    elif buys:
        title = f"V3 Signals | {len(buys)} BUY alert{'s' if len(buys) != 1 else ''} · {slot}"
        priority = "high"
        tag = "moneybag"
    elif sells:
        title = f"V3 Signals | {len(sells)} SELL alert{'s' if len(sells) != 1 else ''} · {slot}"
        priority = "high"
        tag = "warning"
    else:
        title = f"V3 Signals | No orders · {slot}"
        priority = "default"
        tag = "information_source"

    blocks = [f"{title}\n{when(result.get('as_of', result.get('timestamp_ist', datetime.now(IST).isoformat())))}"]

    if buys:
        blocks.append("BUY SIGNALS (Top V3 Rank + A+ HardCap)\n" + "\n\n".join(buy_card(b) for b in buys))
    if sells:
        blocks.append("SELL SIGNALS (Trailing Stop / Rank-Buffer Exit)\n" + "\n\n".join(sell_card(s) for s in sells))
    if not buys and not sells:
        blocks.append("Order Status\nNo new Buy or Sell orders generated this slot.")

    regime = result.get("regime", result.get("regime_state", "UNKNOWN"))
    detail = clean(result.get("regime_detail", (result.get("market") or {}).get("regime_detail", regime)))
    blocks.append(f"Market Regime Context\n{detail}")

    stale_banner = result.get("STALE_QUOTE_WARNING")
    if stale_banner or result.get("stale_quotes"):
        blocks.append(
            "STALE_QUOTE_WARNING\n"
            + clean(
                stale_banner
                or "Live intraday quotes unavailable — using EOD closes for marks/stops/entries"
            )
        )

    if holds:
        blocks.append(
            f"Active Holdings ({len(holds)})\n"
            + "\n".join(
                f"{clean(h.get('symbol'))} ({clean(h.get('qty', 0))} shs): {clean(h.get('reason', 'holding policy unchanged'))}"
                for h in holds[:10]
            )
            + (f"\n... and {len(holds)-10} more" if len(holds) > 10 else "")
        )

    limits = result.get("limits") or {}
    cash = limits.get("cash", result.get("cash"))
    if cash is not None:
        blocks.append(f"Account Context\nAvailable Cash: {rupees(cash)}")

    blocks.append("Execution Notice\nSHADOW / ALERT-ONLY MODE: No automated broker orders placed.")
    return Message(title, "\n\n".join(blocks), priority, tag)


def pre_market_message(result: dict) -> Message:
    slot = clean(result.get("timestamp_ist", datetime.now(IST).strftime("%H:%M IST")))
    regime = clean(result.get("regime_state", "UNKNOWN"))
    nifty = result.get("nifty_prev_close")
    nifty_str = rupees(nifty) if nifty else "Unavailable"
    univ_count = result.get("universe_count", 0)
    notes = clean(result.get("notes", "System ready"))
    title = f"Pre-Market | System Health · {slot}"
    body = (
        f"{title}\n\n"
        f"Connectivity & Environment\nStatus: {result.get('status', 'OK')}\nTimestamp: {slot}\n\n"
        f"Market Context\nNifty Prev Close: {nifty_str}\nRegime State: {regime}\nActive Watchlist: {univ_count} symbols\n\n"
        f"Operational Notes\n{notes}\n\n"
        "Execution Notice\nSHADOW / ALERT-ONLY MODE: No automated broker orders placed."
    )
    return Message(title, body, priority="default", tags="sunrise")


def post_market_message(result: dict) -> Message:
    slot = clean(result.get("timestamp_ist", datetime.now(IST).strftime("%H:%M IST")))
    ingested = result.get("eod_rows_ingested", 0)
    regime = clean(result.get("updated_regime_state", "UNKNOWN"))
    uri = clean(result.get("s3_artifact_uri", "v2/state/latest.json"))
    title = f"Post-Market | EOD Summary · {slot}"
    body = (
        f"{title}\n\n"
        f"EOD Ingestion & Model Updates\nStatus: {result.get('status', 'OK')}\nBhavcopy Rows Ingested: {ingested}\nUpdated Regime: {regime}\n\n"
        f"Artifact State\nPersisted: {uri}\n\n"
        "Execution Notice\nSHADOW / ALERT-ONLY MODE: No automated broker orders placed."
    )
    return Message(title, body, priority="default", tags="twilight")


def monitor_message(doc: dict) -> Message:
    return scan_message(doc)


def send_push_and_email(
    title: str,
    message_body: str,
    email_subject: str,
    priority: str = "default",
    tags: str = "information_source",
) -> dict[str, str]:
    """Deliver notification via ntfy.sh and Amazon SES (or Gmail SMTP fallback)."""
    # 1. NTFY Delivery
    ntfy_topic = os.getenv("NTFY_TOPIC", "msu_swing_alerts_2026")
    ntfy_ok = True
    if ntfy_topic:
        parts = push_parts(message_body)
        for idx, part in enumerate(parts, 1):
            suffix = f" ({idx}/{len(parts)})" if len(parts) > 1 else ""
            try:
                resp = requests.post(
                    f"https://ntfy.sh/{ntfy_topic}",
                    data=part.encode("utf-8"),
                    headers={
                        "Title": header_text(title) + suffix,
                        "Priority": priority,
                        "Tags": tags,
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                    timeout=20,
                )
                resp.raise_for_status()
            except Exception as exc:
                ntfy_ok = False
                print(f"[ntfy] delivery failed: {exc}")

    # 2. Email Delivery (Prefer SES, fallback to Gmail SMTP)
    email_status = "skipped"
    from_addr = os.getenv("ALERT_FROM_EMAIL") or os.getenv("GMAIL_ADDRESS")
    to_addr = os.getenv("ALERT_TO_EMAIL") or from_addr

    if from_addr and to_addr and not from_addr.startswith("YOUR_"):
        # Try SES first if region/credentials available
        ses_sent = False
        try:
            region = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
            ses = boto3.client("ses", region_name=region)
            ses.send_email(
                Source=from_addr,
                Destination={"ToAddresses": [to_addr]},
                Message={
                    "Subject": {"Data": email_subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": message_body, "Charset": "UTF-8"},
                        "Html": {"Data": email_html(email_subject, message_body), "Charset": "UTF-8"},
                    },
                },
            )
            email_status = "sent_ses"
            ses_sent = True
            print(f"[SES] email delivered to {to_addr}")
        except Exception as ses_exc:
            # Fall back to Gmail SMTP if configured
            pass

        if not ses_sent:
            app_pw = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
            if app_pw and from_addr.endswith("@gmail.com"):
                try:
                    msg = MIMEMultipart("alternative")
                    msg.attach(MIMEText(message_body, "plain", "utf-8"))
                    msg.attach(MIMEText(email_html(email_subject, message_body), "html", "utf-8"))
                    msg["Subject"] = email_subject
                    msg["From"] = from_addr
                    msg["To"] = to_addr
                    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
                        server.login(from_addr, app_pw)
                        server.send_message(msg)
                    email_status = "sent_smtp"
                    print(f"[SMTP] email delivered to {to_addr}")
                except Exception as smtp_exc:
                    email_status = f"failed: {smtp_exc}"
                    print(f"[SMTP] email failed: {smtp_exc}")
            else:
                if email_status == "skipped":
                    email_status = "no_credentials"

    return {"ntfy": "sent" if ntfy_ok else "failed", "email": email_status}
