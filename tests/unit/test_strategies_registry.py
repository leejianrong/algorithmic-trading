"""Registry resolution and the SMA indicator helper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.strategies import STRATEGIES, free_parameter_count, get_strategy
from trading.strategies.cross_sectional import CrossSectional
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


class TestFreeParameterCount:
    """The denominator of the trades-per-parameter significance check (ADR-0029)."""

    def test_parameterless_strategy_has_nothing_to_overfit(self) -> None:
        assert free_parameter_count(get_strategy("buy_and_hold")) == 0

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("buy_and_hold", 0),  # takes no arguments
            ("equal_weight", 1),  # invested
            ("sma_crossover", 3),  # fast, slow, weight
            ("cross_sectional", 4),  # lookback, top_k, weight, rebalance_days
        ],
    )
    def test_counts_the_tunable_constructor_arguments(self, name: str, expected: int) -> None:
        assert free_parameter_count(get_strategy(name)) == expected

    def test_accepts_a_class_as_well_as_an_instance(self) -> None:
        assert free_parameter_count(CrossSectional) == free_parameter_count(CrossSectional())

    def test_self_is_never_counted(self) -> None:
        class OneKnob:
            def __init__(self, knob: int = 1) -> None:
                self.knob = knob

            def on_bar(self, ts: datetime, bars: dict[str, Bar], context: object) -> list[object]:
                return []

        assert free_parameter_count(OneKnob) == 1  # type: ignore[arg-type]

    def test_varargs_are_not_counted_as_knobs(self) -> None:
        class Splat:
            def __init__(self, real: int = 1, *args: object, **kwargs: object) -> None:
                self.real = real

            def on_bar(self, ts: datetime, bars: dict[str, Bar], context: object) -> list[object]:
                return []

        assert free_parameter_count(Splat) == 1  # type: ignore[arg-type]

    def test_every_registered_strategy_reports_a_count(self) -> None:
        """No registry entry may crash the significance check."""
        for name in STRATEGIES:
            assert free_parameter_count(get_strategy(name)) >= 0
