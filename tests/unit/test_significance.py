"""Fast, no-infra tests for Sharpe significance: bootstrap CIs and deflation (ADR-0039).

Every fixture is a hand-built equity curve driven by a locally-seeded
:class:`random.Random`, so the whole file is deterministic without a network, an
engine, or the global RNG. Where a property is asserted rather than a constant —
"the interval narrows as the sample grows", "blocks widen the interval on an
autocorrelated series" — it is a property that must hold for *any* seed, and the
seed only fixes which instance of it we look at.

The load-bearing test in this file is
:meth:`TestPairedIsActuallyPaired.test_a_uniformly_better_strategy_wins_every_resample`.
It builds a strategy whose return is the benchmark's plus a small constant, which
must win on *every* shared index set by construction — a fact that survives only
if the two series are resampled on the same block indices. The neighbouring test
reimplements the wrong (independent) version and shows it lands near a coin flip
instead.
"""

from __future__ import annotations

import random
import statistics
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from trading.engine import EquityPoint
from trading.metrics import (
    DEFAULT_BLOCK_LENGTH,
    DEFAULT_BOOTSTRAP_SEED,
    MIN_BLOCKS_PER_RESAMPLE,
    MIN_BOOTSTRAP_OBSERVATIONS,
    ReturnMoments,
    assess_significance,
    curve_moments,
    deflated_sharpe,
    effective_block_length,
    expected_max_sharpe,
    paired_bootstrap,
    probabilistic_sharpe_ratio,
    return_moments,
    sharpe,
    sharpe_confidence_interval,
)

_EPOCH = datetime(2000, 1, 3, tzinfo=UTC)


def _curve(
    returns: list[float], *, first_day: int = 0, start: float = 1_000.0
) -> list[EquityPoint]:
    """An equity curve whose per-bar returns are exactly ``returns``."""
    origin = _EPOCH + timedelta(days=first_day)
    points = [EquityPoint(origin, start)]
    equity = start
    for offset, ret in enumerate(returns, start=1):
        equity *= 1.0 + ret
        points.append(EquityPoint(origin + timedelta(days=offset), equity))
    return points


def _noise(n: int, seed: int, *, mean: float = 0.0005, sigma: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sigma) for _ in range(n)]


def _autocorrelated(n: int, seed: int, *, phi: float = 0.6) -> list[float]:
    """An AR(1) return series — the case block resampling exists for."""
    rng = random.Random(seed)
    previous = 0.0
    series: list[float] = []
    for _ in range(n):
        previous = phi * previous + rng.gauss(0.0005, 0.01)
        series.append(previous)
    return series


class TestDeterminism:
    """A randomized statistic in a test bench must still be reproducible."""

    def test_the_same_seed_gives_the_same_interval(self) -> None:
        curve = _curve(_noise(300, seed=1))
        first = sharpe_confidence_interval(curve, resamples=200, seed=4242)
        second = sharpe_confidence_interval(curve, resamples=200, seed=4242)
        assert first == second

    def test_a_different_seed_gives_a_different_interval(self) -> None:
        curve = _curve(_noise(300, seed=1))
        first = sharpe_confidence_interval(curve, resamples=200, seed=1)
        second = sharpe_confidence_interval(curve, resamples=200, seed=2)
        assert first is not None
        assert second is not None
        assert (first.low, first.high) != (second.low, second.high)

    def test_the_seed_is_recorded_on_the_result(self) -> None:
        interval = sharpe_confidence_interval(_curve(_noise(300, seed=1)), resamples=50, seed=77)
        assert interval is not None
        assert interval.seed == 77

    def test_the_default_seed_is_a_fixed_constant_not_a_clock(self) -> None:
        interval = sharpe_confidence_interval(_curve(_noise(300, seed=1)), resamples=50)
        assert interval is not None
        assert interval.seed == DEFAULT_BOOTSTRAP_SEED

    def test_the_global_rng_is_never_touched(self) -> None:
        """The bootstrap must not perturb — or depend on — module-global state."""
        random.seed(12345)
        before = random.getstate()
        curve = _curve(_noise(300, seed=1))
        sharpe_confidence_interval(curve, resamples=100)
        paired_bootstrap(curve, _curve(_noise(300, seed=2)), resamples=100)
        assert random.getstate() == before

    def test_the_paired_win_rate_is_reproducible(self) -> None:
        strategy = _curve(_noise(300, seed=1))
        benchmark = _curve(_noise(300, seed=2))
        first = paired_bootstrap(strategy, benchmark, resamples=200, seed=9)
        second = paired_bootstrap(strategy, benchmark, resamples=200, seed=9)
        assert first == second


class TestBlockLength:
    """60-bar blocks are the intent; a 40-bar run cannot have them."""

    def test_a_long_series_keeps_the_requested_block_length(self) -> None:
        assert effective_block_length(5_000, 60) == 60

    def test_a_short_series_caps_the_block_length(self) -> None:
        # 39 returns / 4 blocks = 9 (floor), not the 60 that was asked for.
        assert effective_block_length(39, 60) == 39 // MIN_BLOCKS_PER_RESAMPLE

    def test_the_cap_never_falls_below_one_bar(self) -> None:
        assert effective_block_length(1, 60) == 1
        assert effective_block_length(0, 60) == 1

    def test_a_non_positive_request_is_a_caller_bug(self) -> None:
        with pytest.raises(ValueError, match="block_length must be >= 1"):
            effective_block_length(1_000, 0)

    def test_a_short_run_reports_that_its_blocks_were_cut(self) -> None:
        interval = sharpe_confidence_interval(_curve(_noise(39, seed=3)), resamples=100)
        assert interval is not None
        assert interval.requested_block_length == DEFAULT_BLOCK_LENGTH
        assert interval.block_length == 9
        assert interval.block_length_was_reduced

    def test_a_long_run_does_not_claim_a_reduction(self) -> None:
        interval = sharpe_confidence_interval(_curve(_noise(1_000, seed=3)), resamples=100)
        assert interval is not None
        assert not interval.block_length_was_reduced

    def test_blocks_widen_the_interval_on_an_autocorrelated_series(self) -> None:
        """Why blocks exist: shuffling single returns throws away the serial structure.

        The same AR(1) series, bootstrapped with 60-bar blocks and with 1-bar
        (i.i.d.) blocks. The i.i.d. version breaks the autocorrelation and reports a
        materially *narrower* interval — a confident number that the data does not
        support. If a future change quietly drops block resampling, this inequality
        flips.
        """
        curve = _curve(_autocorrelated(1_000, seed=7))
        blocked = sharpe_confidence_interval(curve, resamples=300, block_length=60)
        shuffled = sharpe_confidence_interval(curve, resamples=300, block_length=1)
        assert blocked is not None
        assert shuffled is not None
        assert blocked.width > shuffled.width


class TestTooShortToBootstrap:
    """A garbage interval is worse than no interval, so there is a floor."""

    def test_below_the_floor_there_is_no_interval(self) -> None:
        curve = _curve(_noise(MIN_BOOTSTRAP_OBSERVATIONS - 1, seed=5))
        assert sharpe_confidence_interval(curve, resamples=100) is None

    def test_exactly_at_the_floor_an_interval_exists(self) -> None:
        curve = _curve(_noise(MIN_BOOTSTRAP_OBSERVATIONS, seed=5))
        interval = sharpe_confidence_interval(curve, resamples=100)
        assert interval is not None
        assert interval.observations == MIN_BOOTSTRAP_OBSERVATIONS

    def test_a_flat_curve_has_no_interval_rather_than_a_zero_width_one(self) -> None:
        """A curve that never moves has a Sharpe of 0.0 by convention, not a distribution."""
        assert sharpe_confidence_interval(_curve([0.0] * 100), resamples=50) is None

    def test_the_absence_is_explained_in_words(self) -> None:
        report = assess_significance(_curve(_noise(10, seed=5)), resamples=50)
        assert report.sharpe_interval is None
        assert any("below the 30" in note for note in report.notes)

    def test_the_reduction_is_explained_in_words(self) -> None:
        report = assess_significance(_curve(_noise(40, seed=5)), resamples=50)
        assert any("block length reduced from 60 to 10" in note for note in report.notes)


class TestIntervalShape:
    def test_the_point_estimate_is_the_sharpe_the_report_prints(self) -> None:
        curve = _curve(_noise(500, seed=11))
        interval = sharpe_confidence_interval(curve, resamples=100)
        assert interval is not None
        assert interval.point == sharpe(curve)

    def test_the_interval_brackets_the_point_estimate(self) -> None:
        curve = _curve(_noise(1_000, seed=12))
        interval = sharpe_confidence_interval(curve, resamples=400)
        assert interval is not None
        assert interval.low < interval.point < interval.high

    def test_the_interval_narrows_as_the_sample_grows(self) -> None:
        """The headline claim of the ticket: more data pins the Sharpe down harder."""
        returns = _noise(2_500, seed=4)
        short = sharpe_confidence_interval(_curve(returns[:250]), resamples=300)
        long = sharpe_confidence_interval(_curve(returns), resamples=300)
        assert short is not None
        assert long is not None
        assert long.width < short.width

    def test_a_wider_confidence_level_gives_a_wider_interval(self) -> None:
        curve = _curve(_noise(1_000, seed=13))
        ninety = sharpe_confidence_interval(curve, resamples=400, confidence=0.90)
        ninety_nine = sharpe_confidence_interval(curve, resamples=400, confidence=0.99)
        assert ninety is not None
        assert ninety_nine is not None
        assert ninety_nine.width > ninety.width

    def test_straddling_zero_is_reported_as_such(self) -> None:
        # A pure-noise series with no drift: the interval must not exclude zero.
        curve = _curve(_noise(500, seed=14, mean=0.0))
        interval = sharpe_confidence_interval(curve, resamples=400)
        assert interval is not None
        assert interval.low <= 0.0 <= interval.high
        assert interval.straddles_zero

    def test_nonsense_arguments_raise_rather_than_returning_none(self) -> None:
        curve = _curve(_noise(200, seed=15))
        with pytest.raises(ValueError, match="resamples must be >= 1"):
            sharpe_confidence_interval(curve, resamples=0)
        with pytest.raises(ValueError, match="confidence must be strictly between"):
            sharpe_confidence_interval(curve, resamples=10, confidence=1.0)


def _local_stationary_indices(n: int, block_length: int, rng: random.Random) -> list[int]:
    """A test-local stationary bootstrap, written independently of the module."""
    indices = []
    position = rng.randrange(n)
    for _ in range(n):
        indices.append(position)
        restarts = rng.random() < 1.0 / block_length
        position = rng.randrange(n) if restarts else (position + 1) % n
    return indices


def _step_returns(curve: list[EquityPoint]) -> list[float]:
    """Per-bar returns of an equity curve, computed here rather than imported."""
    return [b.equity / a.equity - 1.0 for a, b in pairwise(curve)]


def _local_sharpe(returns: list[float]) -> float:
    """Annualized Sharpe from the standard library, as an independent reference."""
    stdev = statistics.stdev(returns)
    if stdev == 0.0:
        return 0.0
    return float(statistics.fmean(returns) / stdev * (252.0**0.5))


class TestPairedIsActuallyPaired:
    """The guard: the two series must be resampled on ONE shared index sequence."""

    # The strategy earns the benchmark's return plus this constant every single
    # bar. On any common set of indices its mean is higher by exactly EDGE and its
    # standard deviation is identical, so its Sharpe is strictly higher — always,
    # by construction, not by luck.
    EDGE = 0.0002

    def _pair(self) -> tuple[list[EquityPoint], list[EquityPoint]]:
        base = _noise(500, seed=99, mean=0.0, sigma=0.02)
        return _curve([r + self.EDGE for r in base]), _curve(base)

    def test_a_uniformly_better_strategy_wins_every_resample(self) -> None:
        strategy, benchmark = self._pair()
        paired = paired_bootstrap(strategy, benchmark, resamples=200)
        assert paired is not None
        assert paired.win_rate == 1.0

    def test_resampling_the_two_independently_lands_near_a_coin_flip(self) -> None:
        """The mistake this design exists to prevent, reproduced locally.

        Same fixture, same block bootstrap — but two *separate* index sequences.
        The uniform edge that guarantees a 100% paired win rate all but vanishes,
        because the strategy in one imaginary market is being compared against the
        benchmark in a different one. Any implementation that scores materially
        below 1.0 above has made this mistake.
        """
        strategy, benchmark = self._pair()
        strategy_returns = _step_returns(strategy)
        bench_returns = _step_returns(benchmark)
        rng = random.Random(DEFAULT_BOOTSTRAP_SEED)
        n = len(strategy_returns)
        wins = 0
        for _ in range(200):
            one = _local_stationary_indices(n, DEFAULT_BLOCK_LENGTH, rng)
            other = _local_stationary_indices(n, DEFAULT_BLOCK_LENGTH, rng)
            if _local_sharpe([strategy_returns[i] for i in one]) > _local_sharpe(
                [bench_returns[i] for i in other]
            ):
                wins += 1
        assert wins / 200 < 0.9

    def test_the_observed_edge_is_the_unresampled_sharpe_difference(self) -> None:
        strategy, benchmark = self._pair()
        paired = paired_bootstrap(strategy, benchmark, resamples=50)
        assert paired is not None
        assert paired.observed_edge == pytest.approx(sharpe(strategy) - sharpe(benchmark))

    def test_a_uniformly_worse_strategy_never_wins(self) -> None:
        base = _noise(500, seed=99, mean=0.0, sigma=0.02)
        worse = _curve([r - self.EDGE for r in base])
        paired = paired_bootstrap(worse, _curve(base), resamples=200)
        assert paired is not None
        assert paired.win_rate == 0.0


class TestPairedAlignment:
    """The pairing is by timestamp, reusing ADR-0037's alignment — never by position."""

    def test_only_the_shared_timestamps_are_resampled(self) -> None:
        returns = _noise(200, seed=21)
        strategy = _curve(returns)
        # The benchmark starts 50 days later, so 151 of the strategy's 201 points
        # are shared and 150 return periods survive alignment.
        benchmark = _curve(returns[50:], first_day=50)
        paired = paired_bootstrap(strategy, benchmark, resamples=50)
        assert paired is not None
        assert paired.observations == 150

    def test_too_few_shared_periods_yields_no_figure(self) -> None:
        returns = _noise(200, seed=21)
        strategy = _curve(returns)
        benchmark = _curve(returns[190:], first_day=190)
        assert paired_bootstrap(strategy, benchmark, resamples=50) is None

    def test_the_absence_is_explained_in_words(self) -> None:
        returns = _noise(200, seed=21)
        report = assess_significance(
            _curve(returns), _curve(returns[190:], first_day=190), resamples=50
        )
        assert report.paired is None
        assert any("share fewer than" in note for note in report.notes)

    def test_no_benchmark_is_a_stated_absence(self) -> None:
        report = assess_significance(_curve(_noise(200, seed=21)), resamples=50)
        assert report.paired is None
        assert any("no benchmark ran" in note for note in report.notes)


class TestReturnMoments:
    def test_moments_match_the_standard_library(self) -> None:
        returns = _noise(500, seed=31)
        moments = return_moments(returns)
        assert moments is not None
        assert moments.count == 500
        assert moments.mean == pytest.approx(statistics.fmean(returns))
        assert moments.stdev == pytest.approx(statistics.stdev(returns))

    def test_a_normal_series_scores_a_kurtosis_near_three(self) -> None:
        """Kurtosis is non-excess here: a normal series is ~3.0, not ~0.0."""
        moments = return_moments(_noise(20_000, seed=32))
        assert moments is not None
        assert moments.kurtosis == pytest.approx(3.0, abs=0.15)
        assert moments.skew == pytest.approx(0.0, abs=0.1)

    def test_a_series_with_no_dispersion_has_no_moments(self) -> None:
        assert return_moments([0.01] * 50) is None

    def test_too_few_returns_have_no_moments(self) -> None:
        assert return_moments([0.01]) is None

    def test_curve_moments_reads_the_curve_returns(self) -> None:
        returns = _noise(100, seed=33)
        from_curve = curve_moments(_curve(returns))
        assert from_curve is not None
        assert from_curve.count == 100


class TestExpectedMaxSharpe:
    """The null a search must clear: the best of N skill-free trials is not zero."""

    def test_one_trial_needs_no_deflation(self) -> None:
        assert expected_max_sharpe(1, 1.0) == 0.0

    def test_identical_trials_offer_no_room_to_get_lucky(self) -> None:
        assert expected_max_sharpe(24, 0.0) == 0.0

    def test_more_trials_raise_the_bar(self) -> None:
        assert expected_max_sharpe(2, 1.0) < expected_max_sharpe(24, 1.0)
        assert expected_max_sharpe(24, 1.0) < expected_max_sharpe(100, 1.0)

    def test_it_scales_linearly_with_the_spread(self) -> None:
        assert expected_max_sharpe(24, 2.0) == pytest.approx(2.0 * expected_max_sharpe(24, 1.0))

    def test_twenty_four_trials_matches_the_transcribed_value(self) -> None:
        """Pinned so a change to the approximation is visible, not silent.

        Reproduce with::

            uv run python -c "from trading.metrics import expected_max_sharpe; \\
                print(expected_max_sharpe(24, 1.0))"
        """
        assert expected_max_sharpe(24, 1.0) == pytest.approx(1.9797731141615635)


class TestProbabilisticSharpe:
    def test_the_probability_is_one_half_at_the_observed_sharpe(self) -> None:
        """A clean property: the estimate is the median of its own distribution."""
        moments = ReturnMoments(count=1_000, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        assert probabilistic_sharpe_ratio(moments, 0.1) == pytest.approx(0.5)

    def test_a_higher_threshold_lowers_the_probability(self) -> None:
        moments = ReturnMoments(count=1_000, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        low = probabilistic_sharpe_ratio(moments, 0.0)
        high = probabilistic_sharpe_ratio(moments, 0.05)
        assert low is not None
        assert high is not None
        assert high < low

    def test_more_observations_raise_confidence_in_the_same_sharpe(self) -> None:
        short = ReturnMoments(count=100, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        long = ReturnMoments(count=5_000, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        short_p = probabilistic_sharpe_ratio(short)
        long_p = probabilistic_sharpe_ratio(long)
        assert short_p is not None
        assert long_p is not None
        assert long_p > short_p

    def test_negative_skew_and_fat_tails_cost_confidence(self) -> None:
        clean = ReturnMoments(count=1_000, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        ugly = ReturnMoments(count=1_000, mean=0.001, stdev=0.01, skew=-2.0, kurtosis=12.0)
        clean_p = probabilistic_sharpe_ratio(clean)
        ugly_p = probabilistic_sharpe_ratio(ugly)
        assert clean_p is not None
        assert ugly_p is not None
        assert ugly_p < clean_p

    def test_an_impossible_variance_correction_is_none_not_a_number(self) -> None:
        # A huge per-bar Sharpe against strong positive skew drives the correction
        # non-positive; the answer is "undefined", never a clipped 1.0.
        moments = ReturnMoments(count=100, mean=5.0, stdev=1.0, skew=10.0, kurtosis=3.0)
        assert probabilistic_sharpe_ratio(moments) is None


class TestDeflatedSharpe:
    @staticmethod
    def _moments() -> ReturnMoments:
        result = curve_moments(_curve(_noise(1_000, seed=41)))
        assert result is not None
        return result

    def test_a_lone_run_is_one_trial_not_zero(self) -> None:
        deflated = deflated_sharpe(self._moments(), [1.2])
        assert deflated is not None
        assert deflated.trials == 1
        assert deflated.null_best_sharpe == 0.0
        assert deflated.trial_sharpe_stdev is None

    def test_more_trials_raise_the_null_and_lower_the_probability(self) -> None:
        moments = self._moments()
        spread = [0.2 * i for i in range(24)]
        few = deflated_sharpe(moments, spread[:3])
        many = deflated_sharpe(moments, spread)
        assert few is not None
        assert many is not None
        assert many.null_best_sharpe > few.null_best_sharpe
        assert few.probability is not None
        assert many.probability is not None
        assert many.probability < few.probability

    def test_the_observed_sharpe_is_the_run_s_own(self) -> None:
        curve = _curve(_noise(1_000, seed=41))
        deflated = deflated_sharpe(self._moments(), [1.0, 2.0])
        assert deflated is not None
        assert deflated.observed_sharpe == pytest.approx(sharpe(curve))

    def test_significance_needs_the_probability_and_a_known_one(self) -> None:
        moments = ReturnMoments(count=5_000, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        confident = deflated_sharpe(moments, [1.6])
        assert confident is not None
        assert confident.significant
        crowded = deflated_sharpe(moments, [0.4 * i for i in range(1, 60)])
        assert crowded is not None
        assert not crowded.significant

    def test_no_trials_at_all_is_a_caller_bug(self) -> None:
        with pytest.raises(ValueError, match="at least the run being deflated"):
            deflated_sharpe(self._moments(), [])

    def test_annualization_cancels_out_of_the_comparison(self) -> None:
        """The trial spread and the run's Sharpe must be in the same units.

        Feeding annualized trial Sharpes at one frequency and at another must not
        change *which* side of the null the winner lands on, only the scale of the
        printed numbers.
        """
        moments = ReturnMoments(count=1_000, mean=0.001, stdev=0.01, skew=0.0, kurtosis=3.0)
        daily = deflated_sharpe(moments, [1.0, 2.0, 3.0], 252.0)
        hourly = deflated_sharpe(
            moments, [s * (1_638.0 / 252.0) ** 0.5 for s in (1.0, 2.0, 3.0)], 1_638.0
        )
        assert daily is not None
        assert hourly is not None
        assert daily.probability == pytest.approx(hourly.probability)


class TestAssessSignificance:
    def test_it_always_returns_an_object(self) -> None:
        report = assess_significance(_curve([]), resamples=10)
        assert report.sharpe_interval is None
        assert report.paired is None
        assert report.deflated is None
        assert report.notes

    def test_a_lone_backtest_counts_itself_as_one_trial(self) -> None:
        report = assess_significance(_curve(_noise(500, seed=51)), resamples=50)
        assert report.deflated is not None
        assert report.deflated.trials == 1
        assert any("counts 1 trial(s)" in note for note in report.notes)

    def test_the_invisible_trials_caveat_is_always_stated(self) -> None:
        report = assess_significance(
            _curve(_noise(500, seed=51)), resamples=50, trial_sharpes=[0.4, 0.9, 1.3]
        )
        assert report.deflated is not None
        assert report.deflated.trials == 3
        assert any("LOWER BOUND" in note for note in report.notes)

    def test_a_benchmark_turns_on_the_paired_figure(self) -> None:
        returns = _noise(500, seed=52)
        report = assess_significance(
            _curve([r + 0.0002 for r in returns]), _curve(returns), resamples=50
        )
        assert report.paired is not None
        assert report.paired.win_rate == 1.0

    def test_the_whole_report_is_reproducible(self) -> None:
        returns = _noise(400, seed=53)
        args = (_curve([r + 0.0001 for r in returns]), _curve(returns))
        assert assess_significance(*args, resamples=100) == assess_significance(
            *args, resamples=100
        )
