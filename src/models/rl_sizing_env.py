"""Gymnasium env for RL position sizing over OOF GBDT candidates + HMM state.

Research-only. Actions are continuous [0, 1]^K weight proposals that are
projected through hard regime, position-count, weekly-entry, and sector caps
before execution. Rewards use net equity change minus drawdown, turnover, and
sector-concentration penalties.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces


@dataclass(frozen=True)
class SizingEnvConfig:
    initial_capital: float = 1_000_000.0
    top_k_candidates: int = 5
    max_open_positions: int = 10
    max_new_entries_per_step: int = 2
    max_positions_per_sector: int = 2
    max_sector_weight_share: float = 0.40
    round_trip_cost_bps: float = 35.0
    hold_steps: int = 4
    block_entries_in_bear: bool = True
    max_bear_prob_for_entry: float = 0.65
    drawdown_tolerance: float = 0.06
    lambda_drawdown: float = 2.0
    lambda_turnover: float = 0.25
    lambda_concentration: float = 0.15


@dataclass
class DecisionStep:
    date: pd.Timestamp
    prob_bull: float
    prob_sideways: float
    prob_bear: float
    nifty_mom_20d: float
    vix_zscore: float
    candidates: list[dict[str, Any]]
    next_prices: dict[str, float]


class NseSwingSizingEnv(gym.Env):
    """RL sizer over precomputed GBDT top-K candidates and rolling HMM probs."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        steps: list[DecisionStep],
        cfg: SizingEnvConfig = SizingEnvConfig(),
    ) -> None:
        super().__init__()
        if not steps:
            raise ValueError("`steps` must be a non-empty list of DecisionStep items.")
        self.steps = steps
        self.cfg = cfg
        self.k = cfg.top_k_candidates

        # Macro/HMM (5) + portfolio (5) + per-candidate (k * 4)
        obs_dim = 5 + 5 + (self.k * 4)
        self.observation_space = spaces.Box(
            low=-10.0,
            high=10.0,
            shape=(obs_dim,),
            dtype=np.float32,
        )
        self.action_space = spaces.Box(
            low=0.0,
            high=1.0,
            shape=(self.k,),
            dtype=np.float32,
        )

        self._idx = 0
        self._cash = float(cfg.initial_capital)
        self._peak_equity = float(cfg.initial_capital)
        self._positions: dict[str, dict[str, Any]] = {}

    def _portfolio_value(self, prices: dict[str, float]) -> float:
        pos_val = 0.0
        for sym, pos in self._positions.items():
            px = prices.get(sym, pos["entry_price"])
            pos_val += pos["shares"] * float(px)
        return float(self._cash + pos_val)

    def _sector_weights(self, prices: dict[str, float], total_eq: float) -> dict[str, float]:
        sec_val: dict[str, float] = {}
        for sym, pos in self._positions.items():
            px = prices.get(sym, pos["entry_price"])
            sec = pos["sector"]
            sec_val[sec] = sec_val.get(sec, 0.0) + pos["shares"] * float(px)
        if total_eq <= 0:
            return {}
        return {sec: val / total_eq for sec, val in sec_val.items()}

    def _get_obs(self, step: DecisionStep) -> np.ndarray:
        ref_prices = {c["symbol"]: float(c["price"]) for c in step.candidates}
        for sym, pos in self._positions.items():
            ref_prices.setdefault(sym, float(pos["entry_price"]))

        eq = self._portfolio_value(ref_prices)
        invested = max(eq - self._cash, 0.0)
        dd = max(0.0, (self._peak_equity - eq) / max(self._peak_equity, 1e-8))
        sec_weights = self._sector_weights(ref_prices, eq)
        max_sec_share = max(sec_weights.values(), default=0.0)

        macro_vec = [
            float(np.clip(step.prob_bull, 0.0, 1.0)),
            float(np.clip(step.prob_sideways, 0.0, 1.0)),
            float(np.clip(step.prob_bear, 0.0, 1.0)),
            float(np.clip(step.nifty_mom_20d, -5.0, 5.0)),
            float(np.clip(step.vix_zscore, -5.0, 5.0)),
        ]
        port_vec = [
            float(np.clip(self._cash / max(eq, 1e-8), 0.0, 1.0)),
            float(np.clip(invested / max(eq, 1e-8), 0.0, 2.0)),
            float(np.clip(dd, 0.0, 1.0)),
            float(np.clip(len(self._positions) / max(self.cfg.max_open_positions, 1), 0.0, 2.0)),
            float(np.clip(max_sec_share, 0.0, 1.0)),
        ]

        cand_vec: list[float] = []
        for i in range(self.k):
            if i < len(step.candidates):
                c = step.candidates[i]
                cand_vec.extend(
                    [
                        float(np.clip(c.get("gbdt_score", 0.0), -10.0, 10.0)),
                        float(np.clip(c.get("gbdt_rank_pct", 0.5), 0.0, 1.0)),
                        float(np.clip(c.get("mom_20d", 0.0), -5.0, 5.0)),
                        float(np.clip(c.get("atr_pct", 0.02), 0.0, 1.0)),
                    ]
                )
            else:
                cand_vec.extend([0.0, 0.0, 0.0, 0.0])

        return np.asarray(macro_vec + port_vec + cand_vec, dtype=np.float32)

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        super().reset(seed=seed)
        self._idx = 0
        self._cash = float(self.cfg.initial_capital)
        self._peak_equity = float(self.cfg.initial_capital)
        self._positions = {}
        obs = self._get_obs(self.steps[self._idx])
        return obs, {"date": self.steps[self._idx].date, "equity": self._cash}

    def step(self, action: np.ndarray) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        step_obj = self.steps[self._idx]
        prices_now = {c["symbol"]: float(c["price"]) for c in step_obj.candidates}
        for sym, pos in self._positions.items():
            prices_now.setdefault(sym, float(pos["entry_price"]))

        prev_equity = self._portfolio_value(prices_now)
        traded_notional = 0.0
        cost_rate = self.cfg.round_trip_cost_bps / 20_000.0

        to_close: list[str] = []
        for sym, pos in self._positions.items():
            pos["steps_held"] += 1
            if pos["steps_held"] >= self.cfg.hold_steps and sym in prices_now:
                px = float(prices_now[sym])
                gross_proceeds = pos["shares"] * px
                fee = gross_proceeds * cost_rate
                self._cash += gross_proceeds - fee
                traded_notional += gross_proceeds
                to_close.append(sym)
        for sym in to_close:
            del self._positions[sym]

        regime_blocked = (
            self.cfg.block_entries_in_bear and step_obj.prob_bear >= self.cfg.max_bear_prob_for_entry
        )
        open_slots = max(self.cfg.max_open_positions - len(self._positions), 0)
        allowed_new = 0 if regime_blocked else min(self.cfg.max_new_entries_per_step, open_slots)

        sector_pos_counts: dict[str, int] = {}
        for pos in self._positions.values():
            sector_pos_counts[pos["sector"]] = sector_pos_counts.get(pos["sector"], 0) + 1

        chosen_count = 0
        clipped_action = np.clip(np.asarray(action, dtype=np.float32), 0.0, 1.0)
        ranked_indices = np.argsort(-clipped_action[: len(step_obj.candidates)])
        base_slot_budget = prev_equity / max(self.cfg.max_open_positions, 1)

        for idx_c in ranked_indices:
            if chosen_count >= allowed_new:
                break
            w_prop = float(clipped_action[idx_c])
            if w_prop <= 0.05:
                continue

            cand = step_obj.candidates[int(idx_c)]
            sym = cand["symbol"]
            sec = str(cand["sector"])
            px = float(cand["price"])
            if sym in self._positions or px <= 0.0:
                continue
            if sector_pos_counts.get(sec, 0) >= self.cfg.max_positions_per_sector:
                continue

            desired_notional = min(base_slot_budget * w_prop, self._cash)
            if desired_notional <= 1.0:
                continue

            fee = desired_notional * cost_rate
            net_spend = desired_notional + fee
            if net_spend > self._cash:
                desired_notional = max(self._cash / (1.0 + cost_rate), 0.0)
                fee = desired_notional * cost_rate
                net_spend = desired_notional + fee

            if desired_notional > 1.0:
                shares = desired_notional / px
                self._cash -= net_spend
                traded_notional += desired_notional
                self._positions[sym] = {
                    "shares": shares,
                    "entry_price": px,
                    "sector": sec,
                    "steps_held": 0,
                }
                sector_pos_counts[sec] = sector_pos_counts.get(sec, 0) + 1
                chosen_count += 1

        next_prices = step_obj.next_prices if step_obj.next_prices else prices_now
        curr_equity = self._portfolio_value(next_prices)
        self._peak_equity = max(self._peak_equity, curr_equity)

        step_ret = (curr_equity - prev_equity) / max(prev_equity, 1e-8)
        drawdown = max(0.0, (self._peak_equity - curr_equity) / max(self._peak_equity, 1e-8))
        dd_excess = max(0.0, drawdown - self.cfg.drawdown_tolerance)
        turnover = traded_notional / max(prev_equity, 1e-8)
        sec_weights = self._sector_weights(next_prices, curr_equity)
        hhi = sum(w * w for w in sec_weights.values()) if sec_weights else 0.0

        reward = float(
            step_ret
            - self.cfg.lambda_drawdown * dd_excess
            - self.cfg.lambda_turnover * turnover
            - self.cfg.lambda_concentration * hhi
        )

        self._idx += 1
        terminated = self._idx >= len(self.steps)
        truncated = False
        next_obs = (
            self._get_obs(self.steps[self._idx])
            if not terminated
            else np.zeros(self.observation_space.shape, dtype=np.float32)
        )
        info = {
            "date": step_obj.date,
            "equity": round(curr_equity, 2),
            "step_ret": round(step_ret, 6),
            "drawdown": round(drawdown, 6),
            "turnover": round(turnover, 4),
            "hhi": round(hhi, 4),
            "regime_blocked": regime_blocked,
            "open_positions": len(self._positions),
        }
        return next_obs, reward, terminated, truncated, info


if __name__ == "__main__":
    sample_steps = [
        DecisionStep(
            date=pd.Timestamp("2025-01-03") + pd.Timedelta(days=7 * i),
            prob_bull=0.72,
            prob_sideways=0.20,
            prob_bear=0.08,
            nifty_mom_20d=0.03,
            vix_zscore=-0.4,
            candidates=[
                {
                    "symbol": f"STOCK{j}",
                    "sector": "Financial Services" if j % 2 == 0 else "IT",
                    "gbdt_score": 1.2 - 0.2 * j,
                    "gbdt_rank_pct": 0.95 - 0.1 * j,
                    "mom_20d": 0.05,
                    "atr_pct": 0.018,
                    "price": 1000.0 + 10.0 * j,
                }
                for j in range(5)
            ],
            next_prices={f"STOCK{j}": 1005.0 + 10.0 * j for j in range(5)},
        )
        for i in range(10)
    ]
    env = NseSwingSizingEnv(sample_steps)
    obs, info = env.reset()
    total_reward = 0.0
    done = False
    while not done:
        action = np.ones(env.action_space.shape, dtype=np.float32)
        obs, reward, term, trunc, info = env.step(action)
        total_reward += reward
        done = term or trunc
    print("Final info:", info, "| Cumulative reward:", round(total_reward, 5))
