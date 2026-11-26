"""Stable-Baselines3 PPO trainer for the NSE swing sizing env (research-only)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv

from src.config import RlParams
from src.models.rl_sizing_env import DecisionStep, NseSwingSizingEnv, SizingEnvConfig


def train_ppo_sizer(
    steps: list[DecisionStep],
    cfg: RlParams = RlParams(),
    env_cfg: SizingEnvConfig | None = None,
    model_path: Path | str | None = "artifacts/models/ppo_sizer.zip",
) -> PPO:
    if not steps:
        raise ValueError("PPO training requires a non-empty DecisionStep list")
    sizing_cfg = env_cfg or SizingEnvConfig(
        top_k_candidates=cfg.top_k_candidates,
        hold_steps=cfg.hold_steps,
        drawdown_tolerance=cfg.drawdown_tolerance,
        lambda_drawdown=cfg.lambda_drawdown,
        lambda_turnover=cfg.lambda_turnover,
        lambda_concentration=cfg.lambda_concentration,
    )

    def _make():
        return NseSwingSizingEnv(steps, sizing_cfg)

    env = DummyVecEnv([_make])
    model = PPO(
        "MlpPolicy",
        env,
        learning_rate=cfg.learning_rate,
        n_steps=min(cfg.n_steps, max(len(steps), 8)),
        batch_size=min(cfg.batch_size, max(len(steps), 8)),
        verbose=0,
        seed=cfg.seed,
    )
    model.learn(total_timesteps=cfg.total_timesteps)
    if model_path is not None:
        path = Path(model_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        model.save(str(path))
    return model


def equal_weight_action(k: int) -> np.ndarray:
    return np.ones(k, dtype=np.float32)


def policy_weights_for_candidates(
    model: PPO | None,
    env: NseSwingSizingEnv,
    obs: np.ndarray,
) -> np.ndarray:
    if model is None:
        return equal_weight_action(env.k)
    action, _ = model.predict(obs, deterministic=True)
    return np.clip(np.asarray(action, dtype=np.float32), 0.0, 1.0)
