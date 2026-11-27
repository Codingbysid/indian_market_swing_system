"""Self-contained S3 portfolio state management and optimistic concurrency validation."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any

import boto3


class PortfolioInvalid(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StaleWrite(Exception):
    pass


REQUIRED_HOLDING = ("symbol", "qty", "avg_cost")


def validate_state(data: dict | None) -> dict:
    """Validate portfolio state dictionary strictly. Normal loads never invent cash or holdings."""
    if not isinstance(data, dict):
        raise PortfolioInvalid("state_missing_or_not_object")
    if "cash" not in data:
        raise PortfolioInvalid("cash_missing")
    try:
        cash = float(data["cash"])
    except (TypeError, ValueError):
        raise PortfolioInvalid("cash_not_numeric")
    if not math.isfinite(cash) or cash < 0:
        raise PortfolioInvalid("cash_invalid")

    holdings = data.get("holdings", [])
    if not isinstance(holdings, list):
        raise PortfolioInvalid("holdings_missing")

    seen = set()
    cleaned_holdings = []
    for h in holdings:
        if not isinstance(h, dict):
            raise PortfolioInvalid("holding_not_object")
        for key in REQUIRED_HOLDING:
            if key not in h:
                raise PortfolioInvalid(f"holding_missing_{key}")
        sym = str(h["symbol"]).strip().upper()
        if not sym or sym in seen:
            raise PortfolioInvalid("holding_symbol_invalid")
        seen.add(sym)
        try:
            qty = int(h["qty"])
            avg = float(h["avg_cost"])
        except (TypeError, ValueError):
            raise PortfolioInvalid("holding_qty_or_cost_invalid")
        if qty <= 0 or not math.isfinite(avg) or avg <= 0:
            raise PortfolioInvalid("holding_qty_or_cost_invalid")

        h_clean = copy.deepcopy(h)
        h_clean["symbol"] = sym
        h_clean["qty"] = qty
        h_clean["avg_cost"] = avg
        cleaned_holdings.append(h_clean)

    out = copy.deepcopy(data)
    out["cash"] = cash
    out["holdings"] = cleaned_holdings
    out.setdefault("closed_trades", [])
    out.setdefault("reservations", [])
    out.setdefault("status", "PORTFOLIO_CONFIRMED")
    out.setdefault("updated", datetime.now(timezone.utc).isoformat())
    return out


def apply_fill(state: dict, fill: dict) -> dict:
    """Apply broker fill to portfolio state with cash accounting."""
    state = validate_state(state)
    side = fill.get("side", "").upper()
    sym = fill.get("symbol", "").upper()
    qty = int(fill.get("qty", 0))
    price = float(fill.get("price", 0.0))
    fees = float(fill.get("fees", 0.0))

    if side == "BUY":
        total_cost = qty * price + fees
        if state["cash"] < total_cost:
            raise PortfolioInvalid(f"insufficient_cash: need {total_cost}, have {state['cash']}")
        state["cash"] = round(state["cash"] - total_cost, 2)
        # Update or add holding
        existing = next((h for h in state["holdings"] if h["symbol"] == sym), None)
        if existing:
            new_qty = existing["qty"] + qty
            existing["avg_cost"] = round(((existing["qty"] * existing["avg_cost"]) + (qty * price)) / new_qty, 2)
            existing["qty"] = new_qty
            if "high_watermark" in existing:
                existing["high_watermark"] = max(existing["high_watermark"], price)
        else:
            state["holdings"].append(
                {
                    "symbol": sym,
                    "qty": qty,
                    "avg_cost": price,
                    "entry_date": fill.get("entry_date", datetime.now(timezone.utc).isoformat()),
                    "entry_price": price,
                    "stop": float(fill.get("stop", price * 0.88)),
                    "high_watermark": price,
                    "sector": fill.get("sector", "UNKNOWN"),
                    "v3_rank": fill.get("v3_rank"),
                }
            )

    elif side == "SELL":
        existing = next((h for h in state["holdings"] if h["symbol"] == sym), None)
        if not existing or existing["qty"] < qty:
            raise PortfolioInvalid(f"holding_missing_or_short_qty for {sym}")
        proceeds = qty * price - fees
        state["cash"] = round(state["cash"] + proceeds, 2)
        pnl = round(qty * (price - existing["avg_cost"]) - fees, 2)
        state["closed_trades"].append(
            {
                "symbol": sym,
                "qty": qty,
                "avg_cost": existing["avg_cost"],
                "exit_price": price,
                "pnl": pnl,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "trigger": fill.get("trigger", "SELL"),
            }
        )
        if existing["qty"] == qty:
            state["holdings"] = [h for h in state["holdings"] if h["symbol"] != sym]
        else:
            existing["qty"] -= qty

    state["updated"] = datetime.now(timezone.utc).isoformat()
    return validate_state(state)


def read_state(client: Any, bucket: str, key: str) -> tuple[dict, str]:
    """Optimistic read: returns validated state dictionary and S3 object ETag."""
    obj = client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read().decode("utf-8")
    etag = obj["ETag"].strip('"')
    return validate_state(json.loads(body)), etag


def write_state(client: Any, bucket: str, key: str, state: dict, etag: str | None = None) -> dict:
    """Optimistic conditional write: rejects stale updates if S3 state changed."""
    validated = validate_state(state)
    payload = json.dumps(validated, indent=2, default=str).encode("utf-8")
    kwargs: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": payload,
        "ContentType": "application/json",
    }
    if etag:
        kwargs["IfMatch"] = etag
    try:
        return client.put_object(**kwargs)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            raise StaleWrite(f"Portfolio state changed in S3; ETag {etag} mismatch.") from exc
        raise


def load_portfolio_state(
    s3_client: Any = None,
    bucket: str | None = None,
    key: str = "v2/state/latest.json",
) -> tuple[dict[str, Any], str | None]:
    """Load portfolio state from S3 with local fallback when S3 is unavailable."""
    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    if s3_client is not None:
        try:
            return read_state(s3_client, bucket_name, key)
        except Exception as exc:
            print(f"[State] Note: could not load s3://{bucket_name}/{key} ({exc}); trying legacy/local.")

    # Try local paths
    for local_candidate in [
        Path("data") / "portfolio_state.json",
        Path("v2") / "state" / "latest.json",
    ]:
        if local_candidate.exists():
            try:
                data = json.loads(local_candidate.read_text())
                return validate_state(data), None
            except Exception:
                pass

    # Baseline default state
    default_state = {
        "cash": 100000.0,
        "holdings": [],
        "closed_trades": [],
        "status": "PORTFOLIO_CONFIRMED",
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    return default_state, None


def held_symbol_set(state: dict) -> set[str]:
    holdings = state.get("holdings", []) if isinstance(state, dict) else []
    return {str(h["symbol"]).strip().upper() for h in holdings if "symbol" in h}
