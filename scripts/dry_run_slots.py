#!/usr/bin/env python3
"""Local dry-run of all 5 IST slots via src.aws.lambda_handler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.aws.lambda_handler import handle

SLOTS = [
    {"job_type": "PRE_MARKET", "dry_run": True, "suppress_alerts": True, "ignore_holiday": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "SLOT_1", "slot_ist": "09:45", "dry_run": True, "suppress_alerts": True, "ignore_holiday": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "SLOT_2", "slot_ist": "12:45", "dry_run": True, "suppress_alerts": True, "ignore_holiday": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "SLOT_3_PRE_CAS", "slot_ist": "15:10", "dry_run": True, "suppress_alerts": True, "ignore_holiday": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "SLOT_3", "slot_ist": "15:30", "dry_run": True, "suppress_alerts": True, "ignore_holiday": True},
    {"job_type": "POST_MARKET", "dry_run": True, "suppress_alerts": True, "ignore_holiday": True},
]


def main():
    out_dir = ROOT / "reports" / "slot_dry_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    for event in SLOTS:
        result = handle(event, None)
        name = event.get("slot_name") or event["job_type"]
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        html = result.get("email_preview_html") or ""
        if html:
            (out_dir / f"{name}.html").write_text(html, encoding="utf-8")
        print(f"{name}: status={result['result'].get('status')} email_preview={bool(html)}")


if __name__ == "__main__":
    main()
