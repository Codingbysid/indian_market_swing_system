"""Optimistic S3 concurrency: marks/alerts never overwrite a newer fill ledger."""

from __future__ import annotations
import json
from .state import validate_state, StaleWrite


def read_state(client, bucket, key):
    obj = client.get_object(Bucket=bucket, Key=key)
    return validate_state(json.loads(obj["Body"].read())), obj["ETag"]


def write_state(client, bucket, key, state, etag):
    if not etag:
        raise StaleWrite("existing state ETag required; bootstrap is separate")
    payload = {k: v for k, v in validate_state(state).items() if not k.startswith("_")}
    try:
        return client.put_object(
            Bucket=bucket,
            Key=key,
            IfMatch=etag,
            Body=json.dumps(payload, allow_nan=False).encode(),
            ContentType="application/json",
        )
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            raise StaleWrite("portfolio changed; reload before retry") from exc
        raise
