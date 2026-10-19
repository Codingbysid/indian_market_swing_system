"""The forward CLI, driven by an injected clock and fixture."""

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd


def _frame():
    dates = pd.bdate_range(end="2026-10-16", periods=230)
    rows = []
    for i, day in enumerate(dates):
        rows.append({
            "Date": day.date().isoformat(),
            "Open": 100.0, "High": 101.0, "Low": 35.0, "Close": 100.0, "Volume": 1000,
            "SMA50": 90.0, "SMA200": 100.0, "EMA20": 100.0, "ATR": 2.0,
        })
    last = len(rows) - 1
    rows[last - 1]["SMA50"] = 100
    rows[last - 1]["SMA200"] = 110
    rows[last]["SMA50"] = 120
    rows[last]["SMA200"] = 110
    rows[last - 20]["SMA200"] = 100
    for row in rows[:last]:
        row["High"] = 90
    rows[last]["Close"] = 100
    rows[last]["High"] = 101
    rows[last]["Volume"] = 5000
    return rows


def _write(path: Path, cash: float, metadata: bool, holidays: list[str]):
    path.write_text(json.dumps({
        "calendar_verified": True,
        "holidays": holidays,
        "identity": {"nse_symbol": "TEST", "status": "verified_nse_link"},
        "prices": _frame(),
        "family": "trend_transition",
        "rel63": 0.2,
        "metadata_ok": metadata,
        "opening_cash": cash,
        "source_hash": "src",
        "universe_hash": "uni",
        "membership_first_seen_at": "2026-10-16T09:00:00+05:30",
    }))


def _run(clock: str, fixture: Path, ledger: Path):
    return subprocess.run(
        [sys.executable, "-m", "backtest.v5.forward", "--clock", clock, "--fixture", str(fixture), "--ledger", str(ledger)],
        check=False, capture_output=True, text=True,
    )


def test_cli_admits_then_rerun_is_duplicate_and_capital_rejects(tmp_path):
    fixture = tmp_path / "admit.json"
    poor = tmp_path / "poor.json"
    ledger = tmp_path / "research.sqlite"
    _write(fixture, 5748, True, [])
    _write(poor, 100, True, [])
    first = _run("2026-10-16T16:00:00+05:30", fixture, ledger)
    assert first.returncode == 0, first.stderr
    body = json.loads(first.stdout)
    assert body["evaluated"] is True
    assert body["paper_orders"] == 0
    assert body["evaluation_stage"] == "evaluated"
    assert body["production_eligibility"] == "disabled"
    assert body["price_session"] == "2026-10-16"
    assert body["earliest_execution_at"].startswith("2026-10-19T09:25:00")
    second = _run("2026-10-16T16:00:00+05:30", fixture, ledger)
    assert json.loads(second.stdout)["duplicate_skipped"] is True
    rejected = _run("2026-10-16T16:00:00+05:30", poor, ledger)
    assert json.loads(rejected.stdout)["paper_orders"] == 0
    assert json.loads(rejected.stdout)["evaluation_stage"] != "never_evaluated"
    advanced = subprocess.run(
        [sys.executable, "-m", "backtest.v5.forward", "--advance", "--clock", "2026-10-19T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger)],
        check=False, capture_output=True, text=True,
    )
    assert advanced.returncode == 0, advanced.stderr
    assert json.loads(advanced.stdout)["status"] == "noop"


def test_cli_sunday_and_monday_holiday(tmp_path):
    fixture = tmp_path / "sun.json"
    ledger = tmp_path / "sun.sqlite"
    _write(fixture, 5748, False, ["2026-10-19"])
    sunday = _run("2026-10-18T13:00:00+05:30", fixture, ledger)
    body = json.loads(sunday.stdout)
    assert body["price_session"] == "2026-10-16"
    assert body["earliest_execution_at"].startswith("2026-10-20")
    blocked = json.loads(sunday.stdout)
    assert blocked["paper_orders"] == 0


def test_cli_unverified_calendar_does_not_name_monday(tmp_path):
    fixture = tmp_path / "cal.json"
    fixture.write_text(json.dumps({
        "calendar_verified": False,
        "holidays": [],
        "identity": {"nse_symbol": None},
        "prices": None,
        "source_hash": "src",
        "universe_hash": "uni",
    }))
    proc = _run("2026-10-18T13:00:00+05:30", fixture, tmp_path / "c.sqlite")
    body = json.loads(proc.stdout)
    assert body["earliest_execution_at"] is None
    assert body["evaluated"] is False
    assert body["evaluation_stage"] == "blocked_unresolved_instrument"
