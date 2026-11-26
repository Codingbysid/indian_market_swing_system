"""Backward split and bonus adjustments for raw NSE bhavcopy prices.

Rupee turnover is not adjusted. Prices on and after the ex-date stay as printed.
Prices strictly before the ex-date are multiplied by the cumulative factor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

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


def parse_nse_purpose_to_factor(purpose: str) -> float | None:
    """Return a price multiplier below 1, or None when the text is not a split or bonus."""
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
        if old_face > new_face > 0:
            factor *= new_face / old_face
            matched = True
    if matched and 0 < factor < 1:
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
    frame = raw.rename(columns=rename)
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame["ex_date"] = pd.to_datetime(frame["ex_date"], format="mixed", errors="coerce")
    if "factor" in frame.columns:
        frame["factor"] = pd.to_numeric(frame["factor"], errors="coerce")
    else:
        frame["factor"] = frame["purpose"].map(parse_nse_purpose_to_factor)
    return (
        frame.dropna(subset=["symbol", "ex_date", "factor"])
        .query("0 < factor < 1.0")
        .groupby(["symbol", "ex_date"], as_index=False)["factor"]
        .prod()
    )


def _backward_multiplier(factors: pd.Series) -> pd.Series:
    shifted = factors.shift(-1, fill_value=1.0)
    return shifted.iloc[::-1].cumprod().iloc[::-1]


def apply_corporate_actions(ohlcv_df: pd.DataFrame, actions_df: pd.DataFrame) -> pd.DataFrame:
    """Adjust open and close. Leave traded_value_inr unchanged."""
    frame = ohlcv_df.sort_values(["symbol", "date"]).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    use_isin = "isin" in frame.columns and frame["isin"].notna().all() and (frame["isin"].astype(str).str.len() > 0).all()
    group_key = "isin" if use_isin else "symbol"
    actions = actions_df.copy()
    actions["ex_date"] = pd.to_datetime(actions["ex_date"])
    if group_key == "isin":
        symbol_isin = frame.sort_values("date").groupby("symbol")["isin"].last()
        actions["isin"] = actions["symbol"].map(symbol_isin)
        actions = actions.dropna(subset=["isin"])
    trading_keys = frame[[group_key, "date"]].drop_duplicates().sort_values("date")
    snapped = pd.merge_asof(
        actions.sort_values("ex_date"),
        trading_keys,
        left_on="ex_date",
        right_on="date",
        by=group_key,
        direction="forward",
    ).dropna(subset=["date"])
    daily = snapped.groupby([group_key, "date"], as_index=False)["factor"].prod()
    frame = frame.merge(daily, on=[group_key, "date"], how="left")
    frame["factor"] = frame["factor"].fillna(1.0)
    calendar = frame[[group_key, "date", "factor"]].drop_duplicates([group_key, "date"]).sort_values([group_key, "date"])
    calendar["adj_multiplier"] = calendar.groupby(group_key, group_keys=False)["factor"].transform(_backward_multiplier)
    frame = frame.drop(columns=["factor"]).merge(calendar[[group_key, "date", "adj_multiplier"]], on=[group_key, "date"], how="left")
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
    """Flag overnight gaps that match a standard factor. This is a review list, not an adjustment."""
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
                }
            )
    columns = ["symbol", "ex_date", "overnight_ratio", "matched_event", "factor"]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).sort_values(["ex_date", "symbol"]).reset_index(drop=True)
