"""Route operational actions without writing the broker book."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from swing_core.messages import Message, research_message
from swing_ops.calendar import clock_for
from swing_ops.ledger import S3PaperBook, event_id
from swing_ops.paper_job import build_snapshot
from swing_v5.sessions import IST

ROOT = Path(__file__).resolve().parents[1]
CODE_VERSION = "ops-reviewed-activation"
PAPER_KEY = "research/paper_book.json"


def _now(event: dict) -> datetime:
    if event.get("mode") == "replay" and event.get("clock"):
        clock = datetime.fromisoformat(event["clock"])
        return clock if clock.tzinfo else clock.replace(tzinfo=IST)
    return datetime.now(IST)


def _blocked(action: str, now: datetime) -> Message:
    clock = clock_for(now)
    session = clock.latest_completed_session()
    title = "Holdings | Check blocked" if action == "monitor" else "Swing | Scan blocked"
    body = "\n\n".join([
        f"{title}\n{now.astimezone(IST).strftime('%d %b %Y · %H:%M IST')}",
        "DATA CHECK\nThe exchange is closed. This run did not price the book and it is not an all-clear.",
        "Completed price session: " + (session.isoformat() if session else "Unavailable"),
        "No buy or sell instruction was produced.",
    ])
    return Message(title, body, "default", "warning")


def _test_message(now: datetime) -> Message:
    body = "\n\n".join([
        f"TEST | Notification path\n{now.astimezone(IST).strftime('%d %b %Y · %H:%M IST')}",
        "This message checks ntfy and email delivery.",
        "It is not a buy, sell, hold, or paper order. Do not place a trade from it.",
    ])
    return Message("TEST | Notification path", body, "default", "test_tube")


def handle_ops(event: dict, *, s3, bucket: str, notify, suppress: bool) -> dict | None:
    action = event.get("action", "recommender")
    now = _now(event)
    if action in {"recommender", "monitor"} and clock_for(now).phase() == "closed":
        message = _blocked(action, now)
        if not suppress:
            notify(message)
        return {"statusCode": 200, "body": json.dumps({"status": "BLOCKED", "reason": "exchange_closed", "action": action, "notified": not suppress})}
    if action == "transport_test":
        message = _test_message(now)
        logical = event_id({"run": "transport_test", "day": now.astimezone(IST).date().isoformat()})
        book = S3PaperBook(s3, bucket, "research/transport_tests.json")
        state, etag = book.load()
        stored = book.commit(state, etag, {"event_id": logical, "kind": "transport_test", "payload": {"at": now.isoformat()}})
        if stored["duplicate"]:
            return {"statusCode": 200, "body": json.dumps({"status": "duplicate_skipped", "notified": False})}
        if not suppress:
            notify(message)
        return {"statusCode": 200, "body": json.dumps({"status": "sent", "notified": not suppress})}
    if action != "paper":
        return None
    identities = _identities()
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    panels, index_rows, source_notes = _panels(event)
    holdings = event.get("holdings") or []
    built = build_snapshot(
        now=now, identities=identities, panels=panels, index_rows=index_rows,
        holdings=holdings, policy=policy, code_version=CODE_VERSION,
    )
    built["snapshot"]["source_notes"] = source_notes
    book = S3PaperBook(s3, bucket, PAPER_KEY)
    state, etag = book.load()
    pending = [row for row in state.get("orders") or [] if row.get("order_id") not in {item.get("order_id") for item in state.get("positions") or []}]
    if pending or state.get("positions"):
        built["snapshot"]["advance"] = {"status": "waiting", "reason": "open paper records exist but this run did not receive a newer eligible session"}
        built["event"]["payload"] = built["snapshot"]
    if suppress:
        return {"statusCode": 200, "body": json.dumps({
            "status": "dry_run",
            "logical_id": built["event"]["event_id"],
            "evaluation_stage": built["snapshot"]["evaluation_stage"],
            "evaluated": built["snapshot"]["evaluated_count"],
            "paper_orders": 0,
            "advance": built["snapshot"]["advance"]["status"],
            "production_eligibility": "disabled",
            "price_session": built["snapshot"]["price_session"],
            "notified": False,
        }, default=str)}
    stored = book.commit(state, etag, built["event"])
    if not stored["duplicate"]:
        s3.put_object(
            Bucket=bucket,
            Key=f"research/sources/{built['snapshot'].get('price_session') or 'undated'}.json",
            Body=json.dumps({"notes": source_notes, "session": built["snapshot"].get("price_session"), "code_version": CODE_VERSION}, default=str).encode(),
            ContentType="application/json",
        )
    message = research_message(
        built["snapshot"],
        {"orders": state.get("orders") or [], "open_positions": state.get("positions") or [], "pending": pending},
        {"cash_after_reservations_and_fills": None},
    )
    if not stored["duplicate"] and not suppress:
        notify(message)
    return {"statusCode": 200, "body": json.dumps({
        "status": "duplicate_skipped" if stored["duplicate"] else "stored",
        "logical_id": built["event"]["event_id"],
        "evaluation_stage": built["snapshot"]["evaluation_stage"],
        "evaluated": built["snapshot"]["evaluated_count"],
        "paper_orders": 0,
        "advance": built["snapshot"]["advance"]["status"],
        "production_eligibility": "disabled",
        "price_session": built["snapshot"]["price_session"],
        "families": [{k: row[k] for k in ("family", "production_status", "evaluation", "detection", "production_reason")} for row in built["snapshot"]["families"]],
        "coverage": built["snapshot"]["coverage"],
    }, default=str)}


def _identities() -> list[dict]:
    import csv
    path = ROOT / "artifacts" / "v5" / "forward" / "instrument_resolution.csv"
    with path.open() as handle:
        return list(csv.DictReader(handle))


def _panels(event: dict) -> tuple[dict, list[dict], list[dict]]:
    if event.get("panels") is not None:
        return event["panels"], event.get("index_rows") or [], [{"source": "injected"}]
    from swing_ops.sources import yahoo_bars, yahoo_index_close
    notes = []
    panels = {}
    try:
        index_rows = yahoo_index_close()
    except Exception as exc:
        return {}, [], [{"symbol": "^NSEI", "status": "fetch_failed", "error": type(exc).__name__}]
    for symbol in ("SUNTV", "ZOTA", "CMLL"):
        try:
            fetched = yahoo_bars(symbol)
            panels[symbol] = fetched["rows"]
            notes.append({"symbol": symbol, "status": "fetched", "vendor": fetched["vendor"], "retrieved_at": fetched["retrieved_at"], "bars": len(fetched["rows"])})
        except Exception as exc:
            notes.append({"symbol": symbol, "status": "fetch_failed", "error": type(exc).__name__})
    return panels, index_rows, notes
