"""Dated records. Screens and news become usable only at their known publication time."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScreenSnapshot:
    source: str
    query: str
    retrieved_at: str
    report_period: str | None
    url: str
    complete: bool
    symbols: tuple[str, ...]
    content_hash: str

    def active_on(self, as_of: str) -> bool:
        return self.complete and self.retrieved_at <= as_of


@dataclass(frozen=True)
class RegistryEntry:
    family_id: str
    version: str
    horizon: str
    status: str  # research | shadow | promoted
    requires_fundamental_snapshot: bool


DEFAULT_REGISTRY: dict[str, RegistryEntry] = {
    "quality_continuation": RegistryEntry(
        "quality_continuation", "1", "short", "research", True
    ),
    "trend_transition": RegistryEntry(
        "trend_transition", "1", "long", "research", False
    ),
    "trend_transition_price_control": RegistryEntry(
        "trend_transition_price_control", "1", "long", "research", False
    ),
    "improving_value": RegistryEntry(
        "improving_value", "1", "long", "research", True
    ),
}
