"""Rolling 3-state Gaussian HMM for Nifty / VIX regime labels.

Research-only. Fitted on a trailing window so each day's posterior uses only
data available up to that day. Raw state indices are remapped to BULL,
SIDEWAYS, and BEAR_HIGH_VOL from emission means so labels stay stable across
refits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

# sklearn k-means init inside hmmlearn can abort under OpenMP on some macOS/Py3.13 builds.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")


@dataclass(frozen=True)
class RegimeConfig:
    train_window_days: int = 504  # ~2 years of rolling macro history
    refit_every_n_days: int = 21  # monthly refit keeps backtests fast & causal
    vol_lookback_days: int = 20
    n_iter: int = 200
    random_state: int = 42
    block_bear_entries: bool = True
    max_bear_prob_for_entry: float = 0.65
    # Soft gate: scale new-entry size instead of hard-blocking (0 disables soft mode)
    soft_bear_scale: float = 0.0
    soft_bear_prob_threshold: float = 0.65


def build_macro_features(
    macro_df: pd.DataFrame,
    vol_lookback_days: int = 20,
) -> pd.DataFrame:
    """Build causal daily macro features from Nifty (and optional India VIX)."""
    df = macro_df.sort_values("date").drop_duplicates(subset=["date"]).copy()
    df["date"] = pd.to_datetime(df["date"])
    df["ret_1d"] = np.log(df["nifty_close"] / df["nifty_close"].shift(1))
    df["mom_20d"] = np.log(df["nifty_close"] / df["nifty_close"].shift(vol_lookback_days))

    realized_vol = df["ret_1d"].rolling(vol_lookback_days).std() * np.sqrt(252)
    if "india_vix" in df.columns and df["india_vix"].notna().any():
        vix_norm = pd.to_numeric(df["india_vix"], errors="coerce") / 100.0
        df["vol_feat"] = vix_norm.fillna(realized_vol)
    else:
        df["vol_feat"] = realized_vol

    return df.dropna(subset=["ret_1d", "vol_feat", "mom_20d"]).reset_index(drop=True)


def _label_hmm_states(model: GaussianHMM) -> dict[int, str]:
    """Map raw HMM state IDs to semantic regimes from emission means."""
    means = model.means_  # (3, 2): [ret_1d_mean, vol_feat_mean]
    bear_state = int(np.argmax(means[:, 1]))
    remaining = [state for state in range(len(means)) if state != bear_state]
    if means[remaining[0], 0] >= means[remaining[1], 0]:
        bull_state, side_state = int(remaining[0]), int(remaining[1])
    else:
        bull_state, side_state = int(remaining[1]), int(remaining[0])
    return {
        bull_state: "BULL",
        side_state: "SIDEWAYS",
        bear_state: "BEAR_HIGH_VOL",
    }


def _seed_means(train_slice: np.ndarray) -> np.ndarray:
    """Percentile-based means so EM does not depend on sklearn k-means."""
    order = np.argsort(train_slice[:, 0])
    thirds = np.array_split(order, 3)
    return np.vstack([train_slice[idx].mean(axis=0) for idx in thirds if len(idx) > 0])


def _fit_gaussian_hmm(train_slice: np.ndarray, cfg: RegimeConfig, seed: int) -> GaussianHMM:
    """Fit a 3-state HMM with diag covars and manual mean seeds (no k-means)."""
    candidate = GaussianHMM(
        n_components=3,
        covariance_type="diag",
        n_iter=cfg.n_iter,
        random_state=seed,
        init_params="stc",  # skip 'm' — avoid OpenMP k-means abort paths
        params="stmc",
    )
    candidate.means_ = _seed_means(train_slice)
    candidate.fit(train_slice)
    return candidate


def compute_rolling_hmm_regimes(
    macro_df: pd.DataFrame,
    cfg: RegimeConfig = RegimeConfig(),
) -> pd.DataFrame:
    """Strictly causal rolling-window 3-state HMM regimes.

    At step t the model trains only on [t - train_window_days, t).
    """
    feat_df = build_macro_features(macro_df, vol_lookback_days=cfg.vol_lookback_days)
    if len(feat_df) <= cfg.train_window_days:
        return pd.DataFrame(columns=["date", "regime", "prob_bull", "prob_sideways", "prob_bear"])

    obs = feat_df[["ret_1d", "vol_feat"]].to_numpy(dtype=float)
    dates = feat_df["date"].to_numpy()
    model: GaussianHMM | None = None
    state_map: dict[int, str] = {}
    records: list[dict] = []

    for idx in range(cfg.train_window_days, len(feat_df)):
        need_refit = model is None or ((idx - cfg.train_window_days) % cfg.refit_every_n_days == 0)
        if need_refit:
            train_slice = obs[idx - cfg.train_window_days : idx]
            seed = cfg.random_state + (idx // max(cfg.refit_every_n_days, 1))
            try:
                candidate = _fit_gaussian_hmm(train_slice, cfg, seed)
                model = candidate
                state_map = _label_hmm_states(model)
            except Exception:
                # Keep the previous model if EM fails on this window.
                pass

        if model is None or not state_map:
            continue

        window_obs = obs[max(0, idx - cfg.train_window_days) : idx + 1]
        post_probs = model.predict_proba(window_obs)[-1]
        prob_by_label = {"BULL": 0.0, "SIDEWAYS": 0.0, "BEAR_HIGH_VOL": 0.0}
        for raw_state, label in state_map.items():
            prob_by_label[label] += float(post_probs[raw_state])

        active_regime = max(prob_by_label, key=prob_by_label.get)
        records.append(
            {
                "date": pd.Timestamp(dates[idx]),
                "regime": active_regime,
                "prob_bull": round(prob_by_label["BULL"], 4),
                "prob_sideways": round(prob_by_label["SIDEWAYS"], 4),
                "prob_bear": round(prob_by_label["BEAR_HIGH_VOL"], 4),
            }
        )

    return pd.DataFrame(records)


def label_regime_from_probs(
    prob_bull: float,
    prob_sideways: float,
    prob_bear: float,
) -> str:
    """Exact argmax over the three HMM posteriors (never a binary BULL/SIDEWAYS ternary)."""
    probs = {
        "BULL": float(prob_bull),
        "SIDEWAYS": float(prob_sideways),
        "BEAR_HIGH_VOL": float(prob_bear),
    }
    return max(probs, key=probs.get)


def entries_blocked_by_regime(
    regime_row: dict,
    cfg: RegimeConfig = RegimeConfig(),
) -> bool:
    """True when the HMM gate should hard-block new long entries.

    Soft mode (``soft_bear_scale > 0``) never hard-blocks; use
    ``entry_size_scale_for_regime`` instead.
    """
    if cfg.soft_bear_scale and cfg.soft_bear_scale > 0:
        return False
    if not cfg.block_bear_entries:
        return False
    if regime_row.get("regime") == "UNFILTERED":
        return False
    return (
        regime_row.get("regime") == "BEAR_HIGH_VOL"
        or float(regime_row.get("prob_bear", 0.0)) >= cfg.max_bear_prob_for_entry
    )


def entry_size_scale_for_regime(
    regime_row: dict,
    cfg: RegimeConfig = RegimeConfig(),
) -> float:
    """Return 0.5x (or configured soft scale) when bear prob is elevated, else 1.0."""
    if not cfg.soft_bear_scale or cfg.soft_bear_scale <= 0:
        return 1.0
    if regime_row.get("regime") == "UNFILTERED":
        return 1.0
    if float(regime_row.get("prob_bear", 0.0)) >= cfg.soft_bear_prob_threshold:
        return float(cfg.soft_bear_scale)
    return 1.0
