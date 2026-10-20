"""Route operational actions. Dry runs write nothing. Production buys stay closed."""

from __future__ import annotations

import hashlib
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
CODE_VERSION = "ops-evidence-v3"
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
    if action == "initialize_paper_account":
        return _initialize(event, s3, bucket)
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


def _initialize(event: dict, s3, bucket: str) -> dict:
    if event.get("confirm_hypothetical_assumption") is not True:
        return {"statusCode": 400, "body": json.dumps({"status": "rejected", "reason": "explicit_confirmation_required"})}
    if float(event.get("capital") or 0) != 5748:
        return {"statusCode": 400, "body": json.dumps({"status": "rejected", "reason": "unexpected_capital_assumption"})}
    policy = json.loads((ROOT / "config" / "v5" / "research_policy.json").read_text())
    note = "hypothetical_research_reference_not_broker_cash"
    logical = event_id({"run": "paper_account_init", "cash": 5748, "label": note, "policy": policy.get("version")})
    book = S3PaperBook(s3, bucket, _book_key(event) if event.get("isolated") else PAPER_KEY)
    state, etag = book.load()
    if (state.get("account") or {}).get("initialized") and (state.get("account") or {}).get("cash") != 5748:
        return {"statusCode": 200, "body": json.dumps({"status": "rejected", "reason": "account_already_initialized"})}
    stored = book.commit(state, etag, {
        "event_id": logical,
        "kind": "paper_account_init",
        "payload": {"cash": 5748, "label": note, "assumption_note": note, "policy_version": policy.get("version"), "initialized_at": now_iso()},
    })
    account = stored["state"]["account"]
    return {"statusCode": 200, "body": json.dumps({
        "status": "duplicate_skipped" if stored["duplicate"] else "initialized",
        "event_id": logical,
        "initialized_at": account.get("assumption_note") and event.get("initialized_at"),
        "capital": account.get("cash"),
        "label": account.get("label"),
        "policy_version": policy.get("version"),
        "duplicate": stored["duplicate"],
    })}


def now_iso() -> str:
    from datetime import datetime
    return datetime.now(IST).isoformat(timespec="seconds")


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
    session = clock_for(now).latest_completed_session()
    session_text = None if session is None else session.isoformat()
    panels, index_rows, source_notes, coverage_complete = _panels(event, s3, bucket, key, session_text, manifest, state)
    advanced = advance_events(state, panels, session_text)
    if advanced:
        stored = _commit_all(book, advanced)
        state = stored["state"]
    evidence = _symbol_evidence(event, s3, bucket, key, session_text, manifest, panels)
    config_blob = json.dumps({"policy": policy, "fundamentals": json.loads((ROOT / "config" / "ops" / "fundamentals_snapshots.json").read_text()), "promotion": json.loads((ROOT / "config" / "ops" / "promotion_approval.json").read_text())}, sort_keys=True)
    built = build_snapshot(
        now=now, identities=identities, panels=panels, index_rows=index_rows,
        holdings=event.get("holdings") or [], policy=policy, code_version=CODE_VERSION,
        state=state, manifest=manifest if event.get("panels") is None else {"version": "injected", "symbols": event.get("universe") or [{"symbol": symbol, "role": "injected"} for symbol in panels]},
        metadata=event.get("metadata"),
        symbol_evidence=evidence,
        evidence_hash=hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest() if evidence is not None else None,
        config_hash=hashlib.sha256(config_blob.encode()).hexdigest(),
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
    if event.get("panels") is None and not coverage_complete:
        return {"statusCode": 200, "body": json.dumps({"status": "continuation", "logical_id": signal, "notified": False, "production_eligibility": "disabled", "coverage": built["snapshot"]["coverage"]})}
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


def _symbol_evidence(event, s3, bucket, key, session, manifest, panels):
    if event.get("symbol_evidence") is not None:
        return event["symbol_evidence"]
    if event.get("panels") is not None or event.get("metadata") is not None or not session:
        return None
    from swing_ops.evidence import PARSER_VERSION, collect_session_evidence, symbol_evidence
    prefix = key.rsplit("/", 1)[0]
    cache_key = f"{prefix}/evidence/{session}.json"
    store = _read_json(s3, bucket, cache_key)
    if store is None or store.get("session") != session or store.get("parser_version") != PARSER_VERSION:
        store = collect_session_evidence(session)
        archived = []
        for item in store.pop("archives", []):
            raw_key = f"{prefix}/evidence/{session}/raw/{item['name']}-{item['sha256'][:16]}"
            s3.put_object(Bucket=bucket, Key=raw_key, Body=item["body"], ContentType=item.get("content_type") or "application/octet-stream")
            archived.append({"name": item["name"], "sha256": item["sha256"], "archive_key": raw_key})
        store["raw_archives"] = archived
        s3.put_object(Bucket=bucket, Key=cache_key, Body=json.dumps(store).encode(), ContentType="application/json")
    found = {}
    for item in manifest.get("symbols") or []:
        if item.get("role") == "protected_holding":
            continue
        symbol = item["symbol"]
        rows = panels.get(symbol) or []
        found[symbol] = symbol_evidence(
            session=session, symbol=symbol, calendar=store.get("calendar") or {}, identity=store.get("identity") or {},
            actions=store.get("actions") or {}, restrictions=store.get("restrictions") or {},
            bar_start=rows[0]["Date"] if rows else None,
        )
    return found


def _identities() -> list[dict]:
    import csv
    with (ROOT / "artifacts" / "v5" / "forward" / "instrument_resolution.csv").open() as handle:
        return list(csv.DictReader(handle))


def _read_json(s3, bucket, key):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return None


def _panels(event, s3, bucket, book_key, session: str | None, manifest: dict, state: dict):
    if event.get("panels") is not None:
        return event["panels"], event.get("index_rows") or [], [{"source": "injected"}], False
    from swing_ops.sources import yahoo_bars, yahoo_index_close
    prefix = book_key.rsplit("/", 1)[0]
    work_key = f"{prefix}/work/{session}/{manifest.get('manifest_sha256')}.json"
    work = _read_json(s3, bucket, work_key)
    symbols = [row["symbol"] for row in manifest["symbols"] if row.get("role") != "protected_holding"]
    priority = [row.get("symbol") for row in (state.get("positions") or []) + (state.get("orders") or [])]
    if work is None or work.get("session") != session or work.get("manifest") != manifest.get("manifest_sha256"):
        ordered = [symbol for symbol in priority if symbol in symbols] + [symbol for symbol in symbols if symbol not in priority]
        work = {"session": session, "manifest": manifest.get("manifest_sha256"), "order": ordered, "items": {symbol: {"status": "pending"} for symbol in ordered}}
    notes = []
    try:
        index_rows = yahoo_index_close()
    except Exception as exc:
        return {}, [], [{"symbol": "^NSEI", "status": "fetch_failed", "error": type(exc).__name__}], False
    import time
    deadline = time.monotonic() + float(event.get("batch_seconds") or 180)
    while time.monotonic() < deadline:
        pending = [symbol for symbol in work["order"] if work["items"].get(symbol, {}).get("status") == "pending"]
        if not pending:
            break
        for symbol in pending[:BATCH]:
            cache_key = f"{prefix}/bars/{session}/{symbol}.json"
            try:
                fetched = yahoo_bars(symbol)
                rows = [row for row in fetched["rows"] if row["Date"] <= session]
                if not rows or rows[-1]["Date"] != session:
                    work["items"][symbol] = {"status": "failed", "reason": "stale_or_partial_session"}
                else:
                    body = {"vendor": fetched["vendor"], "adjustment": fetched["adjustment"], "retrieved_at": fetched["retrieved_at"], "session": session, "rows": rows}
                    s3.put_object(Bucket=bucket, Key=cache_key, Body=json.dumps(body).encode(), ContentType="application/json")
                    work["items"][symbol] = {"status": "validated", "cache": cache_key, "bars": len(rows), "retrieved_at": fetched["retrieved_at"]}
                notes.append({"symbol": symbol, **work["items"][symbol]})
            except Exception as exc:
                work["items"][symbol] = {"status": "failed", "reason": type(exc).__name__}
                notes.append({"symbol": symbol, "status": "failed", "reason": type(exc).__name__})
        s3.put_object(Bucket=bucket, Key=work_key, Body=json.dumps(work).encode(), ContentType="application/json")
    panels = {}
    for symbol, item in work["items"].items():
        if item.get("status") != "validated":
            continue
        cached = _read_json(s3, bucket, item["cache"])
        if cached and cached.get("session") == session:
            panels[symbol] = cached["rows"]
    remaining = sum(1 for item in work["items"].values() if item.get("status") == "pending")
    return panels, index_rows, notes, remaining == 0
