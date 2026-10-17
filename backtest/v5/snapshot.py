"""Record a screen snapshot. Historical membership is not inferred from a later page."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--screen", default="")
    args = parser.parse_args(argv)
    health = ROOT / "artifacts" / "v5" / "provider_health.json"
    screens = json.loads(health.read_text())["screens"] if health.exists() else {}
    payload = {
        "as_of": args.as_of,
        "status": "forward_only",
        "reason": "no_archived_point_in_time_screen_snapshot",
        "observed_pages": {name: {"status": row.get("status"), "complete": row.get("complete"), "retrieved_at": args.as_of} for name, row in screens.items()},
        "usable_before_retrieval": False,
    }
    out = ROOT / "artifacts" / "v5" / f"snapshot_{args.as_of}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"status": payload["status"], "out": str(out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
