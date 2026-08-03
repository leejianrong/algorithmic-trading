"""Hand-computed checks for the RSI and Bollinger indicator helpers.

Each expectation is worked out by hand on a small crafted close series so the
formulas are pinned, including the ``None``-when-too-few-bars guard.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from trading.strategies.indicators import bollinger, rolling_std, rsi
from trading.types import Bar


def _bars(closes: Sequence[float]) -> list[Bar]:
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    return [Bar("AAA", ts, c, c, c, c, 100) for c in closes]


def test_rsi_all_gains_is_100() -> None:
    # Every close-to-close change is a gain -> avg_loss 0 -> RSI 100.
    assert rsi(_bars([1, 2, 3, 4, 5]), period=4) == pytest.approx(100.0)


def test_rsi_all_losses_is_0() -> None:
    assert rsi(_bars([5, 4, 3, 2, 1]), period=4) == pytest.approx(0.0)


def test_rsi_flat_window_is_50() -> None:
    # No gains and no losses -> neutral 50 rather than a divide-by-zero.
    assert rsi(_bars([7, 7, 7, 7, 7]), period=4) == pytest.approx(50.0)


def test_rsi_mixed_window_matches_hand_computation() -> None:
    # closes 100,110,105,115,110; deltas +10,-5,+10,-5 over period 4.
    # avg_gain = 20/4 = 5, avg_loss = 10/4 = 2.5, RS = 2 -> RSI = 100 - 100/3.
    value = rsi(_bars([100, 110, 105, 115, 110]), period=4)
    assert value == pytest.approx(100.0 - 100.0 / 3.0)


def test_rsi_uses_only_the_last_period_plus_one_bars() -> None:
    # Leading bars beyond the window must not change the result.
    tail = [100, 110, 105, 115, 110]
    assert rsi(_bars([1, 2, 3, *tail]), period=4) == pytest.approx(
        rsi(_bars(tail), period=4)
    )


def test_rsi_none_when_too_few_bars() -> None:
    # period 4 needs 5 closes; 4 is one short.
    assert rsi(_bars([1, 2, 3, 4]), period=4) is None


def test_rsi_rejects_non_positive_period() -> None:
    with pytest.raises(ValueError, match="period must be positive"):
        rsi(_bars([1, 2, 3]), period=0)


def test_rolling_std_is_population_standard_deviation() -> None:
    # Classic fixture: mean 5, variance 4, population std 2.
    assert rolling_std(_bars([2, 4, 4, 4, 5, 5, 7, 9]), 8) == pytest.approx(2.0)


def test_rolling_std_none_when_too_few_bars() -> None:
    assert rolling_std(_bars([1, 2]), 3) is None


def test_bollinger_bands_sit_num_std_around_the_sma() -> None:
    band = bollinger(_bars([2, 4, 4, 4, 5, 5, 7, 9]), 8, num_std=2.0)
    assert band is not None
    lower, mid, upper = band
    # sma 5, std 2, 2 std -> (1, 5, 9).
    assert (lower, mid, upper) == pytest.approx((1.0, 5.0, 9.0))


def test_bollinger_none_when_too_few_bars() -> None:
    assert bollinger(_bars([1, 2]), 3) is None
