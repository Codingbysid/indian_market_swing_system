"""Production BUY stays closed unless an approval file and the evidence floors both pass."""

from __future__ import annotations

import json
from pathlib import Path

from swing_core.engine_v4 import promotion_supported

ROOT = Path(__file__).resolve().parents[1]


def approved_families(path: Path | None = None) -> set[str]:
    source = path or (ROOT / "config" / "ops" / "promotion_approval.json")
    payload = json.loads(source.read_text())
    return set(payload.get("approved_families") or [])


def buy_allowed(family: str, model: dict | None, approval: set[str] | None = None) -> tuple[bool, str]:
    allowed = approved_families() if approval is None else approval
    if family not in allowed:
        return False, "promotion_evidence_missing_or_failed"
    if not isinstance(model, dict) or model.get("promoted") is not True:
        return False, "model_unpromoted"
    if not promotion_supported(model, family):
        return False, "promotion_evidence_missing_or_failed"
    return True, "approved"


def strip_unapproved_buys(result: dict, model: dict | None = None) -> dict:
    """Move every unapproved BUY onto the watch list. Does not invent a replacement order."""
    kept = []
    watch = list(result.get("WATCH") or [])
    for buy in result.get("BUY") or []:
        ok, why = buy_allowed(str(buy.get("family") or ""), model)
        if ok:
            kept.append(buy)
            continue
        watch.append({**buy, "status": "WATCH", "reasons": list(buy.get("reasons") or []) + [why]})
    result["BUY"] = kept
    result["WATCH"] = watch
    if not kept:
        result["production_buy_eligibility"] = "blocked"
    return result
