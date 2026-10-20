"""Route operational actions. Dry runs write nothing. Production buys stay closed."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from swing_core.messages import Message, research_message
from swing_ops.calendar import clock_for
from swing_ops.ledger import S3PaperBook, event_id, view
from swing_ops.lifecycle import advance_events
from swing_ops.paper_job import build_snapshot
from swing_v5.sessions import IST

ROOT = Path(__file__).resolve().parents[1]
CODE_VERSION = "ops-paper-lifecycle"
PAPER_KEY = "research/paper_book.json"
BATCH = 8


def _now(event: dict) -> datetime:
    if event.get("mode") == "replay" and event.get("clock"):
        clock = datetime.fromisoformat(event["clock"])
        return clock if clock.tzinfo else clock.replace(tzinfo=IST)
    return datetime.now(IST)


def _book_key(event: dict) -> str:
    if event.get("isolated") is True:
        key = str(event.get("book_key") or "")
        if not key.startswith("research-test/"):
            raise ValueError("isolated runs must use a research-test key")
        return key
    return PAPER_KEY


def _policy(event: dict) -> dict:
    name = "research_policy.json"
    if event.get("isolated") is True and event.get("policy_name") == "test_only_mechanics":
        name = "test_only_mechanics.json"
    elif event.get("policy_name") == "test_only_mechanics":
        raise ValueError("test-only policy is not installed on the research account")
    return json.loads((ROOT / "config" / "v5" / name).read_text())


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


def _commit_all(book: S3PaperBook, events: list[dict]) -> dict:
    state, etag = book.load()
    last = {"duplicate": True, "state": state, "etag": etag}
    for event in events:
        last = book.commit(last["state"], last["etag"], event)
        if last["duplicate"]:
            continue
    return last


def _send(notify, message, prior: dict) -> dict:
    status = {"ntfy": prior.get("ntfy"), "email": prior.get("email"), "attempts": int(prior.get("attempts") or 0)}
    if status["attempts"] >= 3 and status["ntfy"] != "sent" and status["email"] != "sent":
        return status
    pending = [channel for channel in ("ntfy", "email") if status.get(channel) not in {"sent", "skipped"}]
    if not pending:
        return status
    result = notify(message, channels=pending) if _accepts_channels(notify) else notify(message)
    if not isinstance(result, dict):
        result = {"ntfy": "sent", "email": "sent"}
    for channel in pending:
        status[channel] = result.get(channel, "failed")
    status["attempts"] = status["attempts"] + 1
    return status


def _accepts_channels(notify) -> bool:
    return bool(getattr(notify, "channel_aware", False))


def handle_ops(event: dict, *, s3, bucket: str, notify, suppress: bool) -> dict | None:
    action = event.get("action", "recommender")
    now = _now(event)
    if suppress:
        if action in {"recommender", "monitor"} and clock_for(now).phase() == "closed":
            return {"statusCode": 200, "body": json.dumps({"status": "BLOCKED", "reason": "exchange_closed", "action": action, "notified": False, "writes": 0})}
        if action == "transport_test":
            return {"statusCode": 200, "body": json.dumps({"status": "dry_run", "notified": False, "writes": 0})}
        if action == "paper":
            return {"statusCode": 200, "body": json.dumps({"status": "dry_run", "notified": False, "writes": 0, "production_eligibility": "disabled"})}
        return None
    if action in {"recommender", "monitor"} and clock_for(now).phase() == "closed":
        return _calendar_notice(action, now, s3, bucket, notify)
    if action == "transport_test":
        return _transport(now, s3, bucket, notify)
    if action != "paper":
        return None
    return _paper(event, now, s3, bucket, notify)


def _calendar_notice(action: str, now: datetime, s3, bucket: str, notify) -> dict:
    message = _blocked(action, now)
    signal = event_id({"run": "calendar_block", "action": action, "day": now.astimezone(IST).date().isoformat()})
    book = S3PaperBook(s3, bucket, "research/calendar_notices.json")
    state, _etag = book.load()
    prior = (state.get("deliveries") or {}).get(signal) or {}
    if prior.get("ntfy") in {"sent", "skipped"} and prior.get("email") in {"sent", "skipped"}:
        return {"statusCode": 200, "body": json.dumps({"status": "BLOCKED", "reason": "exchange_closed", "duplicate_skipped": True, "notified": False})}
    status = _send(notify, message, prior)
    stored = book.commit(state, _etag, {"event_id": event_id({"delivery": signal, "attempts": status["attempts"]}), "kind": "delivery", "payload": {"signal_id": signal, **status}})
    if isinstance(stored, dict) and stored.get("duplicate"):
        state, etag = book.load()
        book.commit(state, etag, {"event_id": event_id({"delivery": signal, "attempts": status["attempts"], "retry": True}), "kind": "delivery", "payload": {"signal_id": signal, **status}})
    return {"statusCode": 200, "body": json.dumps({"status": "BLOCKED", "reason": "exchange_closed", "notified": True, "delivery": status})}


def _transport(now: datetime, s3, bucket: str, notify) -> dict:
    message = _test_message(now)
    signal = event_id({"run": "transport_test", "day": now.astimezone(IST).date().isoformat()})
    book = S3PaperBook(s3, bucket, "research/transport_tests.json")
    state, etag = book.load()
    prior = (state.get("deliveries") or {}).get(signal) or {}
    if prior.get("ntfy") in {"sent", "skipped"} and prior.get("email") in {"sent", "skipped"}:
        return {"statusCode": 200, "body": json.dumps({"status": "duplicate_skipped", "notified": False})}
    status = _send(notify, message, prior)
    book.commit(state, etag, {"event_id": event_id({"delivery": signal, "attempts": status["attempts"]}), "kind": "delivery", "payload": {"signal_id": signal, **status}})
    return {"statusCode": 200, "body": json.dumps({"status": "sent", "notified": True, "delivery": status})}


def _paper(event: dict, now: datetime, s3, bucket: str, notify) -> dict:
    key = _book_key(event)
    policy = _policy(event)
    book = S3PaperBook(s3, bucket, key)
    state, etag = book.load()
    if event.get("isolated") is True and event.get("hypothetical_cash") is not None and not (state.get("account") or {}).get("initialized"):
        stored = book.commit(state, etag, {
            "event_id": event_id({"init": key, "label": "hypothetical_research_account"}),
            "kind": "paper_account_init",
            "payload": {"cash": float(event["hypothetical_cash"]), "label": "hypothetical_research_account"},
        })
        state, etag = stored["state"], stored["etag"]
    identities = _identities()
    manifest = json.loads((ROOT / "config" / "ops" / "universe_manifest.json").read_text())
    panels, index_rows, source_notes, coverage_complete = _panels(event, s3, bucket, key)
    session = clock_for(now).latest_completed_session()
    session_text = None if session is None else session.isoformat()
    advanced = advance_events(state, panels, session_text)
    if advanced:
        stored = _commit_all(book, advanced)
        state = stored["state"]
    built = build_snapshot(
        now=now, identities=identities, panels=panels, index_rows=index_rows,
        holdings=event.get("holdings") or [], policy=policy, code_version=CODE_VERSION,
        state=state, manifest=manifest if event.get("panels") is None else {"symbols": event.get("universe") or [{"symbol": symbol, "role": "injected"} for symbol in panels]},
    )
    built["snapshot"]["coverage"]["complete"] = bool(coverage_complete and built["snapshot"]["coverage"].get("complete"))
    built["snapshot"]["source_notes"] = source_notes
    built["snapshot"]["advance"] = {"status": "advanced", "processed": len(advanced), "execution_proxy": "next_session_open_not_verified_0925"} if advanced else {"status": "noop", "reason": "no pending orders or open positions", "execution_proxy": "next_session_open_not_verified_0925"}
    built["events"][-1]["payload"] = built["snapshot"]
    signal = built["events"][-1]["event_id"]
    before = {row.get("event_id") for row in state.get("events") or []}
    stored = _commit_all(book, built["events"])
    current = view(stored["state"])
    built["snapshot"]["signal_id"] = signal
    account = current["account"]
    message = research_message(
        built["snapshot"],
        current,
        {"cash_after_reservations_and_fills": account.get("cash"), "paper_account": "uninitialized" if not account.get("initialized") else account.get("label")},
    )
    message = Message(message.title, message.body + "\n\nSignal " + signal + "\nA repeat of this message is the same signal, not a second order.", message.priority, message.tags)
    prior = (stored["state"].get("deliveries") or {}).get(signal) or {}
    terminal = prior.get("ntfy") in {"sent", "skipped"} and prior.get("email") in {"sent", "skipped"}
    if signal in before and terminal:
        return {"statusCode": 200, "body": json.dumps({"status": "duplicate_skipped", "logical_id": signal, "paper_orders": current["orders"] and built["snapshot"]["paper_orders"], "advance": built["snapshot"]["advance"]["status"], "production_eligibility": "disabled"})}
    status = _send(notify, message, prior)
    book.commit(stored["state"], stored["etag"], {"event_id": event_id({"delivery": signal, "attempts": status.get("attempts")}), "kind": "delivery", "payload": {"signal_id": signal, **status}})
    return {"statusCode": 200, "body": json.dumps({
        "status": "stored",
        "logical_id": signal,
        "evaluation_stage": built["snapshot"]["evaluation_stage"],
        "evaluated": built["snapshot"]["evaluated_count"],
        "paper_orders": built["snapshot"]["paper_orders"],
        "advance": built["snapshot"]["advance"]["status"],
        "production_eligibility": "disabled",
        "price_session": built["snapshot"]["price_session"],
        "paper_account": built["snapshot"]["paper_account"],
        "families": [{k: row.get(k) for k in ("family", "production_status", "evaluation", "detection", "names_evaluated", "setups_detected", "orders_admitted")} for row in built["snapshot"]["families"]],
        "coverage": built["snapshot"]["coverage"],
        "delivery": status,
    }, default=str)}


def _identities() -> list[dict]:
    import csv
    with (ROOT / "artifacts" / "v5" / "forward" / "instrument_resolution.csv").open() as handle:
        return list(csv.DictReader(handle))


def _panels(event, s3, bucket, book_key):
    if event.get("panels") is not None:
        return event["panels"], event.get("index_rows") or [], [{"source": "injected"}], False
    from swing_ops.sources import yahoo_bars, yahoo_index_close
    manifest = json.loads((ROOT / "config" / "ops" / "universe_manifest.json").read_text())
    prefix = book_key.rsplit("/", 1)[0]
    checkpoint_key = prefix + "/fetch_checkpoint.json"
    try:
        current = json.loads(s3.get_object(Bucket=bucket, Key=checkpoint_key)["Body"].read())
        start = int(current.get("next") or 0)
    except Exception:
        start = 0
    symbols = [row["symbol"] for row in manifest["symbols"] if row.get("role") != "protected_holding"]
    if start >= len(symbols):
        start = 0
    batch = symbols[start:start + BATCH]
    notes = []
    panels = {}
    try:
        index_rows = yahoo_index_close()
    except Exception as exc:
        return {}, [], [{"symbol": "^NSEI", "status": "fetch_failed", "error": type(exc).__name__}], False
    for symbol in batch:
        cache_key = f"{prefix}/cache/{symbol}.json"
        try:
            fetched = yahoo_bars(symbol)
            panels[symbol] = [{**row, "vendor": fetched["vendor"]} for row in fetched["rows"]]
            s3.put_object(Bucket=bucket, Key=cache_key, Body=json.dumps({"vendor": fetched["vendor"], "adjustment": fetched["adjustment"], "retrieved_at": fetched["retrieved_at"], "bars": len(fetched["rows"])}).encode(), ContentType="application/json")
            notes.append({"symbol": symbol, "status": "fetched", "vendor": fetched["vendor"], "retrieved_at": fetched["retrieved_at"], "bars": len(fetched["rows"])})
        except Exception as exc:
            notes.append({"symbol": symbol, "status": "fetch_failed", "error": type(exc).__name__})
    nxt = start + len(batch)
    s3.put_object(Bucket=bucket, Key=checkpoint_key, Body=json.dumps({"next": 0 if nxt >= len(symbols) else nxt, "total": len(symbols), "batch": BATCH, "manifest_sha256": manifest.get("manifest_sha256")}).encode(), ContentType="application/json")
    return panels, index_rows, notes, nxt >= len(symbols) and start == 0
