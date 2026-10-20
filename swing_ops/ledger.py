"""Durable paper book. Conditional writes reject a stale or colliding update."""

from __future__ import annotations

import hashlib
import json


class LedgerConflict(Exception):
    pass


def empty_state() -> dict:
    return {"events": [], "orders": [], "positions": [], "exits": [], "opening_cash": None}


def event_id(parts: dict) -> str:
    body = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:24]


def apply_event(state: dict, event: dict) -> bool:
    if any(row.get("event_id") == event["event_id"] for row in state.get("events") or []):
        return False
    state.setdefault("events", []).append(event)
    kind = event.get("kind")
    payload = event.get("payload") or {}
    if kind == "paper_order":
        state.setdefault("orders", []).append(payload)
    elif kind == "paper_fill":
        state.setdefault("positions", []).append(payload)
    elif kind == "paper_exit":
        state.setdefault("exits", []).append(payload)
        state["positions"] = [row for row in state.get("positions") or [] if row.get("order_id") != payload.get("order_id")]
    elif kind == "paper_expiry":
        state["orders"] = [row for row in state.get("orders") or [] if row.get("order_id") != payload.get("order_id")]
    return True


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
