"""Parameter sweep and simple walk-forward — an OUTER loop over ``Engine.run``.

This module runs the *existing* backtest engine many times; it is not an engine
feature (ADR-0016). Given a strategy name, a parameter grid, a data adapter, a
symbol universe, and a date range, it expands the grid into every combination
(cartesian product), constructs a parameterized strategy for each via the
``STRATEGIES`` registry, runs ``Engine.run`` once per combination, computes the
V4 :func:`~trading.metrics.compute` metrics on each result, and returns a
structured summary that can be ranked by Sharpe or total return.

Everything here is pure with respect to the inputs: no wall clock, no RNG, no
network. Determinism comes entirely from the injected adapter (seed a
``SyntheticAdapter`` for offline, repeatable sweeps) and the deterministic grid
expansion order, so the same strategy + grid + adapter + range always yields the
same ranked summary.

**Walk-forward** is offered as an optional split of ``[start, end]`` into
``windows`` consecutive calendar spans; every combination is run independently on
each window (its own fresh broker and guardrails). This is a *plain* per-window
grid sweep — it does not recombine an in-sample winner into an out-of-sample test;
that recombination is a later slice (see the limitation noted in ADR-0016).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import TYPE_CHECKING, cast

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.engine import Engine
from trading.metrics import PerformanceMetrics, compute
from trading.risk import Guardrails
from trading.strategies import STRATEGIES
from trading.types import Portfolio

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from trading.interfaces import DataAdapter, Strategy

# A single parameter combination, e.g. ``{"fast": 5, "slow": 30}``.
ParamCombo = dict[str, object]

# Ranking keys → how to read them off :class:`~trading.metrics.PerformanceMetrics`.
# Both are "higher is better", so ranking sorts descending on the chosen key.
_RANK_KEYS: dict[str, Callable[[PerformanceMetrics], float]] = {
    "sharpe": lambda m: m.sharpe,
    "total_return": lambda m: m.total_return,
}


def expand_grid(grid: Mapping[str, Sequence[object]]) -> list[ParamCombo]:
    """Expand a parameter grid into the cartesian product of its value lists.

    ``{"fast": [5, 10], "slow": [30, 50]}`` -> four combos, in a deterministic
    order (grid-key order outermost, first key varying slowest). An empty grid
    yields a single empty combo (one run with the strategy's own defaults); a key
    whose value list is empty collapses the whole product to zero combos.
    """
    keys = list(grid)
    value_lists = [list(grid[key]) for key in keys]
    return [dict(zip(keys, combo, strict=True)) for combo in product(*value_lists)]


def split_windows(
    start: datetime,
    end: datetime,
    windows: int,
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end]`` into ``windows`` consecutive equal calendar spans.

    Returns ``(win_start, win_end)`` pairs covering the range back-to-back; the
    last window ends exactly at ``end`` so rounding never drops the final days.
    ``windows <= 1`` (or a non-positive/zero-length range) returns the single
    ``[start, end]`` window unchanged.
    """
    if windows <= 1 or end <= start:
        return [(start, end)]
    span = (end - start) / windows
    bounds = [start + span * i for i in range(windows)]
    bounds.append(end)
    return [(bounds[i], bounds[i + 1]) for i in range(windows)]


@dataclass(frozen=True, slots=True)
class SweepRun:
    """One backtest within a sweep: a parameter combo over one window's metrics.

    ``window`` is 0 for a plain grid sweep and the 0-based window index under
    walk-forward; ``start``/``end`` are that window's actual date bounds.
    """

    params: ParamCombo
    metrics: PerformanceMetrics
    window: int
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """The full set of runs a sweep produced, rankable by a headline metric."""

    strategy: str
    symbols: list[str]
    runs: list[SweepRun] = field(default_factory=list)
    # Combos skipped because the strategy rejected them (e.g. sma fast >= slow),
    # paired with the constructor's error message — surfaced, never silent.
    skipped: list[tuple[ParamCombo, str]] = field(default_factory=list)

    def ranked(self, by: str = "sharpe") -> list[SweepRun]:
        """Runs sorted best-first by ``by`` ('sharpe' or 'total_return').

        The sort is stable, so ties keep their grid-expansion order — the result
        is fully deterministic. Raises ``ValueError`` for an unknown key.
        """
        try:
            key = _RANK_KEYS[by]
        except KeyError:
            known = ", ".join(sorted(_RANK_KEYS))
            raise ValueError(f"unknown rank key {by!r}; known: {known}") from None
        return sorted(self.runs, key=lambda run: key(run.metrics), reverse=True)


def _build_strategy(name: str, combo: ParamCombo) -> Strategy:
    """Construct a parameterized strategy from the registry factory.

    The registry factory *is* the strategy class (``STRATEGIES[name](**combo)``
    builds a configured instance); its declared type takes no args, so we widen
    it to accept the combo's keyword parameters.
    """
    factory = cast("Callable[..., Strategy]", STRATEGIES[name])
    return factory(**combo)


def run_sweep(
    strategy: str,
    grid: Mapping[str, Sequence[object]],
    adapter: DataAdapter,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    cash: float = 1_000.0,
    risk: RiskConfig | None = None,
    windows: int = 1,
) -> SweepSummary:
    """Run ``strategy`` over every grid combination (x every window) and rank.

    For each combination the strategy is built with those parameters and run
    through a *fresh* :class:`~trading.broker.SimulatedBroker` and
    :class:`~trading.risk.Guardrails` (so runs never share state), once per
    walk-forward window. A combination the strategy constructor rejects (e.g.
    ``sma_crossover`` with ``fast >= slow``) is recorded in ``skipped`` and its
    runs omitted, rather than aborting the whole sweep.

    ``risk`` defaults to the enforced :class:`~trading.config.RiskConfig`
    defaults; pass ``RiskConfig.unlimited()`` to sweep unconstrained. Determinism
    is inherited from ``adapter`` — nothing here consults a clock or RNG.
    """
    if strategy not in STRATEGIES:
        known = ", ".join(sorted(STRATEGIES))
        raise KeyError(f"unknown strategy {strategy!r}; known strategies: {known}")

    tickers = list(symbols)
    risk_config = risk if risk is not None else RiskConfig()
    spans = split_windows(start, end, windows)

    runs: list[SweepRun] = []
    skipped: list[tuple[ParamCombo, str]] = []
    for combo in expand_grid(grid):
        try:
            # Construct once to fail fast on an invalid combo before running any
            # window; a fresh instance per window is built inside the loop.
            _build_strategy(strategy, combo)
        except (ValueError, TypeError) as exc:
            skipped.append((combo, str(exc)))
            continue
        for window_index, (win_start, win_end) in enumerate(spans):
            broker = SimulatedBroker(Portfolio(cash=cash))
            engine = Engine(adapter, broker, Guardrails(risk_config))
            result = engine.run(_build_strategy(strategy, combo), tickers, win_start, win_end)
            runs.append(
                SweepRun(
                    params=dict(combo),
                    metrics=compute(result),
                    window=window_index,
                    start=win_start,
                    end=win_end,
                )
            )

    return SweepSummary(strategy=strategy, symbols=tickers, runs=runs, skipped=skipped)
