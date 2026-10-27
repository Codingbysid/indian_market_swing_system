"""Offline Gaussian HMM fit. Smoothed probabilities are not returned."""

from __future__ import annotations

import hashlib
import json

import numpy as np

from research_v6.hmm_runtime import OBSERVATION_NAMES, forward_filter, logsumexp

SEEDS = (0, 1, 2)
VARIANCE_FLOOR = 1e-3
MIN_STATE_MASS = 0.02


def _scale(observations: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    center = observations.mean(axis=0)
    spread = observations.std(axis=0)
    if not np.isfinite(center).all() or np.any(spread <= 1e-8):
        raise ValueError("hmm scaling failed")
    return (observations - center) / spread, center, spread


def _initialize(scaled: np.ndarray, seed: int, states: int = 3) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Seeded starts. Quantile labels alone made every seed identical."""
    rng = np.random.default_rng(seed)
    length, dims = scaled.shape
    picks = rng.choice(length, size=states, replace=False)
    means = scaled[picks] + rng.normal(0.0, 0.15, size=(states, dims))
    variances = np.maximum(1.0 + rng.normal(0.0, 0.05, size=(states, dims)), VARIANCE_FLOOR)
    stay = 0.75 + 0.05 * float(seed % 5)
    off = (1.0 - stay) / (states - 1)
    transition = np.full((states, states), off)
    np.fill_diagonal(transition, stay)
    pi = rng.dirichlet(np.ones(states))
    return pi, transition, means, variances


def _log_gauss(scaled: np.ndarray, means: np.ndarray, variances: np.ndarray) -> np.ndarray:
    diff = scaled[:, None, :] - means[None, :, :]
    quad = (diff ** 2) / variances[None, :, :]
    log_det = np.sum(np.log(2.0 * np.pi * variances), axis=1)
    return -0.5 * (log_det[None, :] + np.sum(quad, axis=2))


def _forward_backward(log_emit: np.ndarray, pi: np.ndarray, transition: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    length, states = log_emit.shape
    log_a = np.log(transition)
    log_alpha = np.empty((length, states))
    log_alpha[0] = np.log(pi) + log_emit[0]
    for index in range(1, length):
        for state in range(states):
            log_alpha[index, state] = log_emit[index, state] + logsumexp(log_alpha[index - 1] + log_a[:, state])
    log_beta = np.zeros((length, states))
    for index in range(length - 2, -1, -1):
        for state in range(states):
            log_beta[index, state] = logsumexp(log_a[state] + log_emit[index + 1] + log_beta[index + 1])
    log_gamma = log_alpha + log_beta
    log_gamma -= np.array([logsumexp(row) for row in log_gamma])[:, None]
    log_xi = np.empty((length - 1, states, states))
    for index in range(length - 1):
        raw = log_alpha[index][:, None] + log_a + log_emit[index + 1][None, :] + log_beta[index + 1][None, :]
        log_xi[index] = raw - logsumexp(raw.reshape(-1))
    return np.exp(log_gamma), np.exp(log_xi), logsumexp(log_alpha[-1])


def _safe_log(probabilities: np.ndarray) -> tuple[np.ndarray, bool]:
    floored = np.maximum(probabilities, 1e-12)
    if floored.ndim == 1:
        floored = floored / floored.sum()
    else:
        floored = floored / floored.sum(axis=1, keepdims=True)
    return np.log(floored), bool(np.any(probabilities < 1e-12))


def _fit_one(scaled: np.ndarray, seed: int, covariance: str, iterations: int = 40) -> dict:
    pi, transition, means, variances = _initialize(scaled, seed)
    if covariance == "spherical":
        variances = np.repeat(variances.mean(axis=1)[:, None], scaled.shape[1], axis=1)
    previous = None
    gamma = None
    floor_applied = False
    record = {
        "seed": seed,
        "covariance": covariance,
        "iterations": 0,
        "converged": False,
        "accepted": False,
        "reason": None,
        "probability_floor_applied": False,
    }
    for iteration in range(iterations):
        record["iterations"] = iteration + 1
        log_pi, floored_pi = _safe_log(pi)
        log_transition, floored_transition = _safe_log(transition)
        floor_applied = floor_applied or floored_pi or floored_transition
        gamma, xi, loglik = _forward_backward(_log_gauss(scaled, means, variances), np.exp(log_pi), np.exp(log_transition))
        if not np.isfinite(loglik):
            record["reason"] = "nonfinite_likelihood"
            record["probability_floor_applied"] = floor_applied
            return record
        if previous is not None and abs(loglik - previous) < 1e-6:
            previous = loglik
            record["converged"] = True
            break
        previous = loglik
        pi = gamma[0] / gamma[0].sum()
        transition = xi.sum(axis=0)
        transition /= transition.sum(axis=1, keepdims=True)
        weight = gamma.sum(axis=0)
        if np.any(weight <= 1e-8):
            record["reason"] = "state_weight_collapsed"
            record["probability_floor_applied"] = floor_applied
            return record
        means = (gamma.T @ scaled) / weight[:, None]
        diff = scaled[:, None, :] - means[None, :, :]
        variances = np.maximum((gamma[:, :, None] * diff ** 2).sum(axis=0) / weight[:, None], VARIANCE_FLOOR)
        if covariance == "spherical":
            shared = variances.mean(axis=1)
            variances = np.repeat(shared[:, None], scaled.shape[1], axis=1)
    else:
        record["reason"] = "not_converged"
        record["last_loglik"] = None if previous is None else float(previous)
        record["probability_floor_applied"] = floor_applied
        if gamma is not None:
            mass = gamma.mean(axis=0)
            record["mass"] = [float(value) for value in mass]
            record["min_variance"] = float(np.min(variances))
            record["zero_transitions"] = int(np.sum(transition <= 1e-12))
        return record
    mass = gamma.mean(axis=0)
    record.update({
        "mass": [float(value) for value in mass],
        "min_variance": float(np.min(variances)),
        "zero_transitions": int(np.sum(transition <= 1e-12)),
        "probability_floor_applied": floor_applied,
        "loglik": float(previous),
    })
    if np.any(mass < MIN_STATE_MASS) or not np.isfinite(previous):
        record["reason"] = "state_occupancy_below_2_percent"
        return record
    record["accepted"] = True
    record["model"] = {"pi": pi, "transition": transition, "means": means, "variances": variances, "loglik": float(previous), "mass": mass}
    return record


def _order_by_training_mean(model: dict, scaled: np.ndarray) -> dict:
    order = np.argsort(model["means"][:, 0])
    model = {
        "pi": model["pi"][order],
        "transition": model["transition"][np.ix_(order, order)],
        "means": model["means"][order],
        "variances": model["variances"][order],
        "loglik": model["loglik"],
        "mass": model["mass"][order],
    }
    return model


def select_hmm(observations: np.ndarray, seeds: tuple[int, ...] = SEEDS) -> tuple[dict | None, list[dict]]:
    """Pick an accepted fit by training likelihood. Attempts are returned even when none qualify."""
    raw = np.asarray(observations, dtype=float)
    input_hash = hashlib.sha256(raw.tobytes()).hexdigest()
    scaled, center, spread = _scale(raw)
    best = None
    attempts = []
    for covariance in ("diagonal", "spherical"):
        for seed in seeds:
            fitted = _fit_one(scaled, seed, covariance)
            public = {key: value for key, value in fitted.items() if key != "model"}
            public["input_hash"] = input_hash
            attempts.append(public)
            if fitted["accepted"] and (best is None or fitted["loglik"] > best["loglik"]):
                best = {**fitted["model"], "covariance": covariance, "seed": seed}
    if best is None:
        return None, attempts
    ordered = _order_by_training_mean(best, scaled)
    payload = {
        "schema": "gaussian_hmm_forward_v1",
        "observation_names": list(OBSERVATION_NAMES),
        "covariance": best["covariance"],
        "variance_floor": VARIANCE_FLOOR,
        "pi": ordered["pi"].tolist(),
        "transition": ordered["transition"].tolist(),
        "means": ordered["means"].tolist(),
        "variances": ordered["variances"].tolist(),
        "scaling": {"mean": center.tolist(), "std": spread.tolist()},
        "state_mapping": {
            "order": "training_mean_nifty_log_return_ascending",
            "labels": ["state_rank_0", "state_rank_1", "state_rank_2"],
        },
        "diagnostics": {
            "seed": best["seed"],
            "loglik": ordered["loglik"],
            "state_mass": [float(value) for value in ordered["mass"]],
            "attempts": attempts,
            "input_hash": input_hash,
            "smoothed_posteriors_exported": False,
            "acceptance_thresholds_changed_after_results": False,
        },
    }
    encoded = json.dumps(payload, sort_keys=True).encode()
    payload["model_hash"] = hashlib.sha256(encoded).hexdigest()
    forward_filter(payload, observations[:2])
    return payload, attempts


class HMMFitFailed(RuntimeError):
    def __init__(self, index: int, attempts: list[dict]):
        super().__init__(f"hmm fit failed at observation {index}")
        self.index = index
        self.attempts = attempts


def causal_posteriors(observations: np.ndarray, minimum: int = 252, block: int = 60) -> tuple[np.ndarray, list[dict]]:
    """Posteriors whose parameters were fit only on earlier blocks."""
    frame = np.asarray(observations, dtype=float)
    out = np.full((len(frame), 3), np.nan)
    blocks = []
    start = minimum
    while start < len(frame):
        model, attempts = select_hmm(frame[:start])
        if model is None:
            raise HMMFitFailed(start, attempts)
        stop = min(len(frame), start + block)
        posts = forward_filter(model, frame[:stop])
        out[start:stop] = posts[start:stop]
        blocks.append({"fit_end_exclusive": start, "apply_start": start, "apply_stop": stop, "model_hash": model["model_hash"], "model": model})
        start = stop
    return out, blocks
