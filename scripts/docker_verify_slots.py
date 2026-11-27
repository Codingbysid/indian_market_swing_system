#!/usr/bin/env python3
"""Verify all 5 IST slots inside the Lambda container image."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.aws.engine import clear_artifact_caches
from src.aws.lambda_handler import lambda_handler

os.environ.setdefault("ENGINE_DRY_RUN", "1")
os.environ.setdefault("QUOTE_MODE", "eod")

SLOTS = [
    {"job_type": "PRE_MARKET", "slot_name": "09:05_IST", "dry_run": True, "ignore_holiday": True, "suppress_alerts": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "09:45_IST", "dry_run": True, "ignore_holiday": True, "suppress_alerts": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "12:45_IST", "dry_run": True, "ignore_holiday": True, "suppress_alerts": True},
    {"job_type": "SIGNAL_RUN", "slot_name": "15:10_IST", "dry_run": True, "ignore_holiday": True, "suppress_alerts": True},
    {"job_type": "POST_MARKET", "slot_name": "16:05_IST", "dry_run": True, "ignore_holiday": True, "suppress_alerts": True},
]


def main() -> None:
    out = Path(os.environ.get("VERIFY_OUT", "/out"))
    out.mkdir(parents=True, exist_ok=True)
    for event in SLOTS:
        clear_artifact_caches()
        name = event.get("slot_name") or event["job_type"]
        result = lambda_handler(event, None)
        (out / f"{name}.json").write_text(json.dumps(result, indent=2, default=str))
        html = result.get("email_preview_html") or ""
        if html:
            (out / f"{name}.html").write_text(html)
        res = result["result"]
        print(
            f"{name}: ok={result['ok']} status={res.get('status')} "
            f"regime={res.get('regime_state') or res.get('updated_regime_state')} "
            f"buys={len(res.get('BUY') or [])} sells={len(res.get('SELL') or [])} "
            f"stale={res.get('stale_quotes')} html={bool(html)}",
            flush=True,
        )
    print("DONE_DOCKER_SLOTS", flush=True)


if __name__ == "__main__":
    main()
