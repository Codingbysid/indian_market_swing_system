"""Float32 split contract. Equality takes the no-branch. Missing values do not score."""

from __future__ import annotations

import json

import numpy as np

from swing_v5.features import FEATURES_PRICE, schema_hash
from swing_v5.scorer import score


def synthetic_model() -> dict:
    names = FEATURES_PRICE
    threshold = 0.1
    return {
        "type": "xgboost_dump",
        "feature_names": names,
        "feature_order_hash": schema_hash(names),
        "promoted": False,
        "status": "research",
        "base_score": 0.5,
        "trees": [
            {
                "nodeid": 0,
                "split": "atr_pct",
                "split_condition": threshold,
                "yes": 1,
                "no": 2,
                "missing": 1,
                "children": [
                    {"nodeid": 1, "leaf": 0.2},
                    {"nodeid": 2, "leaf": -0.2},
                ],
            }
        ],
    }


def main() -> int:
    model = synthetic_model()
    row = {name: 0.0 for name in FEATURES_PRICE}
    threshold = np.float32(0.1)
    below = float(np.nextafter(threshold, np.float32(-np.inf)))
    row["atr_pct"] = below
    low = score(model, row)
    row["atr_pct"] = float(threshold)
    high = score(model, row)
    missing = score(model, {name: 0.0 for name in FEATURES_PRICE if name != "atr_pct"})
    empty = score({**model, "base_score": ""}, row)
    report = {
        "below_threshold_gt_half": low is not None and low > 0.5,
        "equal_threshold_lt_half": high is not None and high < 0.5,
        "missing_rejected": missing is None,
        "empty_base_score_rejected": empty is None,
        "promoted": False,
    }
    print(json.dumps(report, indent=2))
    return 0 if all(report[key] for key in report if key != "promoted") else 2


if __name__ == "__main__":
    raise SystemExit(main())
