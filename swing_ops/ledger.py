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


def migration_status(state: dict) -> dict:
    """A compacted book without durable status cannot be reconstructed."""
    if state.get("order_status") is not None and state.get("applied_ids") is not None:
        return {"status": "current"}
    if int(state.get("events_dropped") or 0) > 0:
        return {"status": "ambiguous", "reason": "compacted history has no durable order status"}
    return {"status": "derivable"}


def _materialize_status(state: dict) -> None:
    status = {}
    applied = []
    for event in state.get("events") or []:
        applied.append(event["event_id"])
        payload = event.get("payload") or {}
        order = payload.get("order_id")
        kind = event.get("kind")
        if kind == "paper_order" and order:
            status[order] = "pending"
        elif kind == "paper_fill" and order:
            status[order] = "filled"
        elif kind == "paper_expiry" and order:
            status[order] = "expired"
        elif kind == "paper_exit" and order:
            status[order] = "exited"
    state["order_status"] = status
    state["applied_ids"] = applied


def apply_event(state: dict, event: dict) -> bool:
    report = migration_status(state)
    if report["status"] == "ambiguous":
        state["migration"] = report
        return False
    if report["status"] == "derivable":
        _materialize_status(state)
    state.setdefault("applied_ids", [])
    if event["event_id"] in set(state["applied_ids"]):
        return False
    state["applied_ids"].append(event["event_id"])
    state.setdefault("events", []).append(event)
    state.setdefault("order_status", {})
    kind = event.get("kind")
    payload = event.get("payload") or {}
    account = state.setdefault("account", empty_state()["account"])
    if kind == "paper_account_init":
        if not account.get("initialized"):
            cash = float(payload["cash"])
            account.update({
                "initialized": True,
                "cash": cash,
                "label": payload.get("label", "hypothetical_research_account"),
                "reserved": 0.0,
                "launch_nav": cash,
                "sleeve_nav": cash,
                "external_inflows": 0.0,
                "realized_pnl": 0.0,
                "circuit_latched": False,
                "open_stressed": 0.0,
                "reserved_stressed": 0.0,
                "assumption_note": payload.get("assumption_note"),
            })
    elif kind == "paper_order":
        state.setdefault("orders", []).append(payload)
        state["order_status"][payload["order_id"]] = "pending"
        reserve = float(payload.get("reserved") or 0.0)
        account["reserved"] = float(account.get("reserved") or 0.0) + reserve
        account["reserved_stressed"] = float(account.get("reserved_stressed") or 0.0) + float(payload.get("stressed_risk") or 0.0)
    elif kind == "paper_fill":
        state.setdefault("positions", []).append(payload)
        state["order_status"][payload["order_id"]] = "filled"
        account["reserved"] = float(account.get("reserved") or 0.0) - float(payload.get("reserved") or 0.0)
        account["reserved_stressed"] = float(account.get("reserved_stressed") or 0.0) - float(payload.get("stressed_risk") or 0.0)
        account["open_stressed"] = float(account.get("open_stressed") or 0.0) + float(payload.get("stressed_risk") or 0.0)
        if account.get("cash") is not None:
            account["cash"] = float(account["cash"]) - float(payload["debit"])
    elif kind == "paper_exit":
        state.setdefault("exits", []).append(payload)
        state["order_status"][payload["order_id"]] = "exited"
        state["positions"] = [row for row in state.get("positions") or [] if row.get("order_id") != payload.get("order_id")]
        account["open_stressed"] = max(0.0, float(account.get("open_stressed") or 0.0) - float(payload.get("stressed_risk") or 0.0))
        if account.get("cash") is not None:
            account["cash"] = float(account["cash"]) + float(payload["proceeds"])
            debit = float(payload.get("debit") or 0.0)
            account["realized_pnl"] = float(account.get("realized_pnl") or 0.0) + float(payload["proceeds"]) - debit
            account["sleeve_nav"] = float(account.get("launch_nav") or 0.0) + float(account["realized_pnl"])
            pause = 1500.0
            if float(account["sleeve_nav"]) - float(account.get("launch_nav") or 0.0) - float(account.get("external_inflows") or 0.0) <= -pause:
                account["circuit_latched"] = True
    elif kind == "paper_expiry":
        state["order_status"][payload["order_id"]] = "expired"
        account["reserved"] = float(account.get("reserved") or 0.0) - float(payload.get("reserved") or 0.0)
        account["reserved_stressed"] = float(account.get("reserved_stressed") or 0.0) - float(payload.get("stressed_risk") or 0.0)
    elif kind == "delivery":
        state.setdefault("deliveries", {})[payload["signal_id"]] = payload
    if len(state["events"]) > MAX_EVENTS:
        overflow = state["events"][:-MAX_EVENTS]
        state.setdefault("pending_archive", []).extend(overflow)
        state["events"] = state["events"][-MAX_EVENTS:]
        state["events_dropped"] = int(state.get("events_dropped") or 0) + len(overflow)
    return True


def view(state: dict) -> dict:
    """Pending comes from durable status, not from the retained event window."""
    if migration_status(state)["status"] == "derivable":
        _materialize_status(state)
    report = migration_status(state)
    orders = list(state.get("orders") or [])
    status = state.get("order_status") or {}
    if report["status"] == "ambiguous":
        return {
            "orders": len(orders), "pending": [], "open_positions": [], "exits": len(state.get("exits") or []),
            "account": state.get("account") or empty_state()["account"], "migration": report,
        }
    pending = [row for row in orders if status.get(row.get("order_id")) == "pending"]
    open_positions = [row for row in state.get("positions") or [] if status.get(row.get("order_id")) == "filled"]
    return {
        "orders": len(orders),
        "pending": pending,
        "open_positions": open_positions,
        "exits": len(state.get("exits") or []),
        "account": state.get("account") or empty_state()["account"],
        "migration": report,
    }


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
        archive = fresh.get("pending_archive") or []
        if archive:
            seq = int(fresh.get("audit_seq") or 0) + 1
            audit_key = f"{self.key}.audit.{seq}.json"
            self.client.put_object(Bucket=self.bucket, Key=audit_key, Body=json.dumps(archive).encode(), ContentType="application/json")
            fresh["audit_seq"] = seq
            fresh["pending_archive"] = []
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
