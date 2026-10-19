"""Resolve a Screener company page from its own NSE or BSE quote link."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


def _links(html: str) -> list[str]:
    import re

    return re.findall(r'href="(https://www\.nseindia\.com[^"]+|https://www\.bseindia\.com[^"]+)"', html)


def parse_company_page(html: str, url: str) -> dict:
    nse = []
    bse = []
    for href in _links(html):
        parsed = urlparse(href)
        host = (parsed.hostname or "").lower()
        if host == "www.nseindia.com" and parsed.path.rstrip("/").endswith("/get-quotes/equity"):
            symbol = parse_qs(parsed.query).get("symbol", [None])[0]
            if symbol:
                nse.append(symbol)
        elif host == "www.bseindia.com" and parsed.path.startswith("/stock-share-price/"):
            parts = [part for part in parsed.path.split("/") if part]
            if parts and parts[-1].isdigit():
                bse.append(parts[-1])
    nse = list(dict.fromkeys(nse))
    bse = list(dict.fromkeys(bse))
    if len(nse) == 1:
        status = "verified_nse_link"
    elif len(nse) > 1:
        status = "ambiguous"
    elif len(bse) == 1:
        status = "verified_bse_only"
    else:
        status = "unresolved"
    return {
        "source_url": url,
        "nse_symbol": nse[0] if status == "verified_nse_link" else None,
        "candidate_symbols": nse,
        "bse_code": bse[0] if len(bse) == 1 else None,
        "status": status,
        "evidence": "nse_get_quotes_equity_link" if nse else "no_nse_equity_link",
        "page_isin": _isin(html),
    }


def _isin(html: str) -> str | None:
    found = re.findall(r"\bIN[A-Z0-9]{10}\b", html)
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else None


def classify_against_official(parsed: dict, official: dict[str, str]) -> dict:
    """official maps NSE symbol to ISIN. Names are never used."""
    row = dict(parsed)
    symbol = row.get("nse_symbol")
    if row["status"] == "fetch_failed":
        return row
    if row["status"] != "verified_nse_link":
        return row
    official_isin = official.get(symbol or "")
    page_isin = row.get("page_isin")
    row["official_isin"] = official_isin
    if official_isin is None:
        row["status"] = "unresolved"
        row["evidence"] = "nse_link_absent_from_official_eq_file"
        row["nse_symbol"] = None
        return row
    if page_isin and page_isin != official_isin:
        row["status"] = "ambiguous"
        row["nse_symbol"] = None
        row["evidence"] = "page_isin_disagrees_with_official_isin"
        return row
    row["status"] = "verified_nse"
    row["evidence"] = "nse_equity_link_and_official_isin" if page_isin else "nse_equity_link_and_official_symbol_isin_not_on_page"
    return row
