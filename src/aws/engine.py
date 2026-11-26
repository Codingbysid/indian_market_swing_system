"""Placeholders for the five daily slots.

No HMM, GBDT, or RL model runs here. An empty signal list is intentional:
the email path stays quiet until a real engine is wired into these functions.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src.aws.schemas import PostMarketResult, PreMarketResult, SignalRunResult

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> str:
    return datetime.now(IST).isoformat()


def run_pre_market() -> PreMarketResult:
    """09:05 IST connectivity and regime context. No legacy model is called."""
    return {
        "status": "OK",
        "timestamp_ist": _now_ist(),
        "nifty_prev_close": None,
        "regime_state": "STUB_NEUTRAL",
        "universe_count": 0,
        "notes": "Clean-slate stub active. No legacy models executed.",
    }


def run_signal_engine(slot_name: str) -> SignalRunResult:
    """09:45, 12:45, and 15:30 IST. Empty signals are not a buy or sell instruction."""
    return {
        "status": "OK",
        "slot_name": slot_name,
        "timestamp_ist": _now_ist(),
        "regime_state": "STUB_NEUTRAL",
        "signals": [],
    }


def run_post_market() -> PostMarketResult:
    """16:05 IST. The URI names the intended artifact. This stub does not write it."""
    bucket = os.getenv("BUCKET_NAME", "indian-swing-bot-data-2026")
    return {
        "status": "OK",
        "timestamp_ist": _now_ist(),
        "eod_rows_ingested": 0,
        "updated_regime_state": "STUB_NEUTRAL",
        "s3_artifact_uri": f"s3://{bucket}/v2/state/latest.json",
    }
