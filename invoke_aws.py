"""Trigger the v2 containerized Lambda (nse-quant-swing-engine-v2) remotely.

Usage:
  python invoke_aws.py PRE_MARKET [--force] [--dry-run]
  python invoke_aws.py SIGNAL_RUN [--slot 09:45_IST|1|2|3] [--force] [--dry-run]
  python invoke_aws.py POST_MARKET [--force] [--dry-run]

Flags:
  --force / --ignore-holiday : Bypass NSE holiday/weekend guards (verification only)
  --dry-run / --suppress-alerts : Suppress live email/ntfy delivery
"""

from __future__ import annotations

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

REGION = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")
FUNCTION_NAME = os.getenv("LAMBDA_FUNCTION_NAME", "nse-quant-swing-engine-v2")

SLOT_ALIASES = {
    "1": "09:45_IST",
    "2": "12:45_IST",
    "3": "15:10_IST",
    "SLOT_1": "09:45_IST",
    "SLOT_2": "12:45_IST",
    "SLOT_3": "15:10_IST",
    "SLOT_3_PRE_CAS": "15:10_IST",
}

args = [a for a in sys.argv[1:] if a]
job_type = "PRE_MARKET"
slot_name = "09:45_IST"
force = False
dry_run = False

i = 0
while i < len(args):
    arg = args[i]
    if arg in ("--force", "--ignore-weekend", "--ignore-holiday"):
        force = True
    elif arg in ("--dry-run", "--suppress-alerts"):
        dry_run = True
    elif arg in ("--slot", "-s") and i + 1 < len(args):
        i += 1
        raw = args[i].upper()
        slot_name = SLOT_ALIASES.get(raw, raw if raw.endswith("_IST") else f"{raw}_IST")
    elif not arg.startswith("-"):
        job_type = arg.upper()
    i += 1

# Legacy action aliases → typed job_type
if job_type in ("RECOMMENDER", "SCAN", "MONITOR", "EXIT"):
    if job_type in ("MONITOR", "EXIT"):
        slot_name = "15:10_IST"
    job_type = "SIGNAL_RUN"
elif job_type == "SCRAPER":
    job_type = "PRE_MARKET"
elif job_type == "EOD":
    job_type = "POST_MARKET"

payload: dict = {"job_type": job_type, "slot_name": slot_name}
if force:
    payload["ignore_holiday"] = True
if dry_run:
    payload["suppress_alerts"] = True
    payload["dry_run"] = True

client = boto3.client(
    "lambda",
    region_name=REGION,
    config=Config(read_timeout=360, connect_timeout=30, retries={"max_attempts": 0}),
)

print(f"Triggering {FUNCTION_NAME} ({REGION}) payload={payload}\n")

try:
    response = client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        LogType="Tail",
        Payload=json.dumps(payload),
    )
except NoCredentialsError:
    print(
        "AWS credentials not found.\n"
        "Configure them first, then re-run:\n"
        "  1) aws configure   OR\n"
        "  2) export AWS_ACCESS_KEY_ID=...\n"
        "     export AWS_SECRET_ACCESS_KEY=...\n"
        f"     export AWS_DEFAULT_REGION={REGION}"
    )
    sys.exit(1)

if response.get("FunctionError"):
    print(f"FunctionError: {response['FunctionError']}")
print(f"Status Code: {response['StatusCode']}")

if "LogResult" in response:
    logs = base64.b64decode(response["LogResult"]).decode("utf-8", errors="ignore")
    print("\n" + "=" * 30 + " AWS LAMBDA EXECUTION LOGS " + "=" * 30)
    print(logs)
    print("=" * 87 + "\n")

response_payload = json.loads(response["Payload"].read().decode("utf-8"))
print(f"Lambda Output: {response_payload}")
if response.get("FunctionError"):
    sys.exit(1)
