"""Itemized Indian equity delivery cost accounting in basis points."""

from __future__ import annotations

from dataclasses import dataclass

from src.config import IndiaDeliveryCostConfig


@dataclass(frozen=True)
class RoundTripCostBps:
    statutory_bps: float
    slippage_bps: float
    total_bps: float

    @property
    def half_leg_fraction(self) -> float:
        """Fraction of notional charged on each of entry and exit (half RT)."""
        return self.total_bps / 20_000.0


def resolve_rt_cost(
    cfg: IndiaDeliveryCostConfig = IndiaDeliveryCostConfig(),
    *,
    stress_slippage: bool = False,
) -> RoundTripCostBps:
    slip = (
        2.0 * cfg.stress_slippage_per_leg_bps
        if stress_slippage
        else 2.0 * cfg.slippage_per_leg_bps
    )
    statutory = cfg.statutory_rt_bps
    return RoundTripCostBps(
        statutory_bps=statutory,
        slippage_bps=slip,
        total_bps=statutory + slip,
    )


def cost_on_notional(notional: float, rt: RoundTripCostBps, *, half: bool = True) -> float:
    """Cash cost for one leg (half) or full round trip."""
    frac = rt.half_leg_fraction if half else rt.total_bps / 10_000.0
    return float(notional) * frac
