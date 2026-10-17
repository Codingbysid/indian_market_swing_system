"""Health is explicit. Missing evidence is not a green light."""

from __future__ import annotations


def summarize(records: dict) -> dict:
    status = "ok"
    reasons = []
    if records.get("provider_failures"):
        status = "degraded"
        reasons.append("provider_failures")
    if records.get("forward_sessions", 0) < 60:
        status = "insufficient"
        reasons.append("forward_shadow_below_60")
    if records.get("promoted"):
        reasons.append("promoted_flag_unexpected")
        status = "review"
    return {"status": status, "green": False, "reasons": reasons}
