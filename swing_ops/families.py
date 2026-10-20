"""One status row per registered family. A missing input is not a completed evaluation."""

from __future__ import annotations

from swing_core.strategies import REGISTRY

V5 = (
    ("quality_continuation", "short", "qtv_v1_frozen"),
    ("trend_transition", "long", "qtv_v1_frozen"),
    ("improving_value", "long", "qtv_v1_frozen"),
)


def family_inventory() -> list[dict]:
    rows = []
    for family, horizon, version in V5:
        rows.append({
            "family": family,
            "horizon": horizon,
            "version": version,
            "registry_status": "research",
            "production_status": "BLOCKED",
            "production_reason": "unpromoted_no_point_in_time_validation",
            "evaluation": "not_evaluated",
            "detection": "not_evaluated",
            "data_blocker": "not_evaluated_no_point_in_time_snapshot" if family != "trend_transition" else "pending_panel",
            "admission_blocker": "promotion_evidence_missing_or_failed",
        })
    for family, spec in REGISTRY.items():
        if spec.status == "research":
            production = "BLOCKED"
            reason = "research_only_not_enabled"
        else:
            production = "WATCH"
            reason = "promotion_evidence_missing_or_failed"
        rows.append({
            "family": family,
            "horizon": "short",
            "version": spec.version,
            "registry_status": spec.status,
            "production_status": production,
            "production_reason": reason,
            "evaluation": "not_evaluated",
            "detection": "not_evaluated",
            "data_blocker": "not_in_bounded_paper_panel",
            "admission_blocker": reason,
        })
    return rows
