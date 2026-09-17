import base64
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import NoCredentialsError

# Set your AWS region (e.g., 'eu-north-1' from your screenshots)
REGION = "eu-north-1"
FUNCTION_NAME = "indian-swing-bot"

# Change action to "scraper", "recommender", or "monitor"
# Or pass as CLI arg: python invoke_aws.py monitor [--force]
# --force bypasses the IST weekend gate (for verification only)
args = [a for a in sys.argv[1:] if a]
action = "recommender"
force = False
for a in args:
    if a in ("--force", "--ignore-weekend"):
        force = True
    elif not a.startswith("-"):
        action = a
payload = {"action": action}
if force:
    payload["ignore_weekend"] = True

# Recommender scans ~90 tickers with TV rate-limit sleeps; allow up to 6 minutes.
client = boto3.client(
    "lambda",
    region_name=REGION,
    config=Config(read_timeout=360, connect_timeout=30, retries={"max_attempts": 0}),
)

print(f"🚀 Triggering AWS Lambda ({FUNCTION_NAME}) with action: {payload['action']}...\n")

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
