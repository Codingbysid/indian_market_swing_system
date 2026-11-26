"""Create/enable weekday EventBridge rules for indian-swing-bot (eu-north-1).

5-slot Mon-Fri IST schedule. Each target input is the EventBridge event dict.

  swing-pre-market      cron(35 3 ? * MON-FRI *)  -> 09:05 IST
  swing-signal-slot-1   cron(15 4 ? * MON-FRI *)  -> 09:45 IST
  swing-signal-slot-2   cron(15 7 ? * MON-FRI *)  -> 12:45 IST
  swing-signal-slot-3   cron(0 10 ? * MON-FRI *)  -> 15:30 IST
  swing-post-market     cron(35 10 ? * MON-FRI *) -> 16:05 IST
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
FUNCTION_NAME = os.getenv("LAMBDA_FUNCTION_NAME", "indian-swing-bot")

RULES = (
    {
        "Name": "swing-pre-market",
        "ScheduleExpression": "cron(35 3 ? * MON-FRI *)",
        "Description": "NSE pre-open system health & context at 09:05 IST",
        "Input": {"job_type": "PRE_MARKET", "slot_name": "09:05_IST", "dry_run": False},
        "StatementId": "EventBridgePreMarket",
    },
    {
        "Name": "swing-signal-slot-1",
        "ScheduleExpression": "cron(15 4 ? * MON-FRI *)",
        "Description": "Morning swing signal scan (Slot 1) at 09:45 IST",
        "Input": {"job_type": "SIGNAL_RUN", "slot_name": "09:45_IST", "dry_run": False},
        "StatementId": "EventBridgeSignalSlot1",
    },
    {
        "Name": "swing-signal-slot-2",
        "ScheduleExpression": "cron(15 7 ? * MON-FRI *)",
        "Description": "Midday swing signal scan (Slot 2) at 12:45 IST",
        "Input": {"job_type": "SIGNAL_RUN", "slot_name": "12:45_IST", "dry_run": False},
        "StatementId": "EventBridgeSignalSlot2",
    },
    {
        "Name": "swing-signal-slot-3",
        "ScheduleExpression": "cron(0 10 ? * MON-FRI *)",
        "Description": "Market close swing signal scan (Slot 3) at 15:30 IST",
        "Input": {"job_type": "SIGNAL_RUN", "slot_name": "15:30_IST", "dry_run": False},
        "StatementId": "EventBridgeSignalSlot3",
    },
    {
        "Name": "swing-post-market",
        "ScheduleExpression": "cron(35 10 ? * MON-FRI *)",
        "Description": "NSE post-close EOD reconciliation at 16:05 IST",
        "Input": {"job_type": "POST_MARKET", "slot_name": "16:05_IST", "dry_run": False},
        "StatementId": "EventBridgePostMarket",
    },
)

LEGACY_RULES = (
    "swing-recommender-daily",
    "swing-holdings-monitor",
)


def main(purge_legacy: bool = False) -> None:
    events = boto3.client("events", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)

    fn = lam.get_function(FunctionName=FUNCTION_NAME)
    fn_arn = fn["Configuration"]["FunctionArn"]
    print(f"Lambda ARN: {fn_arn}")

    account = fn_arn.split(":")[4]

    # Clean up legacy rules if requested
    if purge_legacy:
        for legacy_name in LEGACY_RULES:
            try:
                events.remove_targets(Rule=legacy_name, Ids=["indian-swing-bot"])
                events.delete_rule(Name=legacy_name)
                print(f"Purged legacy rule: {legacy_name}")
            except ClientError as exc:
                print(f"Legacy rule {legacy_name} not found or already deleted: {exc}")

    for spec in RULES:
        name = spec["Name"]
        resp = events.put_rule(
            Name=name,
            ScheduleExpression=spec["ScheduleExpression"],
            State="ENABLED",
            Description=spec["Description"],
        )
        rule_arn = resp["RuleArn"]
        print(f"Rule {name}: {rule_arn} ({spec['ScheduleExpression']})")

        events.put_targets(
            Rule=name,
            Targets=[
                {
                    "Id": "indian-swing-bot",
                    "Arn": fn_arn,
                    "Input": json.dumps(spec["Input"]),
                }
            ],
        )
        source_arn = f"arn:aws:events:{REGION}:{account}:rule/{name}"
        try:
            lam.add_permission(
                FunctionName=FUNCTION_NAME,
                StatementId=spec["StatementId"],
                Action="lambda:InvokeFunction",
                Principal="events.amazonaws.com",
                SourceArn=source_arn,
            )
            print(f"  invoke permission added ({spec['StatementId']})")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "ResourceConflictException":
                raise
            print(f"  invoke permission already present ({spec['StatementId']})")

    print("5-slot EventBridge rules enabled successfully.")


if __name__ == "__main__":
    main()
