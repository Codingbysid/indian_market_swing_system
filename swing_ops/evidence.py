"""Session evidence from public NSE files. A failed or empty payload is not clearance."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime

from swing_v5.sessions import IST

PARSER_VERSION = "evidence_v2"
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
UDIFF_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={start}&to_date={end}"
BAND_URL = "https://nsearchives.nseindia.com/content/equities/eq_band_changes.csv"
RESTRICTION_ATTEMPTS = (
    "https://nsearchives.nseindia.com/content/equities/ASM_Latest_List.csv",
    "https://nsearchives.nseindia.com/content/equities/GSM_Latest_List.csv",
    "https://www.nseindia.com/api/merged-daily-reports?key=CM-Surveillance-ASM",
)
MIN_CROSS_SECTION = 20


def _now() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def _record(url: str, body: bytes | None, *, error: str | None, content_type: str | None, parsed: bool, completeness: str) -> dict:
    raw = body or b""
    return {
        "url": url,
        "retrieved_at": _now(),
        "content_type": content_type,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "error": error,
        "parsed": parsed,
        "completeness": completeness,
        "parser_version": PARSER_VERSION,
    }


def fetch_public(url: str, timeout: int = 30) -> tuple[dict, bytes | None]:
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-ops-research", "Accept": "application/json,text/csv,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            kind = response.headers.get("Content-Type")
            if response.status != 200 or not body:
                return _record(url, body, error=f"status_{response.status}", content_type=kind, parsed=False, completeness="empty_or_error"), body
            return _record(url, body, error=None, content_type=kind, parsed=False, completeness="retrieved"), body
    except Exception as exc:
        code = getattr(exc, "code", None)
        return _record(url, None, error=f"{type(exc).__name__}:{code or ''}", content_type=None, parsed=False, completeness="unavailable"), None


def parse_holiday_master(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode())
        rows = payload["CM"]
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        return {"parsed": False, "completeness": "parse_failed"}
    from datetime import datetime as dt
    dates = []
    special = []
    for row in rows:
        try:
            day = dt.strptime(row["tradingDate"], "%d-%b-%Y").date().isoformat()
        except (KeyError, TypeError, ValueError):
            return {"parsed": False, "completeness": "invalid_date"}
        dates.append(day)
        if row.get("morning_session") not in (None, "null"):
            special.append({"date": day, "morning_session": row.get("morning_session")})
    return {"parsed": True, "completeness": "cm_dates_parsed", "dates": sorted(set(dates)), "special_sessions": special, "coverage_start": min(dates), "coverage_end": max(dates)}


def parse_udiff(body: bytes, session: str) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        name = archive.namelist()[0]
        import pandas as pd
        frame = pd.read_csv(archive.open(name))
    except Exception:
        return {"parsed": False, "completeness": "parse_failed"}
    needed = {"TradDt", "ISIN", "TckrSymb", "SctySrs"}
    if not needed <= set(frame.columns):
        return {"parsed": False, "completeness": "schema_mismatch"}
    dates = set(frame["TradDt"].astype(str).str.slice(0, 10))
    if dates != {session}:
        return {"parsed": False, "completeness": "date_mismatch", "dates": sorted(dates)}
    equity = frame[frame["SctySrs"] == "EQ"]
    symbols = {}
    for row in equity.itertuples(index=False):
        symbols[str(row.TckrSymb)] = {"isin": str(row.ISIN), "series": "EQ"}
    if not symbols:
        return {"parsed": False, "completeness": "empty_eq_list"}
    return {"parsed": True, "completeness": "session_eq_list", "symbols": symbols, "eq_count": len(symbols)}


def parse_corporate_actions(body: bytes, window_start: str, window_end: str) -> dict:
    try:
        payload = json.loads(body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"parsed": False, "completeness": "parse_failed"}
    if not isinstance(payload, list):
        return {"parsed": False, "completeness": "not_a_list"}
    actions = []
    for row in payload:
        symbol = row.get("symbol")
        ex_date = row.get("exDate")
        if not symbol or not ex_date or ex_date == "-":
            continue
        try:
            from datetime import datetime as dt
            day = dt.strptime(ex_date, "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        if window_start <= day <= window_end:
            actions.append({"symbol": symbol, "ex_date": day, "subject": row.get("subject"), "isin": row.get("isin")})
    return {"parsed": True, "completeness": "parsed_list", "window_start": window_start, "window_end": window_end, "actions": actions, "count": len(actions)}


def classify_action(subject: str | None) -> str:
    """Frozen treatment. Yahoo bars are split-adjusted OHLC, not a clearance for structural events.

    structural_price (split, bonus, consolidation): block. Do not apply another ratio on top of the vendor series.
    cash_dividend: no price transformation. It is not a split. A short corporate-action window still blocks long histories.
    rights: block pending review. A rights issue is not a cash dividend and not a split.
    structural_corporate (merger, demerger, amalgamation, scheme): block. Adjusted vendor prices do not clear it.
    unrecognized: block pending review.
    """
    text = (subject or "").lower()
    if any(word in text for word in ("split", "sub-division", "subdivision", "consolidation", "bonus")):
        return "structural_price"
    if "dividend" in text:
        return "cash_dividend"
    if "right" in text:
        return "rights"
    if any(word in text for word in ("merger", "demerger", "amalgamat", "scheme of arrangement")):
        return "structural_corporate"
    return "unrecognized"


def parse_price_band_changes(body: bytes) -> dict:
    """Recent price-band changes. This file is not the full ASM/GSM universe."""
    try:
        text = body.decode()
    except UnicodeDecodeError:
        return {"parsed": False, "completeness": "parse_failed"}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or "Symbol" not in lines[0]:
        return {"parsed": False, "completeness": "schema_mismatch"}
    symbols = []
    for line in lines[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5 or not parts[1]:
            return {"parsed": False, "completeness": "malformed_row"}
        symbols.append(parts[1])
    return {
        "parsed": True,
        "completeness": "price_band_changes_not_full_surveillance_universe",
        "restricted_symbols": symbols,
        "clear_symbols": [],
    }


def symbol_evidence(*, session: str, symbol: str, calendar: dict, identity: dict, actions: dict, restrictions: dict, bar_start: str | None) -> dict:
    """Per-symbol evidence. One symbol's clear result does not apply to another."""
    blockers = []
    calendar_ok = bool(calendar.get("parsed") and calendar.get("coverage_start") and calendar.get("coverage_end") and calendar["coverage_start"] <= session <= calendar["coverage_end"])
    if not calendar_ok:
        blockers.append("calendar_unverified")
    known = (identity.get("symbols") or {}).get(symbol)
    if not known:
        blockers.append("not_in_session_eq_file")
        identity_status = "missing"
    else:
        identity_status = "verified_session_eq"
    partial_restricted = set(restrictions.get("restricted_symbols") or [])
    effective_end = restrictions.get("effective_end")
    if effective_end and effective_end < session:
        restriction_status = "stale"
        blockers.append("restrictions_stale")
    elif restrictions.get("parsed") and restrictions.get("completeness") == "complete_symbol_list":
        if symbol in partial_restricted:
            restriction_status = "restricted"
            blockers.append("restricted")
        elif symbol in set(restrictions.get("clear_symbols") or []):
            restriction_status = "clear"
        else:
            restriction_status = "unknown"
            blockers.append("restrictions_unknown")
    elif symbol in partial_restricted:
        restriction_status = "restricted"
        blockers.append("restricted")
    else:
        restriction_status = "unknown"
        blockers.append("restrictions_unknown")
    if not actions.get("parsed"):
        action_status = "unknown"
        blockers.append("corporate_actions_unknown")
    else:
        hits = [row for row in actions.get("actions") or [] if row.get("symbol") == symbol]
        kinds = {classify_action(row.get("subject")) for row in hits}
        structural = kinds & {"structural_price", "structural_corporate", "rights", "unrecognized"}
        if structural:
            action_status = "adjustment_required"
            blockers.append("corporate_action_adjustment_required")
        elif "cash_dividend" in kinds:
            action_status = "cash_dividend_only"
            if bar_start and actions.get("window_start") and bar_start < actions["window_start"]:
                blockers.append("corporate_actions_incomplete_history")
        elif bar_start and actions.get("window_start") and bar_start < actions["window_start"]:
            action_status = "incomplete_history"
            blockers.append("corporate_actions_incomplete_history")
        else:
            action_status = "none_in_window"
    return {
        "symbol": symbol,
        "session": session,
        "calendar_ok": calendar_ok and "calendar_unverified" not in blockers,
        "identity_status": identity_status,
        "isin": None if not known else known.get("isin"),
        "series": None if not known else known.get("series"),
        "restriction_status": restriction_status,
        "corporate_action_status": action_status,
        "admission_blockers": [item for item in blockers if item not in {"calendar_unverified", "not_in_session_eq_file"}],
        "evaluation_blockers": [item for item in blockers if item in {"calendar_unverified", "not_in_session_eq_file"}],
    }


def collect_session_evidence(session: str) -> dict:
    """Live collection. Unavailable surveillance lists stay unknown."""
    holiday_meta, holiday_body = fetch_public(HOLIDAY_URL)
    calendar = parse_holiday_master(holiday_body) if holiday_body else {"parsed": False, "completeness": "unavailable"}
    holiday_meta["parsed"] = bool(calendar.get("parsed"))
    holiday_meta["completeness"] = calendar.get("completeness")
    holiday_meta["effective_start"] = calendar.get("coverage_start")
    holiday_meta["effective_end"] = calendar.get("coverage_end")
    udiff_meta, udiff_body = fetch_public(UDIFF_URL.format(ymd=session.replace("-", "")))
    identity = parse_udiff(udiff_body, session) if udiff_body else {"parsed": False, "completeness": "unavailable", "symbols": {}}
    udiff_meta["parsed"] = bool(identity.get("parsed"))
    udiff_meta["completeness"] = identity.get("completeness")
    start = "01-01-2026"
    end = datetime.fromisoformat(session).strftime("%d-%m-%Y")
    action_url = ACTIONS_URL.format(start=start, end=end)
    action_meta, action_body = fetch_public(action_url)
    actions = parse_corporate_actions(action_body, "2026-01-01", session) if action_body else {"parsed": False, "completeness": "unavailable", "actions": []}
    action_meta["parsed"] = bool(actions.get("parsed"))
    action_meta["completeness"] = actions.get("completeness")
    action_meta["effective_start"] = "2026-01-01"
    action_meta["effective_end"] = session
    band_meta, band_body = fetch_public(BAND_URL)
    band = parse_price_band_changes(band_body) if band_body else {"parsed": False, "completeness": "unavailable", "restricted_symbols": []}
    band_meta["parsed"] = bool(band.get("parsed"))
    band_meta["completeness"] = band.get("completeness")
    attempts = [band_meta]
    for url in RESTRICTION_ATTEMPTS:
        meta, body = fetch_public(url)
        text = (body or b"")[:120].decode("utf-8", errors="replace")
        if not meta["error"] and "No Data Found" in text:
            meta["parsed"] = False
            meta["completeness"] = "empty_report_not_a_clearance_list"
        attempts.append(meta)
    restrictions = {
        "parsed": bool(band.get("parsed")),
        "completeness": band.get("completeness") or "no_complete_list",
        "restricted_symbols": band.get("restricted_symbols") or [],
        "clear_symbols": [],
        "attempts": attempts,
        "surveillance_universe": "unavailable",
        "effective_end": None,
    }
    archives = []
    for name, meta, body in (
        ("holiday_master", holiday_meta, holiday_body),
        ("udiff", udiff_meta, udiff_body),
        ("corporate_actions", action_meta, action_body),
        ("price_band_changes", band_meta, band_body),
    ):
        if body:
            archives.append({"name": name, "sha256": meta["sha256"], "content_type": meta.get("content_type"), "body": body})
    return {
        "session": session,
        "retrieved_at": _now(),
        "parser_version": PARSER_VERSION,
        "calendar": {**calendar, "source": holiday_meta},
        "identity": {**identity, "source": udiff_meta, "symbols": identity.get("symbols") or {}},
        "actions": {**actions, "source": action_meta},
        "restrictions": restrictions,
        "sources": [holiday_meta, udiff_meta, action_meta, *attempts],
        "archives": archives,
    }
