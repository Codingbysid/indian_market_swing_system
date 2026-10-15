"""Unique actionable intents. WATCH, target touches and reminders never count."""

from __future__ import annotations
import hashlib, json


def record_once(client, bucket, intent, *, observed_at):
    if intent.get("side") not in ("BUY", "SELL") or not intent.get("signal_id"):
        raise ValueError("typed actionable intent with signal_id required")
    digest = hashlib.sha256(intent["signal_id"].encode()).hexdigest()
    key = f"signal_journal/v4/{digest}.json"
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            IfNoneMatch="*",
            ContentType="application/json",
            Body=json.dumps(
                {**intent, "observed_at": observed_at, "fill_status": "unconfirmed"},
                allow_nan=False,
            ).encode(),
        )
        return True
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            return False
        raise
