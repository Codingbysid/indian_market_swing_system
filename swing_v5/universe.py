"""Monthly universe of at most 150 names. Held and pending names are pinned first."""

from __future__ import annotations


def select_universe(
    *,
    held: list[str],
    pending: list[str],
    screens: dict[str, list[tuple[str, str, float]]],
    liquid: list[tuple[str, str, float]],
    max_symbols: int = 150,
    screen_slots_max: int = 100,
    sector_fraction: float = 0.2,
) -> dict:
    """screens/liquid rows are (symbol, sector, median_traded_value), best liquidity first."""
    chosen: list[dict] = []
    seen: set[str] = set()

    def add(symbol: str, sector: str, source: str, turnover: float | None = None) -> bool:
        symbol = symbol.upper()
        if symbol in seen or len(chosen) >= max_symbols:
            return False
        sector_cap = max(1, int(max_symbols * sector_fraction))
        if sector and sum(row["sector"] == sector for row in chosen) >= sector_cap and source != "held_pending":
            return False
        seen.add(symbol)
        chosen.append({"symbol": symbol, "sector": sector or "unknown", "source": source, "turnover": turnover})
        return True

    for symbol in held + pending:
        add(symbol, "held", "held_pending")
    screen_names = ["piotroski", "magic", "coffee", "golden"]
    pools = {name: list(screens.get(name) or []) for name in screen_names}
    for name in pools:
        pools[name].sort(key=lambda row: (-row[2], row[0]))
    added_screens = 0
    while added_screens < screen_slots_max and any(pools.values()) and len(chosen) < max_symbols:
        progressed = False
        for name in screen_names:
            if added_screens >= screen_slots_max or not pools[name]:
                continue
            symbol, sector, turnover = pools[name].pop(0)
            if add(symbol, sector, name, turnover):
                added_screens += 1
                progressed = True
        if not progressed:
            break
    for symbol, sector, turnover in sorted(liquid, key=lambda row: (-row[2], row[0])):
        if len(chosen) >= max_symbols:
            break
        add(symbol, sector, "nifty_liquidity", turnover)
    return {
        "symbols": chosen,
        "count": len(chosen),
        "max_symbols": max_symbols,
        "below_cap_is_valid": len(chosen) < max_symbols,
    }
