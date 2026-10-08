"""Validate a research candidate. This never writes the production model key.

python -m backtest.export --run backtest/artifacts/v3 --candidate backtest/artifacts/v3/model.candidate.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swing_core.features import FEATURE_NAMES_V3


def validate_candidate(payload: dict) -> list[str]:
    errors = []
    if payload.get("feature_names") != FEATURE_NAMES_V3:
        errors.append("feature_names_mismatch")
    if payload.get("schema_id") != "features_v3_20":
        errors.append("schema_id")
    if payload.get("promoted") is not False:
        errors.append("promoted_flag_must_stay_false")
    if payload.get("allowed_live_families"):
        errors.append("live_families_must_be_empty_until_a_separate_promotion")
    if payload.get("base_score") in (None, ""):
        errors.append("missing_base_score")
    if not payload.get("trees"):
        errors.append("missing_trees")
    logistic = payload.get("logistic") or {}
    if "intercept" not in logistic or "coefficients" not in logistic:
        errors.append("missing_logistic")
    if payload.get("training_cutoff") in (None, ""):
        errors.append("missing_training_cutoff")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    src = Path(args.candidate)
    if not src.is_absolute():
        src = ROOT / src
    if src.name == "model.json" or "models/model.json" in str(src):
        print("Refusing to write a production model path.")
        return 2
    payload = json.loads(src.read_text())
    errors = validate_candidate(payload)
    run = Path(args.run)
    if not run.is_absolute():
        run = ROOT / run
    report = {"valid": not errors, "errors": errors, "destination": str(src), "production_write": False}
    (run / "export_report.json").write_text(json.dumps(report, indent=2) + "\n")
    if errors:
        print(json.dumps(report, indent=2))
        return 2
    frozen = run / "model.candidate.json"
    if src.resolve() != frozen.resolve():
        shutil.copyfile(src, frozen)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
