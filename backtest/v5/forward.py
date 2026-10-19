"""Research forward CLI. It evaluates when inputs exist and stays disabled for production."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from swing_v5.execution import label_trade
from swing_v5.pipeline import run_research
from swing_v5.research_ledger import ResearchLedger
from swing_v5.sessions import IST

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clock", required=True, help="Timezone-aware ISO clock injected for this run")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--policy", default="config/v5/research_policy.json")
    parser.add_argument("--advance", action="store_true")
    args = parser.parse_args(argv)
    clock = datetime.fromisoformat(args.clock)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=IST)
    bundle = json.loads(Path(args.fixture).read_text())
    policy = json.loads((ROOT / args.policy).read_text())
    ledger = ResearchLedger(Path(args.ledger))
    if args.advance:
        import pandas as pd

        frame = pd.DataFrame(bundle["prices"])
        outcome = label_trade(frame, int(bundle.get("signal_index", len(frame) - 2)), horizon="long", qty=1)
        outcome["execution_proxy"] = "next_session_open_not_verified_0925"
        logical = __import__("swing_v5.research_ledger", fromlist=["logical_id"]).logical_id(
            {"run": "paper_exit", "symbol": (bundle.get("identity") or {}).get("nse_symbol"), "signal_index": bundle.get("signal_index")}
        )
        stored = ledger.append(logical, outcome)
        print(json.dumps({"logical_id": logical, "status": outcome.get("status"), "duplicate_skipped": stored["duplicate_skipped"], "execution_proxy": outcome["execution_proxy"]}, indent=2))
        return 0
    result = run_research(clock, bundle, ledger, policy)
    payload = result["payload"]
    print(json.dumps({
        "logical_id": result["logical_id"],
        "duplicate_skipped": result["duplicate_skipped"],
        "evaluated": payload["evaluated"],
        "evaluation_stage": payload["evaluation_stage"],
        "price_session": payload["price_session"],
        "earliest_execution_at": payload["earliest_execution_at"],
        "phase": payload["phase"],
        "paper_orders": len(payload["paper_orders"]),
        "production_eligibility": payload["production_eligibility"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
