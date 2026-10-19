"""Admitted paper orders through the real CLI. The wide risk file is test-only."""

from swing_v5.identity import classify_against_official, parse_company_page

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd


def _series(kind: str):
    dates = list(pd.bdate_range(end="2026-10-16", periods=230)) + [pd.Timestamp("2026-10-19"), pd.Timestamp("2026-10-20"), pd.Timestamp("2026-10-21")]
    rows = []
    for day in dates:
        rows.append({
            "Date": pd.Timestamp(day).date().isoformat(),
            "Open": 100.0, "High": 100.0, "Low": 80.0, "Close": 100.0, "Volume": 1000,
            "SMA50": 95.0, "SMA200": 90.0, "EMA20": 99.0, "ATR": 10.0,
        })
    last = 229
    if kind == "long":
        rows[last - 1]["SMA50"] = 100
        rows[last - 1]["SMA200"] = 110
        rows[last]["SMA50"] = 120
        rows[last]["SMA200"] = 110
        rows[last - 20]["SMA200"] = 100
        for row in rows[:last]:
            row["High"] = 90
        rows[last]["High"] = 101
        rows[last]["Volume"] = 5000
    else:
        rows[last - 5]["SMA50"] = 90
        for row in rows[:last]:
            row["High"] = 90
        rows[last]["Low"] = 98.5
        rows[last]["High"] = 101
        rows[last]["Volume"] = 5000
        rows[last]["EMA20"] = 99.5
    rows[230]["Open"] = 100.2
    rows[231]["Open"] = 100.0
    rows[231]["Low"] = 70.0
    rows[232]["SMA50"] = 10
    rows[232]["Close"] = 10
    return rows


def _bundle(path: Path):
    path.write_text(json.dumps({
        "calendar_verified": True,
        "holidays": [],
        "opening_cash": 5748,
        "opening_source": "october17_reference",
        "source_hash": "src",
        "universe_hash": "uni",
        "membership_first_seen_at": "2026-10-16T09:00:00+05:30",
        "symbols": [
            {"identity": {"nse_symbol": "ALPHA", "status": "verified_nse_link"}, "family": "trend_transition", "rel63": 0.2, "metadata_ok": True, "prices": _series("long")},
            {"identity": {"nse_symbol": "BETA", "status": "verified_nse_link"}, "family": "quality_continuation", "member": True, "rel63": 0.2, "metadata_ok": True, "prices": _series("short")},
        ],
    }))


def _run(args):
    return subprocess.run([sys.executable, "-m", "backtest.v5.forward", *args], check=False, capture_output=True, text=True)


def test_admitted_order_fills_and_exits_and_second_symbol_shares_risk(tmp_path):
    fixture = tmp_path / "book.json"
    ledger = tmp_path / "book.sqlite"
    _bundle(fixture)
    policy = "config/v5/test_only_mechanics.json"
    first = _run(["--mode", "fixture", "--clock", "2026-10-16T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger), "--policy", policy])
    assert first.returncode == 0, first.stderr
    body = json.loads(first.stdout)
    assert body["paper_orders"] == 1
    assert body["evaluated"] is True
    again = _run(["--mode", "fixture", "--clock", "2026-10-16T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger), "--policy", policy])
    assert json.loads(again.stdout)["duplicate_skipped"] is True
    filled = _run(["--mode", "fixture", "--advance", "--clock", "2026-10-19T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger), "--policy", policy])
    assert json.loads(filled.stdout)["status"] == "advanced"
    exited = _run(["--mode", "fixture", "--advance", "--clock", "2026-10-20T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger), "--policy", policy])
    assert json.loads(exited.stdout)["status"] == "advanced"
    rows = sqlite3.connect(ledger).execute("SELECT payload FROM events").fetchall()
    kinds = [json.loads(row[0]).get("kind") for row in rows]
    assert "paper_fill" in kinds
    assert "paper_exit" in kinds
    assert kinds.count("paper_order") == 1


def test_exchange_link_parser_ignores_unrelated_symbol_text():
    html = """
    <a href="https://www.nseindia.com/get-quotes/derivatives?symbol=OTHER">deriv</a>
    <a href="https://www.nseindia.com/get-quotes/equity?symbol=SUNTV">NSE</a>
    <a href="https://www.bseindia.com/stock-share-price/x/y/532733/?scripcode=532733">BSE</a>
    <p>symbol=SUNTV&amp;junk</p>
    """
    parsed = parse_company_page(html, "https://www.screener.in/company/SUNTV/")
    assert parsed["status"] == "verified_nse_link"
    assert parsed["nse_symbol"] == "SUNTV"
    official = classify_against_official(parsed, {"SUNTV": "INE424H01027"})
    assert official["status"] == "verified_nse"
    assert official["official_isin"] == "INE424H01027"
    bse = parse_company_page('<a href="https://www.bseindia.com/stock-share-price/palm-jewels-ltd/PALMJEWELS/541444/">BSE</a>', "https://www.screener.in/company/541444/")
    assert bse["status"] == "verified_bse_only"
    assert bse["bse_code"] == "541444"


def test_rejected_and_empty_books_do_not_invent_trades(tmp_path):
    fixture = tmp_path / "wide.json"
    ledger = tmp_path / "wide.sqlite"
    _bundle(fixture)
    raw = json.loads(fixture.read_text())
    for item in raw["symbols"]:
        for row in item["prices"]:
            row["Low"] = 35
    fixture.write_text(json.dumps(raw))
    proc = _run(["--mode", "fixture", "--clock", "2026-10-16T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger)])
    assert json.loads(proc.stdout)["paper_orders"] == 0
    advanced = _run(["--mode", "fixture", "--advance", "--clock", "2026-10-19T16:00:00+05:30", "--fixture", str(fixture), "--ledger", str(ledger)])
    assert json.loads(advanced.stdout)["status"] == "noop"
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"calendar_verified": True, "holidays": [], "symbols": [], "source_hash": "x", "universe_hash": "y"}))
    quiet = _run(["--mode", "fixture", "--advance", "--clock", "2026-10-19T16:00:00+05:30", "--fixture", str(empty), "--ledger", str(tmp_path / "empty.sqlite")])
    assert json.loads(quiet.stdout)["status"] == "noop"
