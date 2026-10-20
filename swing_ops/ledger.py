"""Durable paper book. Conditional writes reject a stale or colliding update."""

from __future__ import annotations

import hashlib
import json


class LedgerConflict(Exception):
    pass


MAX_EVENTS = 400


def empty_state() -> dict:
    return {
        "events": [],
        "orders": [],
        "positions": [],
        "exits": [],
        "events_dropped": 0,
        "retention_limit": MAX_EVENTS,
        "account": {
            "label": "hypothetical_research_account",
            "initialized": False,
            "cash": None,
            "reserved": 0.0,
        },
        "deliveries": {},
    }


def event_id(parts: dict) -> str:
    body = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:24]


def apply_event(state: dict, event: dict) -> bool:
    if any(row.get("event_id") == event["event_id"] for row in state.get("events") or []):
        return False
    state.setdefault("events", []).append(event)
    kind = event.get("kind")
    payload = event.get("payload") or {}
    account = state.setdefault("account", empty_state()["account"])
    if kind == "paper_account_init":
        account["initialized"] = True
        account["cash"] = float(payload["cash"])
        account["label"] = payload.get("label", "hypothetical_research_account")
        account["reserved"] = float(account.get("reserved") or 0.0)
    elif kind == "paper_order":
        state.setdefault("orders", []).append(payload)
        if payload.get("qty") is not None and payload.get("limit") is not None:
            account["reserved"] = float(account.get("reserved") or 0.0) + float(payload["qty"]) * float(payload["limit"])
    elif kind == "paper_fill":
        state.setdefault("positions", []).append(payload)
        account["reserved"] = float(account.get("reserved") or 0.0) - float(payload.get("reserved") or 0.0)
        if account.get("cash") is not None:
            account["cash"] = float(account["cash"]) - float(payload["debit"])
    elif kind == "paper_exit":
        state.setdefault("exits", []).append(payload)
        state["positions"] = [row for row in state.get("positions") or [] if row.get("order_id") != payload.get("order_id")]
        if account.get("cash") is not None:
            account["cash"] = float(account["cash"]) + float(payload["proceeds"])
    elif kind == "paper_expiry":
        account["reserved"] = float(account.get("reserved") or 0.0) - float(payload.get("reserved") or 0.0)
    elif kind == "delivery":
        state.setdefault("deliveries", {})[payload["signal_id"]] = payload
    if len(state["events"]) > MAX_EVENTS:
        overflow = len(state["events"]) - MAX_EVENTS
        state["events"] = state["events"][-MAX_EVENTS:]
        state["events_dropped"] = int(state.get("events_dropped") or 0) + overflow
    return True


def view(state: dict) -> dict:
    """Pending means not filled, expired, or exited. An exit does not reopen the order."""
    orders = list(state.get("orders") or [])
    fills = [row for row in state.get("events") or [] if row.get("kind") == "paper_fill"]
    expiries = [row for row in state.get("events") or [] if row.get("kind") == "paper_expiry"]
    exits = list(state.get("exits") or [])
    closed = {row.get("payload", row).get("order_id") for row in fills + expiries}
    closed |= {row.get("order_id") for row in exits}
    pending = []
    for row in orders:
        if row.get("order_id") not in closed:
            pending.append(row)
    open_positions = [row for row in state.get("positions") or [] if row.get("order_id") not in {item.get("order_id") for item in exits}]
    account = state.get("account") or empty_state()["account"]
    return {"orders": len(orders), "pending": pending, "open_positions": open_positions, "exits": len(exits), "account": account}


class S3PaperBook:
    def __init__(self, client, bucket: str, key: str = "research/paper_book.json"):
        self.client = client
        self.bucket = bucket
        self.key = key

    def load(self) -> tuple[dict, str | None]:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"} or type(exc).__name__ in {"NoSuchKey", "FileNotFoundError"}:
                return empty_state(), None
            raise
        raw = obj["Body"].read()
        text = raw.decode() if isinstance(raw, bytes) else raw
        return json.loads(text), obj.get("ETag")

    def commit(self, state: dict, etag: str | None, event: dict) -> dict:
        fresh = json.loads(json.dumps(state))
        if not apply_event(fresh, event):
            return {"duplicate": True, "state": state, "etag": etag}
        kwargs = {
            "Bucket": self.bucket,
            "Key": self.key,
            "Body": json.dumps(fresh, sort_keys=True, default=str).encode(),
            "ContentType": "application/json",
        }
        if etag:
            kwargs["IfMatch"] = etag
        else:
            kwargs["IfNoneMatch"] = "*"
        try:
            response = self.client.put_object(**kwargs)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"} or type(exc).__name__ == "Conflict":
                raise LedgerConflict(code or type(exc).__name__) from exc
            raise
        return {"duplicate": False, "state": fresh, "etag": response.get("ETag")}
