"""Fast, no-infra tests for Monte Carlo path shuffling (ADR-0067, KAN-859).

Complements ``test_significance.py`` (ADR-0039's bootstrap) rather than
duplicating it: the load-bearing claims here are about a *permutation*, not a
resample-with-replacement — every observed return appears exactly once in every
reshuffle — and about which statistics a pure reordering can and cannot change.
Sharpe (mean/stdev of an unordered multiset) cannot; max drawdown (a genuinely
path-dependent statistic) can and does, which is proven directly with a
hand-built ordering rather than only inferred from random shuffles.

Every fixture is a hand-built equity curve driven by a locally-seeded
:class:`random.Random`, so the whole file is deterministic without a network, an
engine, or the global RNG.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from trading.engine import EquityPoint
from trading.metrics import (
    DEFAULT_BOOTSTRAP_SEED,
    MIN_BOOTSTRAP_OBSERVATIONS,
    _empirical_percentile,
    _max_drawdown_of,
    _sharpe_of,
    _shuffled_copy,
    max_drawdown,
    monte_carlo_shuffle,
    sharpe,
)

_EPOCH = datetime(2000, 1, 3, tzinfo=UTC)


def _curve(returns: list[float], *, start: float = 1_000.0) -> list[EquityPoint]:
    """An equity curve whose per-bar returns are exactly ``returns``, in order."""
    points = [EquityPoint(_EPOCH, start)]
    equity = start
    for offset, ret in enumerate(returns, start=1):
        equity *= 1.0 + ret
        points.append(EquityPoint(_EPOCH + timedelta(days=offset), equity))
    return points


def _noise(n: int, seed: int, *, mean: float = 0.0005, sigma: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sigma) for _ in range(n)]


class TestShuffledCopyIsAPermutation:
    """The crucial difference from ADR-0039's bootstrap: reorder, never resample."""

    def test_every_shuffle_is_the_same_multiset(self) -> None:
        returns = _noise(200, seed=1)
        rng = random.Random(7)
        for _ in range(20):
            shuffled = _shuffled_copy(returns, rng)
            assert Counter(shuffled) == Counter(returns)
            assert len(shuffled) == len(returns)

    def test_it_does_not_touch_the_global_rng(self) -> None:
        random.seed(999)
        before = random.getstate()
        _shuffled_copy(_noise(50, seed=2), random.Random(3))
        assert random.getstate() == before

    def test_a_shuffle_is_not_simply_a_bootstrap_resample(self) -> None:
        """A resample-with-replacement can repeat or skip an observation; a
        permutation never can. Constructing a series with a unique value lets a
        resample's duplicate-or-drop be told apart from a permutation's reorder.
        """
        returns = list(range(100))  # ints stand in for distinct floats; fine for Counter
        rng = random.Random(5)
        shuffled = _shuffled_copy([float(r) for r in returns], rng)
        assert sorted(shuffled) == [float(r) for r in returns]


class TestEmpiricalPercentile:
    def test_below_everything_is_zero(self) -> None:
        assert _empirical_percentile([1.0, 2.0, 3.0], 0.0) == 0.0

    def test_at_or_above_everything_is_one(self) -> None:
        assert _empirical_percentile([1.0, 2.0, 3.0], 3.0) == 1.0
        assert _empirical_percentile([1.0, 2.0, 3.0], 10.0) == 1.0

    def test_the_median_of_five_lands_at_three_fifths(self) -> None:
        assert _empirical_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 3.0) == pytest.approx(3 / 5)

    def test_an_empty_distribution_is_neutral_rather_than_crashing(self) -> None:
        assert _empirical_percentile([], 1.0) == 0.5


class TestSharpeIsInvariantUnderPermutation:
    """The card's central mathematical claim, checked directly rather than assumed.

    Mean and sample variance do not depend on the order their inputs are summed
    in (conceptually) — floating-point summation is not perfectly associative, so
    a permutation can move the *last bit or two* of the result, but it cannot
    produce a materially different Sharpe. That is exactly why a "distribution"
    of shuffled Sharpes would be dishonest: it would just be this same number,
    thousands of times, with rounding noise dressed up as evidence.
    """

    def test_a_thousand_shuffles_leave_the_sharpe_essentially_unchanged(self) -> None:
        returns = _noise(500, seed=11)
        original = _sharpe_of(returns, 252.0)
        rng = random.Random(21)
        for _ in range(1_000):
            shuffled = _shuffled_copy(returns, rng)
            reshuffled_sharpe = _sharpe_of(shuffled, 252.0)
            # Not exact equality: floating-point summation order can move the last
            # bit or two. This tolerance is many orders of magnitude tighter than
            # any Sharpe difference that would ever be read as meaningful.
            assert math.isclose(reshuffled_sharpe, original, rel_tol=1e-9, abs_tol=1e-9)

    def test_the_reported_sharpe_matches_the_unshuffled_one(self) -> None:
        curve = _curve(_noise(200, seed=12))
        report = monte_carlo_shuffle(curve, resamples=50)
        assert report.sharpe == pytest.approx(sharpe(curve))


class TestMaxDrawdownIsNotInvariant:
    """The entire reason this feature exists: reordering the SAME losses changes
    how badly they cluster, and max drawdown sees that even though Sharpe cannot.
    """

    def test_clustering_the_same_losses_together_is_worse_than_spreading_them_out(
        self,
    ) -> None:
        # Same five -5% losses and twenty +1% gains in both orderings.
        clustered = [-0.05] * 5 + [0.01] * 20
        spread = ([-0.05] + [0.01] * 4) * 5
        assert Counter(clustered) == Counter(spread)
        assert _max_drawdown_of(clustered) > _max_drawdown_of(spread)

    def test_a_synthetic_curve_shows_measurable_drawdown_variance_under_shuffling(
        self,
    ) -> None:
        # A trending series with occasional sharp drops: reordering visibly moves
        # the drawdown, unlike the Sharpe above.
        returns = [0.01] * 40 + [-0.15] * 3 + [0.01] * 40
        curve = _curve(returns)
        report = monte_carlo_shuffle(curve, resamples=1_000)
        assert report.shuffled_low is not None
        assert report.shuffled_high is not None
        assert report.shuffled_high - report.shuffled_low > 0.01


class TestDeterminism:
    def test_the_same_seed_gives_the_same_report(self) -> None:
        curve = _curve(_noise(300, seed=1))
        first = monte_carlo_shuffle(curve, resamples=200, seed=4242)
        second = monte_carlo_shuffle(curve, resamples=200, seed=4242)
        assert first == second

    def test_a_different_seed_gives_a_different_distribution(self) -> None:
        curve = _curve(_noise(300, seed=1))
        first = monte_carlo_shuffle(curve, resamples=200, seed=1)
        second = monte_carlo_shuffle(curve, resamples=200, seed=2)
        assert (first.shuffled_low, first.shuffled_high) != (
            second.shuffled_low,
            second.shuffled_high,
        )

    def test_the_seed_is_recorded_on_the_result(self) -> None:
        report = monte_carlo_shuffle(_curve(_noise(300, seed=1)), resamples=50, seed=77)
        assert report.seed == 77

    def test_the_default_seed_is_a_fixed_constant_not_a_clock(self) -> None:
        report = monte_carlo_shuffle(_curve(_noise(300, seed=1)), resamples=50)
        assert report.seed == DEFAULT_BOOTSTRAP_SEED

    def test_the_global_rng_is_never_touched(self) -> None:
        random.seed(12345)
        before = random.getstate()
        monte_carlo_shuffle(_curve(_noise(300, seed=1)), resamples=100)
        assert random.getstate() == before


class TestTooShortToShuffle:
    """A garbage report is worse than an honest absence, so there is a floor."""

    def test_below_the_floor_every_field_is_none(self) -> None:
        report = monte_carlo_shuffle(_curve(_noise(MIN_BOOTSTRAP_OBSERVATIONS - 1, seed=5)))
        assert report.actual_max_drawdown is None
        assert report.shuffled_low is None
        assert report.shuffled_median is None
        assert report.shuffled_high is None
        assert report.actual_percentile is None
        assert report.sharpe is None
        assert report.worse_than_shuffled is None
        assert report.better_than_shuffled is None

    def test_exactly_at_the_floor_a_report_exists(self) -> None:
        report = monte_carlo_shuffle(_curve(_noise(MIN_BOOTSTRAP_OBSERVATIONS, seed=5)))
        assert report.actual_max_drawdown is not None
        assert report.observations == MIN_BOOTSTRAP_OBSERVATIONS

    def test_the_absence_is_explained_in_words(self) -> None:
        report = monte_carlo_shuffle(_curve(_noise(10, seed=5)))
        assert any("below the 30" in note for note in report.notes)

    def test_it_always_returns_an_object_never_none(self) -> None:
        """Mirrors RegimeReport's convention, not SharpeInterval's bare ``None``."""
        report = monte_carlo_shuffle(_curve([]))
        assert report is not None
        assert report.notes


class TestReportShape:
    def test_low_median_high_are_ordered(self) -> None:
        curve = _curve(_noise(1_000, seed=9))
        report = monte_carlo_shuffle(curve, resamples=300)
        assert report.shuffled_low is not None
        assert report.shuffled_median is not None
        assert report.shuffled_high is not None
        assert report.shuffled_low <= report.shuffled_median <= report.shuffled_high

    def test_actual_percentile_is_a_fraction(self) -> None:
        curve = _curve(_noise(1_000, seed=9))
        report = monte_carlo_shuffle(curve, resamples=300)
        assert report.actual_percentile is not None
        assert 0.0 <= report.actual_percentile <= 1.0

    def test_actual_drawdown_matches_the_whole_run_figure(self) -> None:
        """The report's own actual figure agrees with the module-level function
        computed on the real, unshuffled curve.
        """
        curve = _curve(_noise(500, seed=10))
        report = monte_carlo_shuffle(curve, resamples=100)
        assert report.actual_max_drawdown == pytest.approx(max_drawdown(curve), abs=1e-9)

    def test_worse_than_shuffled_and_better_than_shuffled_are_mutually_exclusive(
        self,
    ) -> None:
        curve = _curve(_noise(500, seed=10))
        report = monte_carlo_shuffle(curve, resamples=300)
        assert not (bool(report.worse_than_shuffled) and bool(report.better_than_shuffled))

    def test_an_unusually_bad_actual_ordering_is_flagged_worse(self) -> None:
        """A hand-built worst-case ordering (all losses adjacent) against a mild
        base series must rank above nearly every random reshuffle.
        """
        losses = [-0.03] * 6
        gains = [0.01] * 60
        # The worst possible clustering: every loss first, back to back.
        worst_first = losses + gains
        report = monte_carlo_shuffle(_curve(worst_first), resamples=1_000, confidence=0.90)
        assert report.actual_percentile is not None
        assert report.actual_percentile > 0.9

    def test_an_unusually_good_actual_ordering_is_flagged_better(self) -> None:
        """The mirror case: spreading the same losses out as evenly as possible."""
        losses = [-0.03] * 6
        gains = [0.01] * 60
        spread = gains[:10]
        remaining_gains = gains[10:]
        chunk = len(remaining_gains) // len(losses)
        best_spread = list(spread)
        for i, loss in enumerate(losses):
            best_spread.extend(remaining_gains[i * chunk : (i + 1) * chunk])
            best_spread.append(loss)
        best_spread.extend(remaining_gains[len(losses) * chunk :])
        report = monte_carlo_shuffle(_curve(best_spread), resamples=1_000, confidence=0.90)
        assert report.actual_percentile is not None
        assert report.actual_percentile < 0.1


class TestNonsenseArgumentsRaise:
    def test_bad_resamples_raises(self) -> None:
        curve = _curve(_noise(200, seed=15))
        with pytest.raises(ValueError, match="resamples must be >= 1"):
            monte_carlo_shuffle(curve, resamples=0)

    def test_bad_confidence_raises(self) -> None:
        curve = _curve(_noise(200, seed=15))
        with pytest.raises(ValueError, match="confidence must be strictly between"):
            monte_carlo_shuffle(curve, resamples=10, confidence=1.0)
