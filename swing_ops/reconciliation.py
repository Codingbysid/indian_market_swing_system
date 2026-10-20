"""Broker reconciliation is separate from the hypothetical paper account."""

from __future__ import annotations

REQUIRED_RECORDS = (
    "INFY sale: broker trade id, date, quantity and price",
    "ZENTEC fills: broker trade ids that reconcile quantity and average cost",
    "current settled cash",
    "pending orders and GTT ids",
    "tranche fill history for each legacy lot",
)


def status_for(state: dict | None) -> dict:
    state = state or {}
    record = state.get("broker_reconciliation") or {}
    if record.get("status") == "reconciled" and state.get("broker_reconciled_at"):
        return {"status": "reconciled", "gaps": [], "book_updated": state.get("updated")}
    return {
        "status": "unreconciled",
        "gaps": list(REQUIRED_RECORDS),
        "book_updated": state.get("updated"),
        "s3_quantities_are_not_broker_confirmed": True,
    }


def guard_monitor(payload: dict, status: dict) -> dict:
    """Informational levels may remain. Executable sell quantities may not."""
    payload = dict(payload)
    payload["reconciliation_status"] = status["status"]
    payload["reconciliation_gaps"] = status.get("gaps") or []
    if status["status"] == "reconciled":
        payload["all_clear"] = not payload.get("SELL") and not payload.get("events")
        return payload
    warnings = ["Broker quantities are unverified. This is not an all-clear and it is not an executable sell list."]
    for row in payload.get("holdings") or []:
        row["quantity_status"] = "unverified"
        if row.get("classification") == "system_atr" and row.get("stop") is not None and row.get("ltp") is not None and float(row["ltp"]) <= float(row["stop"]):
            warnings.append(
                f"PROTECTIVE WARNING {row.get('symbol')}: last price is at or through the recorded stop. Quantity is unverified; do not treat this as an order."
            )
        for intent in row.get("actionable_intents") or []:
            warnings.append(f"Suppressed {row.get('symbol')} {intent.get('kind')}: unverified quantity was not turned into a sell.")
        row["actionable_intents"] = []
    payload["events"] = warnings
    payload["SELL"] = []
    payload["all_clear"] = False
    return payload


def broker_record_event(record: dict) -> dict:
    required = ("broker_trade_id", "symbol", "side", "qty", "price", "trade_date")
    missing = [key for key in required if record.get(key) in (None, "")]
    if missing:
        raise ValueError("broker record missing " + ",".join(missing))
    from swing_ops.ledger import event_id
    return {
        "event_id": event_id({"broker_trade_id": record["broker_trade_id"]}),
        "kind": "broker_fill",
        "payload": dict(record),
    }
