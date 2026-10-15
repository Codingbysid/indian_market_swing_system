"""Record confirmed broker fills with fees and an idempotent broker fill ID.
No alerts are fills. Missing S3 state does not bootstrap from a local screenshot.
Usage: show | set-cash AMOUNT | fill path/to/fill.json
BUY fill: fill_id,symbol,side,qty,price,fees,entry_date,entry_atr,stop,target,setup.
SELL fill: fill_id,symbol,side,qty,price,fees,stage(t1/t2/runner/stop/target/vertical).
"""

from __future__ import annotations
import argparse, json, os
from datetime import datetime, timezone
from pathlib import Path
import boto3
from swing_core.s3_state import read_state, write_state
from swing_core.state import apply_fill, validate_state

REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
BUCKET = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
KEY = os.getenv("PORTFOLIO_STATE_KEY", "portfolio_state.json")


def main():
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
                "Broker lock: available_cash_after from Zerodha is required to save a fill. --dry-run may preview arithmetic only."
            )
        state = apply_fill(state, fill)
        if "available_cash_after" in fill:
            state = validate_state({**state, "cash": fill["available_cash_after"]})
            state["broker_reconciled_at"] = datetime.now(timezone.utc).isoformat()
    state["updated"] = datetime.now(timezone.utc).isoformat()
    # Snapshot age follows an explicit broker reconciliation, not an MTM update.
    if args.command == "set-cash":
        state["broker_reconciled_at"] = state["updated"]
    if args.dry_run:
        print(json.dumps({"dry_run": True, "proposed_state": state}, indent=2))
        return 0
    write_state(client, BUCKET, KEY, state, etag)
    print("Confirmed state saved with an S3 conditional write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
