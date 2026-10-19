"""Map a Screener company page to an NSE symbol only from the page's own NSE link."""

from __future__ import annotations

import re


def parse_company_page(html: str, url: str) -> dict:
    symbols = sorted(set(re.findall(r"symbol=([A-Z0-9&.\-]+)", html)))
    bse = re.findall(r"BSE:\s*([0-9]{6})", html)
    if len(symbols) == 1:
        status = "verified_nse_link"
    elif len(symbols) > 1:
        status = "ambiguous"
    elif bse:
        status = "bse_only"
    else:
        status = "unresolved"
    return {
        "source_url": url,
        "nse_symbol": symbols[0] if status == "verified_nse_link" else None,
        "candidate_symbols": symbols,
        "bse_code": bse[0] if len(bse) == 1 else None,
        "status": status,
    }
