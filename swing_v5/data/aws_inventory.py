"""Read-only AWS inventory. Secrets are never copied into the artifact."""

from __future__ import annotations

import os


def inventory() -> dict:
    try:
        from pathlib import Path

        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except ImportError:
        pass
    keys = [
        "BUCKET_NAME",
        "MODEL_S3_KEY",
        "PORTFOLIO_STATE_KEY",
        "NTFY_TOPIC",
        "AWS_DEFAULT_REGION",
    ]
    present = {key: bool(os.getenv(key)) for key in keys}
    present["aws_access_key_present"] = bool(os.getenv("AWS_ACCESS_KEY_ID"))
    record = {"credentials_present": present["aws_access_key_present"], "env_names_present": present, "deployed": None}
    if not present["aws_access_key_present"]:
        record["status"] = "blocked_no_credentials_in_process"
        record["note"] = "Deployment snapshot from 2026-10-07 remains the last recorded live hash; it was not re-read."
        return record
    try:
        import boto3

        client = boto3.client("lambda", region_name=os.getenv("AWS_DEFAULT_REGION", "eu-north-1"))
        function = client.get_function(FunctionName="indian-swing-bot")
        config = function["Configuration"]
        env = (config.get("Environment") or {}).get("Variables") or {}
        record.update(
            {
                "status": "read",
                "function": config.get("FunctionName"),
                "runtime": config.get("Runtime"),
                "timeout": config.get("Timeout"),
                "memory": config.get("MemorySize"),
                "code_sha256": config.get("CodeSha256"),
                "last_modified": config.get("LastModified"),
                "env_names": sorted(env.keys()),
            }
        )
    except Exception as exc:
        record["status"] = "read_failed"
        record["error"] = type(exc).__name__
    return record
