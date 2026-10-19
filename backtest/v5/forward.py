"""Research forward CLI. Live mode uses the wall clock. Replay and fixture modes require an injected clock."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from swing_v5.advance import advance_book
from swing_v5.pipeline import run_research
from swing_v5.research_ledger import ResearchLedger
from swing_v5.sessions import IST

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "replay", "fixture"], default="fixture")
    parser.add_argument("--clock", default=None, help="Required for replay and fixture. Live mode ignores this and uses the wall clock.")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--policy", default="config/v5/research_policy.json")
    parser.add_argument("--advance", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "live":
        clock = datetime.now(IST)
    else:
        if not args.clock:
            raise SystemExit(f"{args.mode} mode requires --clock; it is not a live run")
        clock = datetime.fromisoformat(args.clock)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=IST)
    bundle = json.loads(Path(args.fixture).read_text())
    bundle["run_mode"] = args.mode
    policy = json.loads((ROOT / args.policy).read_text())
    ledger = ResearchLedger(Path(args.ledger))
    if args.advance:
        outcome = advance_book(clock, bundle, ledger)
        outcome["mode"] = args.mode
        print(json.dumps(outcome, indent=2))
        return 0
    result = run_research(clock, bundle, ledger, policy)
    payload = result["payload"]
    print(json.dumps({
        "mode": args.mode,
        "logical_id": result["logical_id"],
        "duplicate_skipped": result["duplicate_skipped"],
        "evaluated": payload["evaluated"],
        "evaluation_stage": payload["evaluation_stage"],
        "price_session": payload["price_session"],
        "earliest_execution_at": payload["earliest_execution_at"],
        "phase": payload["phase"],
        "paper_orders": len(payload["paper_orders"]),
        "blocked": payload.get("blocked", []),
        "production_eligibility": payload["production_eligibility"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
