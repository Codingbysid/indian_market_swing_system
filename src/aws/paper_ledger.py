"""Self-contained Shadow Paper-Trading Portfolio Handler & S3 Ledger.

Manages isolated paper trading under S3 prefix `v2/paper/`:
- `v2/paper/portfolio_state.json`: State with initial capital, cash, positions, weekly counters.
- `v2/paper/trades.jsonl`: Append-only trade journal with 16.90 / 15.40 bps cost accounting.
- `v2/paper/daily_nav.jsonl`: EOD equity, daily returns, alpha vs Nifty, sector allocations.

Zero modification to real portfolio state.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.aws.calendar_nse import IST, now_ist
from src.aws.state import StaleWrite, read_state, write_state

logger = logging.getLogger(__name__)

# Delivery cost constants (basis points)
BUY_COST_BPS: float = 16.90   # 10.0 STT + 1.5 Stamp + 0.4 exchange/SEBI/GST + 5.0 slippage
SELL_COST_BPS: float = 15.40  # 10.0 STT + 0.4 exchange/SEBI/GST + 5.0 slippage
ROUND_TRIP_COST_BPS: float = 32.30

# Option A+ HardCap portfolio limits
MAX_OPEN_POSITIONS: int = 10
MAX_WEEKLY_ENTRIES: int = 2
MAX_OPEN_PER_SECTOR: int = 2
MAX_NEW_PER_SECTOR_WEEKLY: int = 1

# S3 and local keys
PAPER_STATE_KEY: str = "v2/paper/portfolio_state.json"
PAPER_TRADES_KEY: str = "v2/paper/trades.jsonl"
PAPER_DAILY_NAV_KEY: str = "v2/paper/daily_nav.jsonl"


def get_default_initial_capital() -> float:
    try:
        return float(os.getenv("PAPER_INITIAL_CAPITAL", "500000.0"))
    except (ValueError, TypeError):
        return 500000.0


def get_iso_week_id(dt: datetime | None = None) -> str:
    current = dt or now_ist()
    year, week, _ = current.isocalendar()
    return f"{year}-W{week:02d}"


def init_paper_state(initial_capital: float | None = None) -> dict[str, Any]:
    """Initialize a clean paper trading portfolio state."""
    cap = float(initial_capital if initial_capital is not None else get_default_initial_capital())
    current_dt = now_ist()
    return {
        "initial_capital": cap,
        "cash": cap,
        "week_id": get_iso_week_id(current_dt),
        "weekly_entries_count": 0,
        "weekly_sectors_entered": [],
        "positions": [],
        "updated_at": current_dt.isoformat(),
        "status": "PAPER_ACTIVE",
    }


def check_week_rollover(state: dict[str, Any], current_dt: datetime | None = None) -> bool:
    """Reset weekly counters when ISO calendar week rolls over."""
    current_week = get_iso_week_id(current_dt or now_ist())
    if state.get("week_id") != current_week:
        logger.info(
            "Paper state week rollover detected: %s -> %s. Resetting weekly counters.",
            state.get("week_id"),
            current_week,
        )
        state["week_id"] = current_week
        state["weekly_entries_count"] = 0
        state["weekly_sectors_entered"] = []
        return True
    return False


def validate_paper_state(state: dict[str, Any]) -> dict[str, Any]:
    """Validate paper portfolio state structure."""
    if not isinstance(state, dict):
        raise ValueError("Paper state must be a dictionary")
    if "cash" not in state or "positions" not in state:
        raise ValueError("Paper state missing cash or positions")

    state["cash"] = float(state["cash"])
    state.setdefault("initial_capital", get_default_initial_capital())
    state.setdefault("week_id", get_iso_week_id())
    state.setdefault("weekly_entries_count", 0)
    state.setdefault("weekly_sectors_entered", [])
    state.setdefault("positions", [])
    state.setdefault("status", "PAPER_ACTIVE")

    # Validate individual positions
    cleaned_positions = []
    for pos in state["positions"]:
        if not isinstance(pos, dict) or "symbol" not in pos or "qty" not in pos:
            continue
        p = copy.deepcopy(pos)
        p["symbol"] = str(p["symbol"]).strip().upper()
        p["qty"] = int(p["qty"])
        p["avg_entry_price"] = float(p.get("avg_entry_price", 0.0))
        p["peak_price"] = float(p.get("peak_price", p["avg_entry_price"]))
        p["stop_price"] = float(p.get("stop_price", round(p["peak_price"] * 0.88, 2)))
        cleaned_positions.append(p)
    state["positions"] = cleaned_positions
    return state


def _local_paper_dir() -> Path:
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("LAMBDA_TASK_ROOT"):
        out = Path("/tmp") / "v2" / "paper"
        out.mkdir(parents=True, exist_ok=True)
        return out
    candidates = [
        Path(__file__).resolve().parents[2] / "v2" / "paper",
        Path("v2") / "paper",
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except OSError:
            pass
    out = Path("/tmp") / "v2" / "paper"
    out.mkdir(parents=True, exist_ok=True)
    return out


def load_paper_state(
    s3_client: Any = None,
    bucket: str | None = None,
    key: str = PAPER_STATE_KEY,
) -> tuple[dict[str, Any], str | None]:
    """Load paper portfolio state from S3 with local fallback or auto-init."""
    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    if s3_client is not None:
        try:
            obj = s3_client.get_object(Bucket=bucket_name, Key=key)
            body = obj["Body"].read().decode("utf-8")
            etag = obj["ETag"].strip('"')
            state = validate_paper_state(json.loads(body))
            check_week_rollover(state)
            return state, etag
        except Exception as exc:
            logger.info("Could not load s3://%s/%s (%s); trying local or auto-init.", bucket_name, key, exc)

    # Local fallback
    local_path = _local_paper_dir() / "portfolio_state.json"
    if local_path.exists():
        try:
            state = validate_paper_state(json.loads(local_path.read_text(encoding="utf-8")))
            check_week_rollover(state)
            return state, None
        except Exception as exc:
            logger.warning("Local paper state corrupt: %s; re-initializing.", exc)

    # Auto-initialize on first run
    fresh_state = init_paper_state()
    try:
        local_path.write_text(json.dumps(fresh_state, indent=2), encoding="utf-8")
    except Exception:
        pass
    return fresh_state, None


def save_paper_state(
    s3_client: Any,
    bucket: str | None,
    state: dict[str, Any],
    etag: str | None = None,
    key: str = PAPER_STATE_KEY,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Save paper state to S3 with optimistic concurrency and update local file."""
    validated = validate_paper_state(state)
    validated["updated_at"] = now_ist().isoformat()
    payload = json.dumps(validated, indent=2, default=str)

    # Write local copy
    try:
        local_path = _local_paper_dir() / "portfolio_state.json"
        local_path.write_text(payload, encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write local paper state: %s", exc)

    if dry_run or s3_client is None:
        return {"status": "LOCAL_SAVED" if not dry_run else "DRY_RUN"}

    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    kwargs: dict[str, Any] = {
        "Bucket": bucket_name,
        "Key": key,
        "Body": payload.encode("utf-8"),
        "ContentType": "application/json",
    }
    if etag:
        kwargs["IfMatch"] = etag
    try:
        return s3_client.put_object(**kwargs)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            raise StaleWrite(f"Paper state changed in S3; ETag {etag} mismatch.") from exc
        raise


def append_paper_trade(
    s3_client: Any,
    bucket: str | None,
    trade_record: dict[str, Any],
    key: str = PAPER_TRADES_KEY,
    dry_run: bool = False,
) -> None:
    """Append a trade record to local and S3 trades.jsonl."""
    line = json.dumps(trade_record, default=str) + "\n"

    # Local append
    try:
        local_path = _local_paper_dir() / "trades.jsonl"
        with open(local_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("Local trade append failed: %s", exc)

    if dry_run or s3_client is None:
        return

    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    try:
        existing = ""
        try:
            obj = s3_client.get_object(Bucket=bucket_name, Key=key)
            existing = obj["Body"].read().decode("utf-8")
        except Exception:
            existing = ""
        new_content = existing + line
        s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=new_content.encode("utf-8"),
            ContentType="application/x-ndjson",
        )
    except Exception as exc:
        logger.error("Failed to append trade to s3://%s/%s: %s", bucket_name, key, exc)


def append_paper_daily_nav(
    s3_client: Any,
    bucket: str | None,
    nav_record: dict[str, Any],
    key: str = PAPER_DAILY_NAV_KEY,
    dry_run: bool = False,
) -> None:
    """Append EOD daily NAV snapshot to local and S3 daily_nav.jsonl."""
    line = json.dumps(nav_record, default=str) + "\n"

    # Local append
    try:
        local_path = _local_paper_dir() / "daily_nav.jsonl"
        with open(local_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("Local daily NAV append failed: %s", exc)

    if dry_run or s3_client is None:
        return

    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    try:
        existing = ""
        try:
            obj = s3_client.get_object(Bucket=bucket_name, Key=key)
            existing = obj["Body"].read().decode("utf-8")
        except Exception:
            existing = ""
        new_content = existing + line
        s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=new_content.encode("utf-8"),
            ContentType="application/x-ndjson",
        )
    except Exception as exc:
        logger.error("Failed to append daily NAV to s3://%s/%s: %s", bucket_name, key, exc)


def read_paper_trades(
    s3_client: Any = None,
    bucket: str | None = None,
    key: str = PAPER_TRADES_KEY,
) -> list[dict[str, Any]]:
    """Read all trades from S3 or local journal."""
    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    content = ""
    if s3_client is not None:
        try:
            obj = s3_client.get_object(Bucket=bucket_name, Key=key)
            content = obj["Body"].read().decode("utf-8")
        except Exception:
            pass

    if not content:
        local_path = _local_paper_dir() / "trades.jsonl"
        if local_path.exists():
            content = local_path.read_text(encoding="utf-8")

    trades = []
    for line in content.splitlines():
        line = line.strip()
        if line:
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    return trades


def read_paper_daily_nav(
    s3_client: Any = None,
    bucket: str | None = None,
    key: str = PAPER_DAILY_NAV_KEY,
) -> list[dict[str, Any]]:
    """Read all daily NAV rows from S3 or local snapshot file."""
    bucket_name = bucket or os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    content = ""
    if s3_client is not None:
        try:
            obj = s3_client.get_object(Bucket=bucket_name, Key=key)
            content = obj["Body"].read().decode("utf-8")
        except Exception:
            pass

    if not content:
        local_path = _local_paper_dir() / "daily_nav.jsonl"
        if local_path.exists():
            content = local_path.read_text(encoding="utf-8")

    navs = []
    for line in content.splitlines():
        line = line.strip()
        if line:
            try:
                navs.append(json.loads(line))
            except Exception:
                pass
    return navs


def ratchet_trailing_stops(positions: list[dict[str, Any]], price_by_sym: dict[str, float]) -> None:
    """Update peak_price and stop_price = round(peak_price * 0.88, 2) in-place."""
    for pos in positions:
        sym = pos["symbol"]
        current_p = float(price_by_sym.get(sym, pos.get("avg_entry_price", 0.0)))
        peak = max(float(pos.get("peak_price", pos.get("avg_entry_price", 0.0))), current_p)
        pos["peak_price"] = peak
        pos["stop_price"] = round(peak * 0.88, 2)


def execute_paper_sells(
    state: dict[str, Any],
    sell_candidates: list[dict[str, Any]],
    price_by_sym: dict[str, float],
    slot_name: str,
    dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """Execute paper SELL signals at current quote minus 15.40 bps cost."""
    current_dt = dt or now_ist()
    date_str = current_dt.strftime("%Y-%m-%d")
    executed_trades: list[dict[str, Any]] = []

    pos_by_sym = {p["symbol"]: p for p in state["positions"]}

    for item in sell_candidates:
        sym = str(item.get("symbol", "")).strip().upper()
        if sym not in pos_by_sym:
            continue

        pos = pos_by_sym[sym]
        qty = int(pos["qty"])
        fill_price = float(price_by_sym.get(sym, item.get("price", pos["avg_entry_price"])))
        gross_notional = round(qty * fill_price, 2)
        cost_inr = round(gross_notional * (SELL_COST_BPS / 10000.0), 2)
        net_cash_delta = round(gross_notional - cost_inr, 2)

        # Realized PnL accounting (including entry and exit costs)
        entry_price = float(pos.get("avg_entry_price", fill_price))
        entry_notional = round(qty * entry_price, 2)
        entry_cost_inr = float(pos.get("entry_cost_inr", round(entry_notional * (BUY_COST_BPS / 10000.0), 2)))
        gross_pnl = gross_notional - entry_notional
        realized_pnl_inr = round(gross_pnl - (entry_cost_inr + cost_inr), 2)
        realized_pnl_bps = round((realized_pnl_inr / entry_notional) * 10000.0, 1) if entry_notional > 0 else 0.0

        trigger_raw = item.get("trigger", item.get("reason", "RANK_BUFFER_TOP40"))
        if "trailing" in str(trigger_raw).lower():
            trigger_reason = "TRAILING_STOP_12PCT"
        elif "hold" in str(trigger_raw).lower() or "week" in str(trigger_raw).lower():
            trigger_reason = "MAX_HOLD_12W"
        else:
            trigger_reason = "RANK_BUFFER_TOP40"

        trade_record = {
            "timestamp_ist": current_dt.isoformat(),
            "date": date_str,
            "slot_name": slot_name,
            "action": "SELL",
            "symbol": sym,
            "sector": pos.get("sector", "UNKNOWN"),
            "qty": qty,
            "fill_price": fill_price,
            "gross_notional": gross_notional,
            "cost_bps": SELL_COST_BPS,
            "cost_inr": cost_inr,
            "net_cash_delta": net_cash_delta,
            "v3_rank": item.get("v3_rank", pos.get("entry_v3_rank", 999)),
            "trigger_reason": trigger_reason,
            "realized_pnl_inr": realized_pnl_inr,
            "realized_pnl_bps": realized_pnl_bps,
        }

        # Credit cash and remove position
        state["cash"] = round(state["cash"] + net_cash_delta, 2)
        state["positions"] = [p for p in state["positions"] if p["symbol"] != sym]
        executed_trades.append(trade_record)

    return executed_trades


def execute_paper_buys(
    state: dict[str, Any],
    buy_candidates: list[dict[str, Any]],
    price_by_sym: dict[str, float],
    slot_name: str,
    dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """Execute paper BUY signals with A+ HardCap sizing and 16.90 bps cost."""
    current_dt = dt or now_ist()
    date_str = current_dt.strftime("%Y-%m-%d")
    check_week_rollover(state, current_dt)

    executed_trades: list[dict[str, Any]] = []

    # Current holdings inventory
    held_symbols = {p["symbol"] for p in state["positions"]}
    sector_counts: dict[str, int] = {}
    for p in state["positions"]:
        sec = p.get("sector", "UNKNOWN")
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    weekly_sectors = set(state.get("weekly_sectors_entered", []))
    weekly_entries_count = int(state.get("weekly_entries_count", 0))

    # Calculate portfolio equity for position sizing
    invested_val = sum(
        p["qty"] * float(price_by_sym.get(p["symbol"], p.get("avg_entry_price", 0.0)))
        for p in state["positions"]
    )
    total_equity = state["cash"] + invested_val

    available_slots = min(
        MAX_OPEN_POSITIONS - len(state["positions"]),
        MAX_WEEKLY_ENTRIES - weekly_entries_count,
    )

    if available_slots <= 0:
        return []

    for candidate in buy_candidates:
        if available_slots <= 0 or weekly_entries_count >= MAX_WEEKLY_ENTRIES:
            break

        sym = str(candidate.get("symbol", "")).strip().upper()
        if not sym or sym in held_symbols:
            continue

        sec = candidate.get("sector", "UNKNOWN")
        rank = int(candidate.get("rank", candidate.get("v3_rank", 999)))
        price = float(price_by_sym.get(sym, candidate.get("price", candidate.get("entry_price", 0.0))))

        if price <= 0:
            continue

        # HardCap Constraint 1: Max 2 open per sector
        if sector_counts.get(sec, 0) >= MAX_OPEN_PER_SECTOR:
            continue

        # HardCap Constraint 2: Max 1 new entry per sector per week
        if sec in weekly_sectors:
            continue

        # Target sizing: min(10% of portfolio equity, cash / available_slots)
        target_notional = min(total_equity * 0.10, state["cash"] / max(available_slots, 1))
        qty = int(target_notional // price)
        if qty <= 0:
            continue

        gross_notional = round(qty * price, 2)
        cost_inr = round(gross_notional * (BUY_COST_BPS / 10000.0), 2)
        net_cash_delta = -round(gross_notional + cost_inr, 2)

        if state["cash"] < abs(net_cash_delta):
            continue

        # Debit cash
        state["cash"] = round(state["cash"] + net_cash_delta, 2)

        # Record position
        new_position = {
            "symbol": sym,
            "qty": qty,
            "avg_entry_price": price,
            "entry_date": date_str,
            "entry_slot": slot_name,
            "peak_price": price,
            "stop_price": round(price * 0.88, 2),
            "sector": sec,
            "entry_v3_rank": rank,
            "entry_cost_inr": cost_inr,
        }
        state["positions"].append(new_position)

        # Update weekly trackers
        weekly_entries_count += 1
        state["weekly_entries_count"] = weekly_entries_count
        weekly_sectors.add(sec)
        state["weekly_sectors_entered"] = list(weekly_sectors)
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        held_symbols.add(sym)
        available_slots -= 1

        trade_record = {
            "timestamp_ist": current_dt.isoformat(),
            "date": date_str,
            "slot_name": slot_name,
            "action": "BUY",
            "symbol": sym,
            "sector": sec,
            "qty": qty,
            "fill_price": price,
            "gross_notional": gross_notional,
            "cost_bps": BUY_COST_BPS,
            "cost_inr": cost_inr,
            "net_cash_delta": net_cash_delta,
            "v3_rank": rank,
            "trigger_reason": "TOP_V3_RANK",
            "realized_pnl_inr": None,
            "realized_pnl_bps": None,
        }
        executed_trades.append(trade_record)

    return executed_trades


def compute_paper_summary(
    state: dict[str, Any],
    price_by_sym: dict[str, float],
    today_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute real-time paper book valuation and health snapshot."""
    cash = float(state.get("cash", 0.0))
    positions = state.get("positions", [])

    invested_value = 0.0
    cost_basis = 0.0
    for p in positions:
        sym = p["symbol"]
        qty = int(p["qty"])
        ltp = float(price_by_sym.get(sym, p.get("avg_entry_price", 0.0)))
        invested_value += qty * ltp
        cost_basis += qty * float(p.get("avg_entry_price", 0.0))

    unrealized_pnl = round(invested_value - cost_basis, 2)
    unrealized_pnl_pct = round((unrealized_pnl / cost_basis) * 100.0, 2) if cost_basis > 0 else 0.0
    total_equity = round(cash + invested_value, 2)

    return {
        "cash": round(cash, 2),
        "invested_value": round(invested_value, 2),
        "total_equity": total_equity,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "open_count": len(positions),
        "weekly_entries": int(state.get("weekly_entries_count", 0)),
        "today_fills": today_trades or [],
    }
