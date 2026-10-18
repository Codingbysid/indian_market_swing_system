"""Append-only local paper log. It does not write S3 or send alerts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "artifacts" / "v5" / "forward" / "observations.jsonl"


def observe(as_of: str, screens: dict | None = None) -> dict:
    record = {
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of,
        "destination": "local_artifacts_v5_forward",
        "production_write": False,
        "screens": screens or {},
        "families": {
            "quality_continuation": {"status": "blocked", "reason": "no_point_in_time_snapshot"},
            "improving_value": {"status": "blocked", "reason": "no_point_in_time_snapshot"},
            "trend_transition_price_control": {"status": "not_a_forward_fundamental_observation"},
        },
        "missing_metadata": ["holiday_calendar", "corporate_actions", "restriction_status"],
        "orders": [],
        "frozen_before_outcome": True,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)
    health = ROOT / "artifacts" / "v5" / "provider_health.json"
    screens = {}
    if health.exists():
        raw = json.loads(health.read_text())
        for name, row in (raw.get("screens") or {}).items():
            screens[name] = {
                "status": row.get("status"),
                "complete": row.get("complete"),
                "results_found": row.get("results_found"),
                "first_observed": args.as_of,
                "historical_backfill": False,
            }
    record = observe(args.as_of, screens)
    print(json.dumps({"appended": str(LOG), "production_write": record["production_write"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
