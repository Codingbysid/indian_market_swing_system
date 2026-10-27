"""Offline runner. Refuses broker and paper book paths and does not send alerts."""

from __future__ import annotations

import argparse
from pathlib import Path

from research_v6.experiments import run, write_baseline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run v6 research experiments locally")
    parser.add_argument("--out", default="artifacts/research_v6")
    parser.add_argument("--baseline-tests", type=int, default=171)
    parser.add_argument("--refresh-sectors", action="store_true")
    args = parser.parse_args()
    destination = Path(args.out)
    blocked = {"portfolio_state.json", "paper_book.json"}
    if destination.name in blocked or any(part in {"portfolio_state.json", "paper_book.json"} for part in destination.parts):
        raise SystemExit("refusing to write a broker or paper book")
    write_baseline(destination, args.baseline_tests)
    run(destination, refresh_sectors=args.refresh_sectors)


if __name__ == "__main__":
    main()
