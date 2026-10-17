"""Write a shadow candidate only. Production model keys are refused."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from swing_v5.features import FEATURES_PRICE, schema_hash

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--out", default="artifacts/v5/model.candidate.json")
    args = parser.parse_args(argv)
    destination = ROOT / args.out
    if destination.name == "model.json" or "models/model.json" in str(destination):
        print("Refusing a production model path.")
        return 2
    metrics = ROOT / "artifacts" / "v5" / "metrics.json"
    payload = {
        "type": "unpromoted_rules_reference",
        "schema_id": "v5_price_only",
        "feature_names": FEATURES_PRICE,
        "feature_order_hash": schema_hash(FEATURES_PRICE),
        "promoted": False,
        "allowed_live_families": [],
        "status": "research",
        "metrics_ref": str(metrics) if metrics.exists() else None,
        "reason": "No horizon passed promotion gates. This file must not replace models/model.json.",
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"promoted": False, "out": str(destination)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
