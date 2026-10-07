"""Read-only snapshot of deployed Lambda/S3/EventBridge. Does not overwrite state."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
FUNCTION_NAME = os.getenv("LAMBDA_FUNCTION_NAME", "indian-swing-bot")
BUCKET = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
OUT = Path(__file__).resolve().parents[1] / "docs" / "deployment_snapshot.json"

SAFE_ENV_KEYS = (
    "CORE_CAPITAL",
    "SATELLITE_CAPITAL",
    "CORE_MIN_PROB",
    "SATELLITE_MIN_PROB",
    "CORE_KELLY_FRACTION",
    "SATELLITE_KELLY_FRACTION",
    "CORE_MAX_POSITIONS",
    "SATELLITE_MAX_POSITIONS",
    "BUCKET_NAME",
    "MODEL_S3_KEY",
    "PORTFOLIO_STATE_KEY",
    "NTFY_TOPIC",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    lam = boto3.client("lambda", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    events = boto3.client("events", region_name=REGION)

    cfg = lam.get_function_configuration(FunctionName=FUNCTION_NAME)
    env = (cfg.get("Environment") or {}).get("Variables") or {}
    safe_env = {k: env.get(k) for k in SAFE_ENV_KEYS if k in env}

    model_key = env.get("MODEL_S3_KEY", "models/model.json")
    state_key = env.get("PORTFOLIO_STATE_KEY", "portfolio_state.json")
    objects = {}
    for key in (model_key, state_key, "holdings_levels.json", "equity_curve.json"):
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=key)
            body = obj["Body"].read()
            objects[key] = {
                "bytes": len(body),
                "sha256": _sha256_bytes(body),
                "last_modified": str(obj.get("LastModified")),
            }
        except Exception as exc:
            objects[key] = {"error": str(exc.__class__.__name__)}

    rules = []
    for name in ("swing-recommender-daily", "swing-holdings-monitor"):
        try:
            r = events.describe_rule(Name=name)
            tgt = events.list_targets_by_rule(Rule=name).get("Targets", [])
            rules.append(
                {
                    "name": name,
                    "state": r.get("State"),
                    "schedule": r.get("ScheduleExpression"),
                    "input": [t.get("Input") for t in tgt],
                }
            )
        except Exception as exc:
            rules.append({"name": name, "error": str(exc)})

    snapshot = {
        "captured_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "function": {
            "name": FUNCTION_NAME,
            "runtime": cfg.get("Runtime"),
            "code_sha256": cfg.get("CodeSha256"),
            "last_modified": cfg.get("LastModified"),
            "timeout": cfg.get("Timeout"),
            "memory": cfg.get("MemorySize"),
            "env": safe_env,
        },
        "s3_objects": objects,
        "eventbridge": rules,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(snapshot, indent=2))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
