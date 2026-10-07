"""Sector / theme tags. Overlapping tags are tested independently, never summed."""

from __future__ import annotations

# Conservative mapped clusters for concentration gates. Missing tag ≠ diversified.
SYMBOL_TAGS: dict[str, list[str]] = {
    "HAL": ["defence", "psu"],
    "BEL": ["defence", "psu"],
    "BHEL": ["defence", "psu", "capital_goods"],
    "BEML": ["defence", "psu"],
    "MAZDOCK": ["defence", "psu"],
    "COCHINSHIP": ["defence", "psu"],
    "GRSE": ["defence", "psu"],
    "GARDENREACH": ["defence", "psu"],
    "ZENTEC": ["defence"],
    "PARAS": ["defence"],
    "DATAPATTNS": ["defence"],
    "MTARTECH": ["defence"],
    "SOLARINDS": ["defence"],
    "BPCL": ["psu", "energy"],
    "HPCL": ["psu", "energy"],
    "HINDPETRO": ["psu", "energy"],
    "IOC": ["psu", "energy"],
    "ONGC": ["psu", "energy"],
    "GAIL": ["psu", "energy"],
    "COALINDIA": ["psu", "energy"],
    "NTPC": ["psu", "energy"],
    "POWERGRID": ["psu", "energy"],
    "SBIN": ["psu", "financials"],
    "PNB": ["psu", "financials"],
    "BANKBARODA": ["psu", "financials"],
    "CANBK": ["psu", "financials"],
    "UNIONBANK": ["psu", "financials"],
    "INFY": ["it"],
    "TCS": ["it"],
    "WIPRO": ["it"],
    "HCLTECH": ["it"],
    "TECHM": ["it"],
    "LTIM": ["it"],
    "PERSISTENT": ["it"],
    "COFORGE": ["it"],
    "CDSL": ["financials"],
    "BSE": ["financials"],
    "MCX": ["financials"],
}


def tags_for(symbol: str) -> list[str]:
    return list(SYMBOL_TAGS.get(str(symbol).strip().upper(), []))
