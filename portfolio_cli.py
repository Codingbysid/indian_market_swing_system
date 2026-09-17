"""Sync Zerodha fills into s3://<bucket>/portfolio_state.json

The Lambda recommender and holdings monitor read this file each run.
Keep it in lockstep with the brokerage account after every fill.

Usage:
  python portfolio_cli.py show
  python portfolio_cli.py set-cash 24000
  python portfolio_cli.py add INFY 5 1030 [--sleeve core|satellite|legacy]
  python portfolio_cli.py close INFY 5 1065
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

REGION = os.getenv("AWS_DEFAULT_REGION", "eu-north-1")
BUCKET = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
STATE_KEY = os.getenv("PORTFOLIO_STATE_KEY", "portfolio_state.json")
LOCAL_SEED = Path(__file__).resolve().parent / "data" / "portfolio_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _s3():
    return boto3.client("s3", region_name=REGION)


def default_state() -> dict:
    if LOCAL_SEED.exists():
        return json.loads(LOCAL_SEED.read_text(encoding="utf-8"))
    return {"cash": 24000.0, "holdings": [], "closed_trades": [], "updated": _now()}


def load_state() -> dict:
    try:
        obj = _s3().get_object(Bucket=BUCKET, Key=STATE_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            print(f"s3://{BUCKET}/{STATE_KEY} missing; using local seed.")
            data = default_state()
        else:
            raise
    data.setdefault("cash", 0.0)
    data.setdefault("holdings", [])
    data.setdefault("closed_trades", [])
    return data


def save_state(state: dict) -> None:
    state["updated"] = _now()
    body = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    _s3().put_object(
        Bucket=BUCKET,
        Key=STATE_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    LOCAL_SEED.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_SEED.write_text(body, encoding="utf-8")
    print(f"Wrote s3://{BUCKET}/{STATE_KEY} and {LOCAL_SEED}")


def _find_holding(state: dict, symbol: str) -> dict | None:
    sym = symbol.strip().upper()
    for h in state["holdings"]:
        if str(h.get("symbol", "")).strip().upper() == sym:
            return h
    return None


def cmd_show(state: dict) -> None:
    print(json.dumps(state, indent=2, ensure_ascii=False))
    invested = sum(
        float(h.get("qty", 0)) * float(h.get("avg_cost", 0))
        for h in state.get("holdings", [])
    )
    print(
        f"\ncash=Rs {float(state['cash']):,.2f} | "
        f"holdings cost-basis=Rs {invested:,.2f} | "
        f"names={len(state.get('holdings', []))}"
    )


def cmd_set_cash(state: dict, amount: float) -> None:
    state["cash"] = round(float(amount), 2)
    save_state(state)
    print(f"cash set to Rs {state['cash']:,.2f}")


def cmd_add(
    state: dict, symbol: str, qty: int, price: float, sleeve: str
) -> None:
    if qty <= 0 or price <= 0:
        raise SystemExit("qty and price must be positive")
    cost = qty * price
    if float(state["cash"]) + 1e-6 < cost:
        raise SystemExit(
            f"Insufficient cash Rs {state['cash']:,.2f} for Rs {cost:,.2f} buy"
        )
    existing = _find_holding(state, symbol)
    if existing:
        old_qty = int(existing["qty"])
        old_cost = float(existing["avg_cost"])
        new_qty = old_qty + qty
        existing["avg_cost"] = round((old_qty * old_cost + cost) / new_qty, 4)
        existing["qty"] = new_qty
        if sleeve:
            existing["sleeve"] = sleeve
    else:
        state["holdings"].append(
            {
                "symbol": symbol.strip().upper(),
                "qty": int(qty),
                "avg_cost": round(float(price), 4),
                "sleeve": sleeve or "legacy",
            }
        )
    state["cash"] = round(float(state["cash"]) - cost, 2)
    save_state(state)


def cmd_close(state: dict, symbol: str, qty: int, price: float) -> None:
    if qty <= 0 or price <= 0:
        raise SystemExit("qty and price must be positive")
    holding = _find_holding(state, symbol)
    if holding is None:
        raise SystemExit(f"{symbol} is not in holdings")
    held_qty = int(holding["qty"])
    if qty > held_qty:
        raise SystemExit(f"Cannot close {qty}; holding is {held_qty}")
    proceeds = qty * price
    avg = float(holding["avg_cost"])
    state["closed_trades"].append(
        {
            "symbol": symbol.strip().upper(),
            "qty": int(qty),
            "avg_cost": avg,
            "exit_price": round(float(price), 4),
            "pnl": round((price - avg) * qty, 2),
            "closed_at": _now(),
            "sleeve": holding.get("sleeve", "legacy"),
        }
    )
    remaining = held_qty - qty
    if remaining <= 0:
        state["holdings"] = [
            h
            for h in state["holdings"]
            if str(h.get("symbol", "")).strip().upper() != symbol.strip().upper()
        ]
    else:
        holding["qty"] = remaining
    state["cash"] = round(float(state["cash"]) + proceeds, 2)
    save_state(state)
    print(f"Closed {qty} {symbol} @ {price}; cash now Rs {state['cash']:,.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Print current S3 portfolio state")

    p_cash = sub.add_parser("set-cash", help="Overwrite deployable cash")
    p_cash.add_argument("amount", type=float)

    p_add = sub.add_parser("add", help="Record a buy fill (reduces cash)")
    p_add.add_argument("symbol")
    p_add.add_argument("qty", type=int)
    p_add.add_argument("price", type=float)
    p_add.add_argument(
        "--sleeve",
        default="legacy",
        choices=("core", "satellite", "legacy"),
    )

    p_close = sub.add_parser("close", help="Record a sell fill (increases cash)")
    p_close.add_argument("symbol")
    p_close.add_argument("qty", type=int)
    p_close.add_argument("price", type=float)

    args = parser.parse_args()
    state = load_state()
    if args.cmd == "show":
        cmd_show(state)
    elif args.cmd == "set-cash":
        cmd_set_cash(state, args.amount)
    elif args.cmd == "add":
        cmd_add(state, args.symbol, args.qty, args.price, args.sleeve)
    elif args.cmd == "close":
        cmd_close(state, args.symbol, args.qty, args.price)
    return 0


if __name__ == "__main__":
    sys.exit(main())
