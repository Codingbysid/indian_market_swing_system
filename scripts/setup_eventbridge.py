"""Create/enable weekday EventBridge rules for indian-swing-bot (eu-north-1).

  swing-recommender-daily  cron(55 3 ? * MON-FRI *)  -> 09:25 IST  action=recommender
  swing-holdings-monitor   cron(35 9 ? * MON-FRI *)  -> 15:05 IST  action=monitor
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
        "Name": "swing-recommender-daily",
        "ScheduleExpression": "cron(55 3 ? * MON-FRI *)",
        "Description": "NSE pre-open swing scan at 09:25 IST",
        "Input": {"action": "recommender"},
        "StatementId": "EventBridgeRecommendDaily",
    },
    {
        "Name": "swing-holdings-monitor",
        "ScheduleExpression": "cron(35 9 ? * MON-FRI *)",
        "Description": "Holdings profit-exit watch at 15:05 IST",
        "Input": {"action": "monitor"},
        "StatementId": "EventBridgeHoldingsMonitor",
    },
)


def main() -> None:
    events = boto3.client("events", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)

    fn = lam.get_function(FunctionName=FUNCTION_NAME)
    fn_arn = fn["Configuration"]["FunctionArn"]
    print(f"Lambda ARN: {fn_arn}")

    account = fn_arn.split(":")[4]

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
        source_arn = (
            f"arn:aws:events:{REGION}:{account}:rule/{name}"
        )
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

    print("EventBridge rules enabled.")


if __name__ == "__main__":
    main()
