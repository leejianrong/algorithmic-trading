"""Registry resolution and the SMA indicator helper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.strategies import get_strategy
from trading.strategies.indicators import sma
from trading.types import Bar


def _bar(close: float) -> Bar:
    return Bar("AAA", datetime(2024, 1, 1, tzinfo=UTC), close, close, close, close, 100)


@pytest.mark.parametrize("name", ["buy_and_hold", "sma_crossover", "equal_weight"])
def test_registry_resolves_known_strategies(name: str) -> None:
    strat = get_strategy(name)
    assert hasattr(strat, "on_bar")


def test_registry_rejects_unknown_with_a_helpful_message() -> None:
    with pytest.raises(KeyError, match="unknown strategy"):
        get_strategy("does_not_exist")


def test_sma_averages_the_last_n_closes() -> None:
    bars = [_bar(c) for c in (10, 20, 30, 40)]
    assert sma(bars, 2) == pytest.approx(35.0)  # (30 + 40) / 2
    assert sma(bars, 4) == pytest.approx(25.0)


def test_sma_returns_none_when_too_few_bars() -> None:
    assert sma([_bar(10)], 3) is None
