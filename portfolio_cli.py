"""Record confirmed broker fills against v2 S3 portfolio state.

Usage: show | set-cash AMOUNT | fill path/to/fill.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3

from src.aws.state import apply_fill, read_state, validate_state, write_state
from src.config import S3Paths

REGION = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
BUCKET = os.getenv("BUCKET_NAME", S3Paths().default_bucket)
KEY = os.getenv("PORTFOLIO_STATE_KEY", S3Paths().state_key)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    c = sub.add_parser("set-cash")
    c.add_argument("amount", type=float)
    f = sub.add_parser("fill")
    f.add_argument("json_file")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    client = boto3.client("s3", region_name=REGION)
    state, etag = read_state(client, BUCKET, KEY)
    if args.command == "show":
        print(json.dumps(state, indent=2))
        return 0
    if args.command == "set-cash":
        state = validate_state({**state, "cash": args.amount})
    else:
        fill = json.loads(Path(args.json_file).read_text())
        if "available_cash_after" not in fill and not args.dry_run:
            raise SystemExit(
                "Broker lock: available_cash_after from Zerodha is required to save a fill. "
                "--dry-run may preview arithmetic only."
            )
        state = apply_fill(state, fill)
        if "available_cash_after" in fill:
            state = validate_state({**state, "cash": fill["available_cash_after"]})
            state["broker_reconciled_at"] = datetime.now(timezone.utc).isoformat()
    state["updated"] = datetime.now(timezone.utc).isoformat()
    if args.command == "set-cash":
        state["broker_reconciled_at"] = state["updated"]
    if args.dry_run:
        print(json.dumps({"dry_run": True, "proposed_state": state}, indent=2))
        return 0
    write_state(client, BUCKET, KEY, state, etag)
    print(f"Confirmed state saved to s3://{BUCKET}/{KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
