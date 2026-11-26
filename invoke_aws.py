"""Trigger AWS Lambda (indian-swing-bot) remotely with tail logs and error handling.

Usage:
  python invoke_aws.py PRE_MARKET [--force] [--dry-run]
  python invoke_aws.py SIGNAL_RUN [--slot 1|2|3] [--force] [--dry-run]
  python invoke_aws.py POST_MARKET [--force] [--dry-run]

Flags:
  --force / --ignore-weekend : Bypass exchange weekend & holiday guards (verification only)
  --dry-run / --suppress-alerts : Suppress live email/ntfy notification delivery and S3 writes
"""

import base64
import json
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import NoCredentialsError

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
FUNCTION_NAME = os.getenv("LAMBDA_FUNCTION_NAME", "indian-swing-bot")

args = [a for a in sys.argv[1:] if a]
action = "PRE_MARKET"
slot_name = "SLOT_1"
force = False
dry_run = False

i = 0
while i < len(args):
    arg = args[i]
    if arg in ("--force", "--ignore-weekend"):
        force = True
    elif arg in ("--dry-run", "--suppress-alerts"):
        dry_run = True
    elif arg in ("--slot", "-s") and i + 1 < len(args):
        i += 1
        val = args[i]
        slot_name = f"SLOT_{val}" if val.isdigit() else val.upper()
    elif not arg.startswith("-"):
        action = arg.upper()
    i += 1

# Map legacy action names if passed
if action in ("RECOMMENDER", "SCAN"):
    action = "SIGNAL_RUN"
    slot_name = "SLOT_1"
elif action in ("MONITOR", "EXIT"):
    action = "SIGNAL_RUN"
    slot_name = "SLOT_3"
elif action == "SCRAPER":
    action = "PRE_MARKET"

payload: dict = {"action": action}
if action == "SIGNAL_RUN":
    payload["slot_name"] = slot_name
if force:
    payload["ignore_weekend"] = True
if dry_run:
    payload["suppress_alerts"] = True
    payload["dry_run"] = True

client = boto3.client(
    "lambda",
    region_name=REGION,
    config=Config(read_timeout=360, connect_timeout=30, retries={"max_attempts": 0}),
)

print(f"🚀 Triggering AWS Lambda ({FUNCTION_NAME}) with payload: {payload}...\n")

try:
    response = client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        LogType="Tail",  # Captures the last 4KB of execution logs
        Payload=json.dumps(payload),
    )
except NoCredentialsError:
    print(
        "AWS credentials not found.\n"
        "Configure them first, then re-run:\n"
        "  1) aws configure   OR\n"
        "  2) export AWS_ACCESS_KEY_ID=...\n"
        "     export AWS_SECRET_ACCESS_KEY=...\n"
        "     export AWS_DEFAULT_REGION=eu-north-1"
    )
    sys.exit(1)

if response.get("FunctionError"):
    print(f"FunctionError: {response['FunctionError']}")
print(f"Status Code: {response['StatusCode']}")

# Print the exact execution log output (all print statements inside Lambda)
if "LogResult" in response:
    logs = base64.b64decode(response["LogResult"]).decode("utf-8", errors="ignore")
    print("\n" + "=" * 30 + " AWS LAMBDA EXECUTION LOGS " + "=" * 30)
    print(logs)
    print("=" * 87 + "\n")

# Print function response
response_payload = json.loads(response["Payload"].read().decode("utf-8"))
print(f"Lambda Output: {response_payload}")
if response.get("FunctionError"):
    sys.exit(1)
if isinstance(response_payload, dict) and int(response_payload.get("statusCode") or 200) >= 400:
    sys.exit(1)
