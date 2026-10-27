"""Training-only payoff shrinkage. Wins and losses keep separate counts."""

from __future__ import annotations


def shrink_mean(count: int, mean: float, strength: float, pooled: float) -> float:
    """mu = (n * mean + m * pooled) / (n + m). A zero count returns the pooled mean."""
    if strength < 0:
        raise ValueError("shrinkage strength must be non-negative")
    if count < 0:
        raise ValueError("count must be non-negative")
    if count == 0:
        return float(pooled)
    return (count * float(mean) + strength * float(pooled)) / (count + strength)


def family_payoffs(net_r: list[float], families: list[str], strength: float) -> dict:
    """Fit pooled and per-family shrunken means. Losses stay negative."""
    if len(net_r) != len(families):
        raise ValueError("net_r and families must align")
    wins = [value for value in net_r if value > 0]
    losses = [value for value in net_r if value <= 0]
    if not wins or not losses:
        raise ValueError("training sample needs both a win and a loss")
    pooled_win = sum(wins) / len(wins)
    pooled_loss = sum(losses) / len(losses)
    out = {"m": strength, "pooled_win": pooled_win, "pooled_loss": pooled_loss, "families": {}}
    names = sorted(set(families))
    for name in names:
        sample = [value for value, family in zip(net_r, families) if family == name]
        family_wins = [value for value in sample if value > 0]
        family_losses = [value for value in sample if value <= 0]
        out["families"][name] = {
            "n_win": len(family_wins),
            "n_loss": len(family_losses),
            "mu_win": shrink_mean(len(family_wins), sum(family_wins) / len(family_wins) if family_wins else 0.0, strength, pooled_win),
            "mu_loss": shrink_mean(len(family_losses), sum(family_losses) / len(family_losses) if family_losses else 0.0, strength, pooled_loss),
        }
    return out


def expected_net_r(probability: float, family: str, table: dict) -> float:
    row = table["families"][family]
    probability = min(max(float(probability), 0.0), 1.0)
    return probability * row["mu_win"] + (1.0 - probability) * row["mu_loss"]
