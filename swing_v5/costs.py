"""Effective-dated CNC delivery costs. Slippage is applied once, not also inside the fill price."""

from __future__ import annotations

from dataclasses import dataclass

# Zerodha equity delivery reference checked against the public charges page.
# IPFT is not added as a separate invented rate; txn is the published NSE charge.


@dataclass(frozen=True)
class Tariff:
    effective_from: str
    stt: float = 0.001
    nse_txn: float = 0.0000307
    stamp_buy: float = 0.00015
    sebi: float = 0.000001
    gst: float = 0.18
    dp_per_scrip_sell: float = 15.34
    brokerage: float = 0.0
    approximation: str = "present_schedule_not_a_historical_reconstruction"


TARIFF = Tariff(effective_from="2026-01-01")


def tariff_as_of(day: str) -> Tariff:
    if not isinstance(day, str) or len(day) < 10:
        raise ValueError("tariff date required")
    if day < TARIFF.effective_from:
        return Tariff(
            effective_from="unknown_before_2026",
            approximation="present_cost_approximation_before_verified_history",
        )
    return TARIFF


def buy_fees(price: float, qty: int, *, day: str, slippage_bps: float = 10.0) -> float:
    tariff = tariff_as_of(day)
    notional = max(price, 0.0) * max(int(qty), 0)
    if notional <= 0:
        return 0.0
    exch = tariff.nse_txn * notional
    sebi = tariff.sebi * notional
    brokerage = tariff.brokerage * notional
    gst = tariff.gst * (exch + sebi + brokerage)
    slip = (slippage_bps / 10000.0) * notional
    return tariff.stt * notional + exch + sebi + brokerage + gst + tariff.stamp_buy * notional + slip


def sell_fees(price: float, qty: int, *, day: str, slippage_bps: float = 10.0) -> float:
    tariff = tariff_as_of(day)
    notional = max(price, 0.0) * max(int(qty), 0)
    if notional <= 0:
        return 0.0
    exch = tariff.nse_txn * notional
    sebi = tariff.sebi * notional
    brokerage = tariff.brokerage * notional
    gst = tariff.gst * (exch + sebi + brokerage)
    slip = (slippage_bps / 10000.0) * notional
    dp = tariff.dp_per_scrip_sell
    return tariff.stt * notional + exch + sebi + brokerage + gst + dp + slip


def fee_components(entry: float, exit_px: float, qty: int, *, entry_day: str, exit_day: str, slippage_bps: float = 10.0, charge_dp: bool = True) -> dict:
    """DP is once per scrip sale, not once per share. Slippage is in the fee total, not in the prices."""
    buy = tariff_as_of(entry_day)
    sell = tariff_as_of(exit_day)
    buy_notional = max(entry, 0.0) * max(int(qty), 0)
    sell_notional = max(exit_px, 0.0) * max(int(qty), 0)
    stt = buy.stt * buy_notional + sell.stt * sell_notional
    exch = buy.nse_txn * buy_notional + sell.nse_txn * sell_notional
    sebi = buy.sebi * buy_notional + sell.sebi * sell_notional
    brokerage = buy.brokerage * buy_notional + sell.brokerage * sell_notional
    gst = buy.gst * (buy.nse_txn * buy_notional + buy.sebi * buy_notional + buy.brokerage * buy_notional)
    gst += sell.gst * (sell.nse_txn * sell_notional + sell.sebi * sell_notional + sell.brokerage * sell_notional)
    stamp = buy.stamp_buy * buy_notional
    dp = sell.dp_per_scrip_sell if charge_dp and qty > 0 else 0.0
    slippage = (slippage_bps / 10000.0) * (buy_notional + sell_notional)
    total = stt + exch + sebi + brokerage + gst + stamp + dp + slippage
    return {
        "stt": stt,
        "exchange": exch,
        "sebi": sebi,
        "gst": gst,
        "stamp": stamp,
        "dp": dp,
        "slippage": slippage,
        "total": total,
        "buy_tariff": buy.approximation,
        "sell_tariff": sell.approximation,
    }


def round_trip(entry: float, exit_px: float, qty: int, *, day: str, slippage_bps: float = 10.0) -> float:
    return buy_fees(entry, qty, day=day, slippage_bps=slippage_bps) + sell_fees(
        exit_px, qty, day=day, slippage_bps=slippage_bps
    )


def legacy_be(avg_cost: float, qty: int) -> float:
    return float(avg_cost) * 1.0025 + 18.0 / max(int(qty), 1)


def tranche_net_pnl(avg_cost: float, exit_px: float, qty: int, *, day: str) -> float:
    """Allocated cost versus actual sell proceeds after this sale's own fees."""
    gross = (exit_px - avg_cost) * qty
    return gross - sell_fees(exit_px, qty, day=day, slippage_bps=0.0)
