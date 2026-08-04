"""Strategies and a name->strategy registry (the loader the CLI uses).

Buy-and-hold is the correctness baseline; SMA crossover and equal-weight
allocation are the V2 additions; momentum and RSI mean-reversion round out the
strategy pack.
"""

from __future__ import annotations

from collections.abc import Callable

from trading.interfaces import Strategy
from trading.strategies.buy_and_hold import BuyAndHold
from trading.strategies.cross_sectional import CrossSectional
from trading.strategies.equal_weight import EqualWeight
from trading.strategies.mean_reversion import MeanReversion
from trading.strategies.momentum import Momentum
from trading.strategies.sma_crossover import SmaCrossover

# A factory per name so each run gets a fresh, un-shared strategy instance.
STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "buy_and_hold": BuyAndHold,
    "sma_crossover": SmaCrossover,
    "equal_weight": EqualWeight,
    "momentum": Momentum,
    "mean_reversion": MeanReversion,
    "cross_sectional": CrossSectional,
}


def get_strategy(name: str) -> Strategy:
    """Instantiate a strategy by name, or raise with the list of known names."""
    try:
        factory = STRATEGIES[name]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise KeyError(f"unknown strategy {name!r}; known strategies: {known}") from None
    return factory()
