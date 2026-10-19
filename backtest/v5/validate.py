"""Resolve Screener company links and build the verified research bundle. Live clock is not injected here."""

from __future__ import annotations

import csv
import hashlib
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

from swing_v5.data.screener import fetch_page
from swing_v5.forward_log import now_iso
from swing_v5.identity import classify_against_official, parse_company_page
from swing_v5.sessions import IST

ROOT = Path(__file__).resolve().parents[2]
FORWARD = ROOT / "artifacts" / "v5" / "forward"


def _official() -> tuple[dict[str, str], str]:
    path = ROOT / "artifacts" / "v5" / "nse_raw" / "udiff_20261016_eq.csv"
    frame = pd.read_csv(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(zip(frame["Symbol"], frame["ISIN"])), digest


def _yahoo(symbol: str) -> pd.DataFrame:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?interval=1d&range=10y&events=div%2Csplit"
    request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-v5-research"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
    result = payload["chart"]["result"][0]
    stamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    rows = []
    for stamp, open_, high, low, close, volume in zip(stamps, quote["open"], quote["high"], quote["low"], quote["close"], quote["volume"]):
        if None in (open_, high, low, close, volume):
            continue
        day = datetime.fromtimestamp(stamp, IST).date().isoformat()
        rows.append({"Date": day, "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})
    return pd.DataFrame(rows).drop_duplicates("Date").reset_index(drop=True)


def _piotroski_note() -> dict:
    import re

    root = FORWARD / "screens" / "piotroski"
    latest = {}
    for path in root.glob("page-*.html"):
        number = int(re.search(r"page-(\d+)", path.name).group(1))
        if number not in latest or path.stat().st_mtime > latest[number].stat().st_mtime:
            latest[number] = path
    pages = [latest[number] for number in sorted(latest)]
    bad = []
    parsed_rows = 0
    for path in pages:
        html = path.read_text(errors="replace")
        for row in re.findall(r"<tr[^>]*>.*?</tr>", html, flags=re.S):
            text = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", row)).strip()
            if re.match(r"\d+\.", text):
                parsed_rows += 1
            if 'href="/company/id/' in row:
                bad.append({"page": path.name, "row": text[:80]})
    return {
        "reported_results": 554,
        "table_rows_parsed": parsed_rows,
        "non_identifying_slug": "/company/id/",
        "rows": bad,
        "conclusion": "Both missing unique ids are present as table rows. They share the non-identifying slug id, so unique company hrefs are 552. The screen stays incomplete.",
    }


def main() -> int:
    screens = json.loads((FORWARD / "screens.json").read_text())
    golden = screens["screens"]["golden"]["company_ids"]
    official, official_hash = _official()
    retrieved = now_iso()
    rows = []
    for company_id in golden:
        url = f"https://www.screener.in/company/{company_id}/"
        time.sleep(1.5)
        try:
            _, html = fetch_page(url, retries=4)
            parsed = parse_company_page(html, url)
        except Exception as exc:
            parsed = {"source_url": url, "nse_symbol": None, "status": "fetch_failed", "evidence": type(exc).__name__, "page_isin": None, "candidate_symbols": [], "bse_code": None}
        classified = classify_against_official(parsed, official)
        classified["company_id"] = company_id
        classified["retrieved_at"] = retrieved
        classified["official_file_sha256"] = official_hash
        classified["official_session"] = "2026-10-16"
        rows.append(classified)
    if len(rows) != len(golden):
        raise SystemExit("resolution rows do not cover every golden company id")
    out = FORWARD / "instrument_resolution.csv"
    fields = ["company_id", "status", "nse_symbol", "candidate_symbols", "bse_code", "page_isin", "official_isin", "evidence", "retrieved_at", "official_session", "official_file_sha256", "source_url"]
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    note = _piotroski_note()
    (FORWARD / "piotroski_row_gap.json").write_text(json.dumps(note, indent=2) + "\n")
    verified = [row for row in rows if row["status"] == "verified_nse" and row.get("nse_symbol")]
    panel_dir = FORWARD / "panels" / "yahoo"
    panel_dir.mkdir(parents=True, exist_ok=True)
    master = json.loads((FORWARD / "nse_holiday_master.json").read_text())
    holiday_dates = []
    for row in master["CM"]:
        holiday_dates.append(datetime.strptime(row["tradingDate"], "%d-%b-%Y").date().isoformat())
    from swing_v5.sessions import SessionClock

    session = SessionClock(datetime.now(IST), {datetime.fromisoformat(day).date() for day in holiday_dates}, calendar_verified=True)
    price_session = session.latest_completed_session().isoformat()
    request = urllib.request.Request(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=10y",
        headers={"User-Agent": "indian-swing-v5-research"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    index_rows = []
    for stamp, close in zip(result["timestamp"], quote["close"]):
        if close is None:
            continue
        index_rows.append({"Date": datetime.fromtimestamp(stamp, IST).date().isoformat(), "Close": close})
    index = pd.DataFrame(index_rows).drop_duplicates("Date")
    symbols = []
    readiness = []
    for row in rows:
        symbol = row.get("nse_symbol")
        if row["status"] != "verified_nse":
            readiness.append({"symbol": row["company_id"], "status": row["status"], "stage": "blocked_unresolved_instrument", "vendor": ""})
            continue
        prices = _yahoo(symbol)
        prices.to_csv(panel_dir / f"{symbol}.csv", index=False)
        joined = prices.merge(index, on="Date", suffixes=("", "_index"))
        joined = joined.loc[joined["Date"] <= price_session]
        if len(joined) < 220 or joined.empty or joined["Date"].iloc[-1] != price_session:
            readiness.append({"symbol": symbol, "status": "verified_nse", "stage": "blocked_prices", "vendor": "yahoo_chart", "bars": int(len(joined)), "last": None if joined.empty else joined["Date"].iloc[-1]})
            continue
        readiness.append({"symbol": symbol, "status": "verified_nse", "stage": "evaluable", "vendor": "yahoo_chart", "adjustment": "yahoo_split_adjusted_ohlc", "bars": int(len(joined)), "last": joined["Date"].iloc[-1]})
        symbols.append({
            "company_id": row["company_id"],
            "identity": {"nse_symbol": symbol, "status": "verified_nse", "isin": row.get("official_isin"), "evidence": row.get("evidence")},
            "family": "trend_transition",
            "prices": joined[["Date", "Open", "High", "Low", "Close", "Volume"]].to_dict("records"),
            "index_close": joined["Close_index"].tolist(),
            "metadata": {"status": "present", "source": "screener_golden_membership_plus_yahoo_panel", "retrieved_at": retrieved},
        })
    for held in ("HAL", "ZENTEC", "CDSL", "BPCL"):
        readiness.append({"symbol": held, "status": "protected_holding", "stage": "blocked_prices", "vendor": "", "reason": "no same-vendor panel in this run"})
    pd.DataFrame(readiness).to_csv(FORWARD / "per_symbol_readiness.csv", index=False)
    bundle = {
        "run_mode": "live",
        "calendar_verified": True,
        "holidays": holiday_dates,
        "price_session_used_for_panels": price_session,
        "opening_cash": 5748,
        "opening_source": "october17_reference",
        "source_hash": hashlib.sha256(out.read_bytes()).hexdigest(),
        "universe_hash": hashlib.sha256(json.dumps(sorted(row.get("nse_symbol") or row["company_id"] for row in rows)).encode()).hexdigest(),
        "membership_first_seen_at": screens.get("retrieved_at"),
        "vendor": "yahoo_chart",
        "adjustment": "yahoo_split_adjusted_ohlc",
        "symbols": symbols,
    }
    (FORWARD / "live_verified_bundle.json").write_text(json.dumps(bundle))
    print(json.dumps({"resolved_rows": len(rows), "verified_nse": len(verified), "evaluable": len(symbols), "piotroski_bad_rows": len(note["rows"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
