"""Forward-only diagonal or spherical Gaussian HMM. No scipy, sklearn, or hmmlearn."""

from __future__ import annotations

import math

import numpy as np

OBSERVATION_NAMES = (
    "nifty_log_return",
    "log_high_low_range",
    "breadth_above_sma50",
    "cross_sectional_return_dispersion",
)


def logsumexp(values: np.ndarray) -> float:
    finite = np.asarray(values, dtype=float)
    peak = np.max(finite)
    if not np.isfinite(peak):
        return float("-inf")
    return float(peak + math.log(np.sum(np.exp(finite - peak))))


def _as_model(model: dict) -> dict:
    if not isinstance(model, dict):
        raise ValueError("hmm model must be an object")
    names = model.get("observation_names")
    if tuple(names or ()) != OBSERVATION_NAMES:
        raise ValueError("observation order mismatch")
    if model.get("covariance") not in {"diagonal", "spherical"}:
        raise ValueError("covariance specification rejected")
    pi = np.asarray(model["pi"], dtype=float)
    transition = np.asarray(model["transition"], dtype=float)
    means = np.asarray(model["means"], dtype=float)
    variances = np.asarray(model["variances"], dtype=float)
    if pi.ndim != 1 or transition.ndim != 2 or means.ndim != 2 or variances.ndim != 2:
        raise ValueError("hmm parameter rank rejected")
    states, dims = means.shape
    if dims != len(OBSERVATION_NAMES) or pi.shape != (states,) or transition.shape != (states, states) or variances.shape != (states, dims):
        raise ValueError("hmm parameter shape rejected")
    if not np.isfinite(pi).all() or not np.isfinite(transition).all() or not np.isfinite(means).all() or not np.isfinite(variances).all():
        raise ValueError("hmm parameter nonfinite")
    if np.any(variances <= 0) or np.any(pi < 0) or np.any(transition < 0):
        raise ValueError("hmm covariance or probability rejected")
    if abs(float(pi.sum()) - 1.0) > 1e-6 or np.any(np.abs(transition.sum(axis=1) - 1.0) > 1e-6):
        raise ValueError("hmm probabilities do not sum to one")
    scaling = model.get("scaling") or {}
    center = np.asarray(scaling.get("mean"), dtype=float)
    spread = np.asarray(scaling.get("std"), dtype=float)
    if center.shape != (dims,) or spread.shape != (dims,) or np.any(spread <= 0) or not np.isfinite(center).all():
        raise ValueError("hmm scaling rejected")
    return {"pi": pi, "transition": transition, "means": means, "variances": variances, "center": center, "spread": spread}


def log_emission(observation: np.ndarray, model: dict) -> np.ndarray:
    checked = _as_model(model)
    x = (np.asarray(observation, dtype=float) - checked["center"]) / checked["spread"]
    if x.shape != (checked["means"].shape[1],) or not np.isfinite(x).all():
        raise ValueError("hmm observation rejected")
    out = []
    for mean, variance in zip(checked["means"], checked["variances"]):
        quad = ((x - mean) ** 2) / variance
        out.append(float(-0.5 * np.sum(math.log(2.0 * math.pi) + np.log(variance) + quad)))
    return np.asarray(out, dtype=float)


def forward_filter(model: dict, observations: np.ndarray) -> np.ndarray:
    """Each row is P(S_t | observations through t) in the exported state order."""
    checked = _as_model(model)
    frame = np.asarray(observations, dtype=float)
    if frame.ndim != 2 or frame.shape[1] != checked["means"].shape[1]:
        raise ValueError("hmm observation matrix rejected")
    states = len(checked["pi"])
    posts = np.zeros((len(frame), states))
    log_alpha = np.log(checked["pi"]) + log_emission(frame[0], model)
    log_alpha -= logsumexp(log_alpha)
    posts[0] = np.exp(log_alpha)
    log_transition = np.log(checked["transition"])
    for index in range(1, len(frame)):
        prior = np.array([logsumexp(log_alpha + log_transition[:, state]) for state in range(states)])
        log_alpha = prior + log_emission(frame[index], model)
        log_alpha -= logsumexp(log_alpha)
        posts[index] = np.exp(log_alpha)
    return posts


def load_model(payload: dict) -> dict:
    _as_model(payload)
    return payload
