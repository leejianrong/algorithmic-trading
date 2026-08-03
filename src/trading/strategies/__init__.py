"""Strategies and a name→strategy registry (the loader the CLI uses).

V1 ships buy-and-hold as the correctness baseline; SMA crossover and an
allocation example arrive in V2.
"""

from __future__ import annotations

from collections.abc import Callable

from trading.interfaces import Strategy
from trading.strategies.buy_and_hold import BuyAndHold

# A factory per name so each run gets a fresh, un-shared strategy instance.
STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "buy_and_hold": BuyAndHold,
}


def get_strategy(name: str) -> Strategy:
    """Instantiate a strategy by name, or raise with the list of known names."""
    try:
        factory = STRATEGIES[name]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise KeyError(f"unknown strategy {name!r}; known strategies: {known}") from None
    return factory()
