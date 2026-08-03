"""Prove the whole stack runs on synthetic data — every strategy, offline.

This is the "does it all still work without a network" check the user asked for
before V3: each registered strategy runs through the engine over a multi-symbol
synthetic year and produces a sane, reproducible result.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.data.synthetic import SyntheticAdapter
from trading.engine import BacktestResult, Engine
from trading.strategies import get_strategy
from trading.types import Portfolio

_SYMBOLS = ["AAA", "BBB", "CCC"]
_START = datetime(2021, 1, 1, tzinfo=UTC)
_END = datetime(2021, 12, 31, tzinfo=UTC)


def _run(strategy_name: str, seed: int = 3) -> BacktestResult:
    adapter = SyntheticAdapter(seed=seed)
    broker = SimulatedBroker(Portfolio(cash=1_000.0))
    return Engine(adapter, broker).run(get_strategy(strategy_name), _SYMBOLS, _START, _END)


@pytest.mark.parametrize("strategy", ["buy_and_hold", "sma_crossover", "equal_weight"])
def test_every_strategy_runs_on_synthetic_data(strategy: str) -> None:
    result = _run(strategy)

    # About a year of weekdays, one equity point per trading day.
    assert 250 <= len(result.equity_curve) <= 262
    assert result.final_equity > 0
    # Equity is finite and positive throughout.
    assert all(p.equity > 0 for p in result.equity_curve)


@pytest.mark.parametrize("strategy", ["buy_and_hold", "equal_weight"])
def test_fully_invested_strategies_deploy_capital_without_rejections(strategy: str) -> None:
    result = _run(strategy)
    assert result.fills, "expected the strategy to trade"
    assert not result.rejections, f"unexpected rejections: {result.rejections[:3]}"


def test_synthetic_backtest_is_reproducible() -> None:
    first = _run("equal_weight", seed=99)
    second = _run("equal_weight", seed=99)
    assert [p.equity for p in first.equity_curve] == [p.equity for p in second.equity_curve]
