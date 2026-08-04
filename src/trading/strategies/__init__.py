"""Strategies and a name->strategy registry (the loader the CLI uses).

Buy-and-hold is the correctness baseline; SMA crossover and equal-weight
allocation are the V2 additions; momentum and RSI mean-reversion round out the
strategy pack.
"""

from __future__ import annotations

import inspect
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


def free_parameter_count(strategy: Strategy | type[Strategy]) -> int:
    """How many tunable knobs a strategy exposes (ADR-0029).

    Counts the named arguments of the strategy's ``__init__`` — precisely the
    values ``trading sweep --param`` can search over, which is what makes them the
    *free* parameters in the overfitting sense. ``self``, ``*args``, and
    ``**kwargs`` are excluded. ``buy_and_hold`` takes no arguments and reports
    ``0`` — nothing to curve-fit; ``equal_weight`` reports ``1`` (its ``invested``
    weight) and ``cross_sectional`` reports ``4``.

    This is the denominator of the trades-per-parameter significance figure
    (:func:`trading.metrics.trades_per_parameter`). Accepts an instance or the
    class. Returns ``0`` for anything whose signature cannot be inspected, so an
    exotic strategy degrades to "no ratio reported" rather than raising mid-run.
    """
    target = strategy if isinstance(strategy, type) else type(strategy)
    try:
        signature = inspect.signature(target.__init__)
    except (TypeError, ValueError):  # pragma: no cover - builtins/C-level callables
        return 0
    return sum(
        1
        for name, param in signature.parameters.items()
        if name != "self"
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    )
