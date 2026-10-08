"""Fail-closed probability helpers. None/NaN must never admit a BUY."""

from __future__ import annotations

import math

import numpy as np


def output_policy(event: dict | None) -> dict:
    event = event or {}
    dry = bool(event.get("dry_run") or event.get("suppress_alerts"))
    if dry:
        return {"emit_alerts": False, "write_state": False, "destination": "null"}
    return {"emit_alerts": True, "write_state": True, "destination": "s3"}


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _parse_base_score(raw):
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if not text:
        return None
    try:
        value = float(text.split(",")[0].strip())
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _walk(node: dict, fmap: dict) -> float | None:
    if "leaf" in node:
        return float(node["leaf"])
    fname = node.get("split")
    if fname not in fmap:
        return None
    fval = np.float32(fmap[fname])
    thresh = np.float32(node["split_condition"])
    children = {c["nodeid"]: c for c in node.get("children") or []}
    if fval != fval:
        nid = int(node.get("missing", node["yes"]))
    elif fval < thresh:
        nid = int(node["yes"])
    else:
        nid = int(node["no"])
    child = children.get(nid)
    if child is None:
        return None
    return _walk(child, fmap)


def score_dump(model: dict | None, features: dict) -> float | None:
    """Pure-Python float32 walker. An empty base_score is an error, not 0.5."""
    if not isinstance(model, dict):
        return None
    names = list(model.get("feature_names") or [])
    fmap = {}
    for name in names:
        if name not in features:
            return None
        try:
            value = float(features[name])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        fmap[name] = value
    base = _parse_base_score(model.get("base_score"))
    if base is None:
        return None
    trees = model.get("trees")
    if not isinstance(trees, list) or not trees:
        return None
    margin = math.log(base / (1.0 - base)) if 0.0 < base < 1.0 else base
    total = margin
    for tree in trees:
        leaf = _walk(tree, fmap)
        if leaf is None or not math.isfinite(leaf):
            return None
        total += leaf
    probability = _sigmoid(total)
    return probability if math.isfinite(probability) else None


def finite_probability(value) -> float | None:
    if value is None:
        return None
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or p < 0.0 or p > 1.0:
        return None
    return p


def admit_buy(p, min_prob: float) -> tuple[bool, str]:
    scored = finite_probability(p)
    if scored is None:
        return False, "fail_closed_invalid_probability"
    if scored < float(min_prob):
        return False, f"p={scored:.3f}<min={float(min_prob):.2f}"
    return True, "ok"
