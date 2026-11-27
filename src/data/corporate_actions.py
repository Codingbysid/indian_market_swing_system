"""Official NSE split/bonus adjustments. Heuristic gaps are audit-only."""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

COMMON_NSE_FACTORS: dict[str, float] = {
    "1:1 Bonus / 2:1 Split": 1 / 2,
    "1:2 Bonus (3 for 2)": 2 / 3,
    "2:1 Bonus (3 for 1)": 1 / 3,
    "1:3 Bonus (4 for 3)": 3 / 4,
    "3:1 Bonus (4 for 1)": 1 / 4,
    "1:4 Bonus (5 for 4)": 4 / 5,
    "5:1 Split (FV 10->2)": 1 / 5,
    "10:1 Split (FV 10->1)": 1 / 10,
    "2:3 Bonus (5 for 3)": 3 / 5,
}

ADJUSTMENTS_CSV_URL = "https://stocks.events/data/adjustments.csv"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*",
}


def parse_nse_purpose_to_factor(purpose: str) -> float | None:
    """Return a price multiplier, or None when the text is not a split or bonus."""
    if not isinstance(purpose, str):
        return None
    text = purpose.upper()
    factor = 1.0
    matched = False
    bonus = re.search(r"BONUS\s+(\d+)\s*:\s*(\d+)", text)
    if bonus:
        new_shares, held_shares = int(bonus.group(1)), int(bonus.group(2))
        if new_shares > 0 and held_shares > 0:
            factor *= held_shares / (held_shares + new_shares)
            matched = True
    split = re.search(
        r"(?:SPLIT|SUB-DIVISION).*?FROM\s+(?:RS\.?|RE\.?|INR)?\s*(\d+(?:\.\d+)?)\s*/?-?.*?"
        r"TO\s+(?:RS\.?|RE\.?|INR)?\s*(\d+(?:\.\d+)?)",
        text,
    )
    if split:
        old_face, new_face = float(split.group(1)), float(split.group(2))
        if old_face > 0 and new_face > 0 and old_face != new_face:
            factor *= new_face / old_face
            matched = True
    if matched and factor > 0 and abs(factor - 1.0) > 1e-9:
        return round(factor, 8)
    return None


def load_nse_corporate_actions(csv_path: Path) -> pd.DataFrame:
    raw = pd.read_csv(csv_path, dtype=str)
    raw.columns = [column.strip().lower() for column in raw.columns]
    rename = {}
    for column in raw.columns:
        if column in {"symbol", "tckrsymb"}:
            rename[column] = "symbol"
        elif column in {"ex-date", "ex_date", "exdate"}:
            rename[column] = "ex_date"
        elif column in {"purpose", "subject"}:
            rename[column] = "purpose"
        elif column == "factor":
            rename[column] = "factor"
        elif column == "isin":
            rename[column] = "isin"
    frame = raw.rename(columns=rename)
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame["ex_date"] = pd.to_datetime(frame["ex_date"], format="mixed", errors="coerce")
    if "factor" in frame.columns:
        frame["factor"] = pd.to_numeric(frame["factor"], errors="coerce")
    else:
        frame["factor"] = frame["purpose"].map(parse_nse_purpose_to_factor)
    keep = ["symbol", "ex_date", "factor"]
    if "isin" in frame.columns:
        keep.append("isin")
    return (
        frame.dropna(subset=["symbol", "ex_date", "factor"])
        .query("factor > 0 and abs(factor - 1.0) > 1e-9")
        [keep]
        .drop_duplicates()
        .reset_index(drop=True)
    )


def fetch_official_split_bonus_actions(
    *,
    start: str = "2016-01-01",
    end: str = "2026-11-26",
    cache_path: Path | None = None,
    exchanges_must_include: str = "NSE",
) -> pd.DataFrame:
    """Official bonus / split / consolidation factors (stocks.events, NSE lineage).

    Never uses heuristic overnight-gap detection. Returns columns:
    symbol, isin, ex_date, factor, type, detail, source.
    """
    if cache_path is not None and cache_path.exists():
        frame = pd.read_parquet(cache_path)
        frame["ex_date"] = pd.to_datetime(frame["ex_date"])
        return frame

    resp = requests.get(ADJUSTMENTS_CSV_URL, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    raw = pd.read_csv(io.StringIO(resp.text))
    raw["ex_date"] = pd.to_datetime(raw["ex_date"], errors="coerce")
    raw["factor"] = pd.to_numeric(raw["factor"], errors="coerce")
    raw["type"] = raw["type"].astype(str).str.lower().str.strip()
    exch = raw["exchanges"].astype(str)
    mask = (
        raw["ex_date"].between(pd.Timestamp(start), pd.Timestamp(end))
        & raw["type"].isin({"bonus", "split", "consolidation"})
        & exch.str.contains(exchanges_must_include, case=False, na=False)
        & raw["factor"].notna()
        & (raw["factor"] > 0)
        & (raw["factor"] - 1.0).abs() > 1e-9
    )
    frame = raw.loc[mask, ["isin", "symbol", "ex_date", "factor", "type", "detail"]].copy()
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame["isin"] = frame["isin"].astype(str).str.strip()
    frame["source"] = "stocks.events/adjustments.csv"
    frame = (
        frame.dropna(subset=["ex_date", "factor"])
        .sort_values(["ex_date", "symbol"])
        .drop_duplicates(["isin", "ex_date", "factor"], keep="last")
        .reset_index(drop=True)
    )
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(cache_path, index=False)
    logger.info("Official NSE split/bonus actions: %d rows", len(frame))
    return frame


def _backward_multiplier(factors: pd.Series) -> pd.Series:
    shifted = factors.shift(-1, fill_value=1.0)
    return shifted.iloc[::-1].cumprod().iloc[::-1]


def apply_corporate_actions(ohlcv_df: pd.DataFrame, actions_df: pd.DataFrame) -> pd.DataFrame:
    """Adjust open and close. Leave traded_value_inr unchanged.

    NSE face-value splits often mint a new ISIN on the ex-date, so matching
    actions to the panel by ISIN alone silently drops real events. We therefore
    apply by ``symbol`` (ticker on the exchange), while still accepting an
    optional ``isin`` column for audit. Supports factors < 1 (split/bonus) and
    > 1 (consolidation).
    """
    if actions_df is None or actions_df.empty:
        return ohlcv_df.sort_values(["date", "symbol"]).reset_index(drop=True)

    frame = ohlcv_df.sort_values(["symbol", "date"]).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["symbol"] = frame["symbol"].astype(str).str.strip()

    actions = actions_df.copy()
    actions["ex_date"] = pd.to_datetime(actions["ex_date"])
    actions["factor"] = pd.to_numeric(actions["factor"], errors="coerce")
    actions["symbol"] = actions["symbol"].astype(str).str.strip()
    actions = actions.dropna(subset=["symbol", "ex_date", "factor"]).query(
        "factor > 0 and abs(factor - 1.0) > 1e-9"
    )
    panel_symbols = set(frame["symbol"].unique())
    actions = actions[actions["symbol"].isin(panel_symbols)]
    if actions.empty:
        return frame.sort_values(["date", "symbol"]).reset_index(drop=True)

    # Multiple official rows can share an ex-date (bonus+split same day) → product
    actions = actions.groupby(["symbol", "ex_date"], as_index=False)["factor"].prod()

    trading_keys = frame[["symbol", "date"]].drop_duplicates().sort_values("date")
    snapped = pd.merge_asof(
        actions.sort_values("ex_date"),
        trading_keys,
        left_on="ex_date",
        right_on="date",
        by="symbol",
        direction="forward",
    ).dropna(subset=["date"])
    daily = snapped.groupby(["symbol", "date"], as_index=False)["factor"].prod()
    frame = frame.merge(daily, on=["symbol", "date"], how="left")
    frame["factor"] = frame["factor"].fillna(1.0)
    calendar = (
        frame[["symbol", "date", "factor"]]
        .drop_duplicates(["symbol", "date"])
        .sort_values(["symbol", "date"])
    )
    calendar["adj_multiplier"] = calendar.groupby("symbol", group_keys=False)["factor"].transform(
        _backward_multiplier
    )
    frame = frame.drop(columns=["factor"]).merge(
        calendar[["symbol", "date", "adj_multiplier"]], on=["symbol", "date"], how="left"
    )
    turnover = frame["traded_value_inr"].copy()
    frame["open"] = frame["open"] * frame["adj_multiplier"]
    frame["close"] = frame["close"] * frame["adj_multiplier"]
    frame["traded_value_inr"] = turnover
    return frame.drop(columns=["adj_multiplier"]).sort_values(["date", "symbol"]).reset_index(drop=True)


def detect_unadjusted_splits(
    ohlcv_df: pd.DataFrame,
    max_gap_ratio: float = 0.78,
    ratio_tolerance: float = 0.04,
) -> pd.DataFrame:
    """Flag overnight gaps that match a standard factor. Audit list only — never auto-apply."""
    frame = ohlcv_df.sort_values(["symbol", "date"]).copy()
    frame["prev_close"] = frame.groupby("symbol")["close"].shift(1)
    frame["overnight_ratio"] = frame["open"] / frame["prev_close"]
    suspects = frame[frame["overnight_ratio"] <= max_gap_ratio].dropna(subset=["overnight_ratio"])
    rows = []
    for row in suspects.itertuples(index=False):
        best_label, best_factor, best_error = None, None, float("inf")
        for label, target in COMMON_NSE_FACTORS.items():
            error = abs(float(row.overnight_ratio) - target) / target
            if error < best_error:
                best_label, best_factor, best_error = label, target, error
        if best_label is not None and best_error <= ratio_tolerance:
            rows.append(
                {
                    "symbol": row.symbol,
                    "ex_date": row.date,
                    "overnight_ratio": round(float(row.overnight_ratio), 4),
                    "matched_event": best_label,
                    "factor": best_factor,
                    "traded_value_inr": float(getattr(row, "traded_value_inr", float("nan"))),
                }
            )
    columns = ["symbol", "ex_date", "overnight_ratio", "matched_event", "factor", "traded_value_inr"]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).sort_values(["ex_date", "symbol"]).reset_index(drop=True)
