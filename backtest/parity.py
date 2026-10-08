"""Native XGBoost versus the pure-Python float32 walker."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from swing_core.features import FEATURE_NAMES_V3
from swing_core.scoring import score_dump


def parity(candidate_path: Path, n: int = 40) -> dict:
    payload = json.loads(Path(candidate_path).read_text())
    from xgboost import XGBClassifier

    rng = np.random.default_rng(11)
    rows = rng.normal(size=(n, len(FEATURE_NAMES_V3)))
    frame = pd.DataFrame(rows, columns=FEATURE_NAMES_V3)
    model = XGBClassifier()
    model.load_model(str(candidate_path.with_suffix(".xgb.json"))) if False else None
    # Rebuild is not possible from dump JSON alone. Compare walker against a
    # booster saved beside the candidate when train exported one.
    booster_path = candidate_path.with_name("model.candidate.xgb.json")
    if not booster_path.exists():
        return {"compared": False, "reason": "native booster file missing"}
    import xgboost as xgb

    booster = xgb.Booster()
    booster.load_model(str(booster_path))
    dmat = xgb.DMatrix(frame, feature_names=FEATURE_NAMES_V3)
    native = booster.predict(dmat)
    walker = []
    for _, row in frame.iterrows():
        walker.append(score_dump(payload, row.to_dict()))
    walker = np.array(walker, dtype=float)
    if np.isnan(walker).any():
        return {"compared": True, "ok": False, "reason": "walker returned none"}
    gap = float(np.max(np.abs(native - walker)))
    # Float32 boundary: move the first split's feature to the threshold and one ulp below.
    tree = payload["trees"][0]
    boundary = _boundary_gap(payload, booster, tree)
    return {"compared": True, "ok": gap < 1e-5 and boundary["ok"], "max_abs_gap": gap, "boundary": boundary}


def _boundary_gap(payload, booster, node) -> dict:
    import xgboost as xgb

    if "leaf" in node:
        return {"ok": True, "note": "first node is a leaf"}
    name = node["split"]
    thresh = np.float32(node["split_condition"])
    below = np.nextafter(thresh, np.float32(-np.inf))
    row = {feature: 0.0 for feature in FEATURE_NAMES_V3}
    gaps = []
    for value in (below, thresh):
        row[name] = float(value)
        native = float(booster.predict(xgb.DMatrix(pd.DataFrame([row]), feature_names=FEATURE_NAMES_V3))[0])
        walked = score_dump(payload, row)
        gaps.append(abs(native - walked))
    missing = score_dump(payload, {feature: 0.0 for feature in FEATURE_NAMES_V3 if feature != name})
    return {"ok": max(gaps) < 1e-5 and missing is None, "max_abs_gap": float(max(gaps)), "missing_feature_rejected": missing is None}
