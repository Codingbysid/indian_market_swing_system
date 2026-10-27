"""Persisted forward posterior. Duplicate sessions do not move it."""

from __future__ import annotations

import numpy as np

from research_v6.hmm_runtime import forward_filter, load_model


def bootstrap(model: dict) -> dict:
    checked = load_model(model)
    return {
        "model_hash": checked["model_hash"],
        "last_session": None,
        "posterior": None,
        "updates": 0,
    }


def advance(state: dict, model: dict, session: str, observation, calendar: list[str]) -> dict:
    checked = load_model(model)
    if state.get("model_hash") != checked["model_hash"]:
        raise ValueError("posterior model hash mismatch")
    if session not in calendar:
        raise ValueError("session missing from the declared calendar")
    last = state.get("last_session")
    if last == session:
        return {**state, "duplicate": True}
    if last is None:
        expected = calendar[0]
    else:
        position = calendar.index(last)
        if position + 1 >= len(calendar):
            raise ValueError("no next session")
        expected = calendar[position + 1]
    if session != expected:
        raise ValueError("missing exchange session")
    if last is None:
        posterior = forward_filter(checked, [observation])[0]
    else:
        predictive = np.asarray(checked["transition"], dtype=float).T @ np.asarray(state["posterior"], dtype=float)
        restarted = dict(checked)
        restarted["pi"] = predictive.tolist()
        posterior = forward_filter(restarted, [observation])[0]
    return {
        "model_hash": checked["model_hash"],
        "last_session": session,
        "posterior": posterior.tolist(),
        "updates": int(state.get("updates") or 0) + 1,
        "duplicate": False,
    }
