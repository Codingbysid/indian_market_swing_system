"""Typed results for the five EventBridge slots.

These shapes are the contract for the email formatter and any later S3 writer.
HMM, GBDT, and RL outputs have to fit these fields. They do not add new ones.
"""

from __future__ import annotations

from typing import Literal, TypedDict

JobType = Literal["PRE_MARKET", "SIGNAL_RUN", "POST_MARKET"]
ActionType = Literal["BUY", "SELL", "HOLD"]


class SignalItem(TypedDict):
    symbol: str
    action: ActionType
    confidence: float
    target_weight: float
    regime_state: str
    reason: str


class PreMarketResult(TypedDict):
    status: Literal["OK", "HOLIDAY", "ERROR"]
    timestamp_ist: str
    nifty_prev_close: float | None
    regime_state: str
    universe_count: int
    notes: str


class SignalRunResult(TypedDict):
    status: Literal["OK", "HOLIDAY", "ERROR"]
    slot_name: str
    timestamp_ist: str
    regime_state: str
    signals: list[SignalItem]


class PostMarketResult(TypedDict):
    status: Literal["OK", "HOLIDAY", "ERROR"]
    timestamp_ist: str
    eod_rows_ingested: int
    updated_regime_state: str
    s3_artifact_uri: str
