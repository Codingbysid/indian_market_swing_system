"""Collect Screener pages into a local archive. This does not write production state."""

from __future__ import annotations

import json
from pathlib import Path

from swing_v5.data.screener import SCREENS, collect_screen
from swing_v5.forward_log import now_iso

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    archive = ROOT / "artifacts" / "v5" / "forward" / "screens"
    retrieved = now_iso()
    payload = {"retrieved_at": retrieved, "screens": {}}
    for name, url in SCREENS.items():
        payload["screens"][name] = collect_screen(url, archive_dir=archive / name)
    out = ROOT / "artifacts" / "v5" / "forward" / "screens.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({name: row.get("status") for name, row in payload["screens"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
