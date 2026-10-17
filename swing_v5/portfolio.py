"""Broker truth, immutable tranches, and conditional writes. A screenshot is not a fill ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .costs import legacy_be, tranche_net_pnl


class PortfolioInvalid(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StaleWrite(Exception):
    pass


def book_from_reference(raw: dict) -> dict:
    """Onboarding reference. ZENTEC's new quantity is not a silent tranche reset."""
    holdings = []
    for row in raw["holdings"]:
        item = {
            "symbol": row["symbol"],
            "qty": int(row["qty"]),
            "avg_cost": float(row["avg_cost"]),
            "mark": float(row["mark"]),
            "classification": "legacy_profit_only",
            "no_averaging": True,
            "t1_filled_qty": 0,
            "t2_filled_qty": 0,
        }
        if row["symbol"] == "HAL":
            item.update({"t1_qty_plan": 5, "t2_qty_plan": 5, "runner_qty_plan": 6, "plan_status": "saved_if_unfilled"})
        elif row["symbol"] == "CDSL":
            item.update({"t1_qty_plan": 3, "t2_qty_plan": 3, "runner_qty_plan": 2, "plan_status": "saved_if_unfilled"})
        elif row["symbol"] == "BPCL":
            item.update({"t1_qty_plan": 2, "t2_qty_plan": 2, "runner_qty_plan": 2, "plan_status": "saved_if_unfilled"})
        elif row["symbol"] == "ZENTEC":
            item.update(
                {
                    "plan_status": "PLAN_RECONCILIATION_REQUIRED",
                    "proposed_remaining_plan": [3, 3, 4],
                    "plan_applied": False,
                }
            )
        holdings.append(item)
    return {
        "schema": "v5_book",
        "as_of": raw["as_of"],
        "cash": float(raw["cash"]),
        "launch_nav": float(raw["cash"]),
        "external_inflows": 0.0,
        "legacy_transfers": 0.0,
        "circuit_latched": False,
        "holdings": holdings,
        "reservations": [],
        "fills": [],
        "pending_orders": list(raw.get("pending_orders") or []),
        "quote_status": "unverified_weekend_screenshot",
        "restriction_metadata": "missing",
        "source": raw.get("schema"),
        "s3_overwrite_allowed": False,
    }


def validate_book(book: dict) -> dict:
    if not isinstance(book, dict) or book.get("cash") is None:
        raise PortfolioInvalid("book_missing")
    cash = float(book["cash"])
    if not math.isfinite(cash) or cash < 0:
        raise PortfolioInvalid("cash_invalid")
    if not isinstance(book.get("holdings"), list):
        raise PortfolioInvalid("holdings_missing")
    return book


def remaining_tranches(holding: dict) -> tuple[int, int, int] | None:
    if holding.get("plan_status") == "PLAN_RECONCILIATION_REQUIRED" and not holding.get("plan_applied"):
        return None
    qty = int(holding["qty"])
    t1 = max(0, int(holding.get("t1_qty_plan") or 0) - int(holding.get("t1_filled_qty") or 0))
    t2 = max(0, int(holding.get("t2_qty_plan") or 0) - int(holding.get("t2_filled_qty") or 0))
    runner = max(0, int(holding.get("runner_qty_plan") or 0))
    if t1 + t2 + runner > qty:
        runner = max(0, qty - t1 - t2)
    return t1, t2, runner


def apply_fill(book: dict, fill: dict) -> dict:
    book = validate_book(book)
    fill_id = str(fill.get("fill_id") or "")
    if not fill_id:
        raise PortfolioInvalid("fill_id_required")
    if any(row.get("fill_id") == fill_id for row in book.get("fills") or []):
        return book
    side = fill["side"]
    symbol = fill["symbol"]
    qty = int(fill["qty"])
    price = float(fill["price"])
    fees = float(fill.get("fees") or 0)
    if side == "buy":
        if fill.get("classification") != "system_atr":
            raise PortfolioInvalid("new_buy_must_be_system_lot")
        for required in ("entry_atr", "stop", "target", "setup"):
            if fill.get(required) in (None, ""):
                raise PortfolioInvalid("system_lot_missing_risk")
        book["cash"] = round(book["cash"] - qty * price - fees, 2)
        book["holdings"].append(
            {
                "symbol": symbol,
                "qty": qty,
                "avg_cost": price,
                "classification": "system_atr",
                "entry_atr": fill["entry_atr"],
                "stop": fill["stop"],
                "target": fill["target"],
                "setup": fill["setup"],
            }
        )
    elif side == "sell":
        holding = next(row for row in book["holdings"] if row["symbol"] == symbol)
        if qty > int(holding["qty"]):
            raise PortfolioInvalid("oversell")
        stage = fill.get("stage")
        if stage == "t1":
            left = remaining_tranches(holding)
            if left is None or qty > left[0]:
                raise PortfolioInvalid("immutable_stage")
            holding["t1_filled_qty"] = int(holding.get("t1_filled_qty") or 0) + qty
        holding["qty"] = int(holding["qty"]) - qty
        book["cash"] = round(book["cash"] + qty * price - fees, 2)
        if holding["qty"] == 0:
            book["holdings"] = [row for row in book["holdings"] if row is not holding]
    else:
        raise PortfolioInvalid("side")
    book.setdefault("fills", []).append({"fill_id": fill_id, "symbol": symbol, "side": side, "qty": qty})
    reservations = []
    for row in book.get("reservations") or []:
        if row.get("symbol") == symbol and row.get("status") == "open":
            remaining = int(row.get("qty") or 0) - qty
            row = {**row, "qty": max(0, remaining), "status": "open" if remaining > 0 else "filled"}
        reservations.append(row)
    book["reservations"] = reservations
    return book


def legacy_sell_allowed(holding: dict, price: float, qty: int, *, day: str) -> bool:
    if holding.get("classification") != "legacy_profit_only":
        return False
    be = legacy_be(holding["avg_cost"], holding["qty"])
    if price <= be:
        return False
    return tranche_net_pnl(holding["avg_cost"], price, qty, day=day) > 0


def system_exit(holding: dict, price: float, sessions_held: int) -> str | None:
    if holding.get("classification") != "system_atr":
        return None
    if price <= float(holding["stop"]):
        return "system_stop"
    if price >= float(holding["target"]):
        return "system_target"
    if sessions_held >= int(holding.get("max_sessions") or 15):
        return "system_time"
    return None


def update_trail(prev_hwm: float, active: bool, day_low: float, day_high: float, t1: float, be: float, atr: float) -> dict:
    prior = max(be, prev_hwm - 1.5 * atr) if active and prev_hwm > 0 else None
    breached = prior is not None and day_low <= prior and prior > be
    hwm = max(prev_hwm, day_high)
    now = active or hwm > t1
    return {"breached_prior": breached, "high_watermark": hwm, "trail_active": now, "prior_trail": prior}


def apply_marks(book: dict, prices: dict, required: list[str]) -> dict:
    missing = [symbol for symbol in required if prices.get(symbol) in (None, "")]
    out = dict(book)
    if missing or book.get("marks_status") == "stale":
        out["marks_status"] = "stale"
        out["missing_marks"] = missing
        return out
    out["marks_status"] = "ok"
    out["marks"] = {symbol: float(prices[symbol]) for symbol in required}
    return out


@dataclass
class VersionedStore:
    docs: dict

    def __init__(self):
        self.docs = {}

    def put(self, key: str, body: dict, expected_version):
        current = self.docs.get(key)
        version = None if current is None else current["version"]
        if expected_version != version:
            raise StaleWrite(key)
        new_version = 1 if version is None else version + 1
        self.docs[key] = {"version": new_version, "body": dict(body)}
        return new_version


def conditional_put(client, bucket: str, key: str, body: bytes, etag: str):
    if not etag:
        raise StaleWrite("etag_required")
    try:
        return client.put_object(Bucket=bucket, Key=key, Body=body, IfMatch=etag)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "412", "409"):
            raise StaleWrite("conflict") from exc
        raise
