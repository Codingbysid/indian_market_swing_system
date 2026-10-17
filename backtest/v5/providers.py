"""Probe providers. Failures stay failures; an empty screen is a different status."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from swing_v5.data.aws_inventory import inventory
from swing_v5.data.nse import CANDIDATE_URLS, probe_url
from swing_v5.data.screener import SCREENS, collect_screen

ROOT = Path(__file__).resolve().parents[2]


def build() -> dict:
    started = time.perf_counter()
    screens = {}
    for name, url in SCREENS.items():
        t0 = time.perf_counter()
        row = collect_screen(url)
        row["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
        screens[name] = row
    nse = [probe_url(url) for url in CANDIDATE_URLS]
    return {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "screener_has_no_api": True,
        "screens": screens,
        "nse": nse,
        "tradingview": {"status": "not_probed_from_this_process", "live_fallback": "prohibited"},
        "yahoo_live_fallback": False,
        "aws": inventory(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "homepage_200_is_not_health": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--out", default="artifacts/v5/provider_health.json")
    args = parser.parse_args(argv)
    payload = build()
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"out": str(out), "screens": {k: v.get("status") for k, v in payload["screens"].items()}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
