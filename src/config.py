"""Central dataclasses for S3 paths, NSE delivery costs, universe, HMM, and GBDT."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


SectorTier = Literal["sector", "macro_sector"]


@dataclass(frozen=True)
class S3Paths:
    bucket_env: str = "BUCKET_NAME"
    # Prefer ap-south-1 colocated bucket for NSE latency (legacy eu-north-1 bucket kept for archive).
    default_bucket: str = "nse-quant-swing-data-ap-south-1"
    state_key: str = "v2/state/latest.json"
    universe_key: str = "v2/universes/latest_150.json"
    ohlcv_raw_prefix: str = "v2/clean/ohlcv_raw_partitioned"
    ohlcv_adj_prefix: str = "v2/clean/ohlcv_adj_partitioned"
    regime_key: str = "v2/state/regime_latest.parquet"
    oof_scores_key: str = "v2/models/gbdt_oof_latest.parquet"
    models_prefix: str = "v2/models/latest/"
    features_key: str = "v2/state/latest_features.json"


@dataclass(frozen=True)
class IndiaDeliveryCostConfig:
    """NSE equity delivery charges in basis points of notional.

    Defaults sum to ~32.3 bps round-trip with 5 bps/leg slippage.
    Stress uses 10 bps/leg slippage (~42.3 bps RT).
    """

    stt_buy_bps: float = 10.0
    stt_sell_bps: float = 10.0
    stamp_buy_bps: float = 1.5
    exchange_sebi_gst_rt_bps: float = 0.8
    slippage_per_leg_bps: float = 5.0
    stress_slippage_per_leg_bps: float = 10.0

    @property
    def statutory_rt_bps(self) -> float:
        return self.stt_buy_bps + self.stt_sell_bps + self.stamp_buy_bps + self.exchange_sebi_gst_rt_bps

    @property
    def default_rt_bps(self) -> float:
        return self.statutory_rt_bps + 2.0 * self.slippage_per_leg_bps

    @property
    def stress_rt_bps(self) -> float:
        return self.statutory_rt_bps + 2.0 * self.stress_slippage_per_leg_bps


@dataclass(frozen=True)
class UniverseParams:
    lookback_days: int = 126
    min_trading_days: int = 90
    pool_size_c: int = 300
    target_size: int = 150
    max_per_sector: int = 15
    sector_col: SectorTier = "sector"


@dataclass(frozen=True)
class PortfolioLimits:
    initial_capital: float = 1_000_000.0
    max_open_positions: int = 10
    max_new_entries_per_week: int = 2
    max_positions_per_sector: int = 2
    max_new_entries_per_sector_per_week: int = 1
    hold_weeks: int = 4  # used when exit_mode == "fixed_weeks"
    # Rank-buffer / trailing-stop exit (lower turnover)
    exit_mode: Literal["fixed_weeks", "rank_buffer"] = "fixed_weeks"
    rank_buffer_top_n: int = 60
    trailing_stop_pct: float = 0.12
    # If > 0, trailing stop distance = atr_mult * atr_pct_14d (overrides fixed %)
    trailing_stop_atr_mult: float = 0.0
    max_hold_weeks: int = 12
    rank_exit_confirm_weeks: int = 2  # require N consecutive weeks outside buffer
    # Post-hoc idle-cash credit (LiquidBees-style); does not change trade path
    idle_cash_yield_annual: float = 0.0


@dataclass(frozen=True)
class HmmParams:
    train_window_days: int = 504
    refit_every_n_days: int = 21
    vol_lookback_days: int = 20
    n_iter: int = 200
    random_state: int = 42
    block_bear_entries: bool = True
    max_bear_prob_for_entry: float = 0.65


@dataclass(frozen=True)
class GbdtParams:
    forward_horizon_days: int = 20
    train_min_days: int = 756
    step_days: int = 63
    embargo_days: int = 20
    objective: str = "regression"
    num_leaves: int = 31
    learning_rate: float = 0.03
    n_estimators: int = 400
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_samples: int = 50
    random_state: int = 42
    top_k: int = 5


@dataclass(frozen=True)
class RlParams:
    top_k_candidates: int = 5
    hold_steps: int = 4
    drawdown_tolerance: float = 0.06
    lambda_drawdown: float = 0.25
    lambda_turnover: float = 0.02
    lambda_concentration: float = 0.02
    cash_opportunity_cost: float = 1.5  # penalize idle cash when Nifty rises
    total_timesteps: int = 150_000
    learning_rate: float = 3e-4
    n_steps: int = 512
    batch_size: int = 64
    ent_coef: float = 0.02
    seed: int = 42


@dataclass(frozen=True)
class SystemConfig:
    s3: S3Paths = field(default_factory=S3Paths)
    costs: IndiaDeliveryCostConfig = field(default_factory=IndiaDeliveryCostConfig)
    universe: UniverseParams = field(default_factory=UniverseParams)
    portfolio: PortfolioLimits = field(default_factory=PortfolioLimits)
    hmm: HmmParams = field(default_factory=HmmParams)
    gbdt: GbdtParams = field(default_factory=GbdtParams)
    rl: RlParams = field(default_factory=RlParams)
    signal_slots_ist: tuple[str, ...] = ("09:45", "12:45", "15:30")
    pre_cas_slot_ist: str = "15:10"
    post_market_ist: str = "16:05"
    pre_market_ist: str = "09:05"
