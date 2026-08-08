"""Fast, no-infra unit tests for the V4 performance metrics.

Fixtures are built by hand (tz-aware timestamps) so every expected value is a
transcribed hand computation, not a re-derivation of the code under test. The
Sharpe check uses the standard library's :mod:`statistics` as an independent
reference for sample mean/stdev.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from math import sqrt
from typing import ClassVar

import pytest

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import (
    MIN_TRADES_PER_PARAMETER,
    PerformanceMetrics,
    align_curves,
    aligned_returns,
    alpha,
    annualized_return,
    avg_exposure,
    beta,
    calmar,
    compare_to_benchmark,
    compute,
    correlation,
    daily_returns,
    entry_count,
    information_ratio,
    max_drawdown,
    peak_exposure,
    return_per_unit_exposure,
    sharpe,
    sortino,
    total_return,
    trades_per_parameter,
    turnover,
    win_rate,
)
from trading.types import Fill, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _curve(equities: list[float]) -> list[EquityPoint]:
    return [EquityPoint(_ts(i + 1), e) for i, e in enumerate(equities)]


def _curve_from(day: int, equities: list[float]) -> list[EquityPoint]:
    """An equity curve whose first point sits on ``day`` (for misalignment tests)."""
    return [EquityPoint(_ts(day + i), e) for i, e in enumerate(equities)]


def _curve_with_exposure(equities: list[float], exposures: list[float]) -> list[EquityPoint]:
    return [
        EquityPoint(_ts(i + 1), e, x)
        for i, (e, x) in enumerate(zip(equities, exposures, strict=True))
    ]


class TestMonotonicUpCurve:
    """SLICES V4 acceptance: rising curve → positive return, Sharpe > 0, no DD."""

    curve: ClassVar[list[EquityPoint]] = _curve([100.0, 101.0, 102.0, 103.0, 104.0])

    def test_total_return_positive(self) -> None:
        assert total_return(self.curve) > 0

    def test_sharpe_positive(self) -> None:
        assert sharpe(self.curve) > 0

    def test_max_drawdown_zero(self) -> None:
        assert max_drawdown(self.curve) == 0.0


class TestMaxDrawdown:
    def test_known_dip_exact(self) -> None:
        # Peak 120 at bar 2, trough 90 at bar 3 → (120 - 90) / 120 = 0.25.
        curve = _curve([100.0, 120.0, 90.0, 110.0, 105.0])
        assert max_drawdown(curve) == 0.25

    def test_empty_curve_zero(self) -> None:
        assert max_drawdown([]) == 0.0


class TestDailyReturns:
    def test_simple_steps(self) -> None:
        curve = _curve([100.0, 110.0, 104.5, 125.4])
        rets = daily_returns(curve)
        assert rets == pytest.approx([0.1, -0.05, 0.2])

    def test_too_short_is_empty(self) -> None:
        assert daily_returns(_curve([100.0])) == []


class TestSharpe:
    def test_matches_reference_stats(self) -> None:
        # Returns 0.10, -0.05, 0.20 built into the equity series.
        curve = _curve([100.0, 110.0, 104.5, 125.4])
        rets = daily_returns(curve)
        mean = statistics.fmean(rets)
        stdev = statistics.stdev(rets)  # sample stdev (n - 1)
        expected = mean / stdev * sqrt(252)
        assert sharpe(curve) == pytest.approx(expected, rel=1e-12)

    def test_flat_curve_is_zero(self) -> None:
        assert sharpe(_curve([100.0, 100.0, 100.0])) == 0.0

    def test_single_return_is_zero(self) -> None:
        assert sharpe(_curve([100.0, 110.0])) == 0.0


class TestAnnualizedReturn:
    def test_geometric_hand_value(self) -> None:
        # total = 120 / 100 - 1 = 0.2 over n = 3 periods → 1.2 ** (252 / 3) - 1.
        curve = _curve([100.0, 90.0, 130.0, 120.0])
        expected = 1.2 ** (252 / 3) - 1.0
        assert annualized_return(curve) == pytest.approx(expected, rel=1e-12)

    def test_no_periods_is_zero(self) -> None:
        assert annualized_return(_curve([100.0])) == 0.0


class TestWinRate:
    def test_two_wins_one_loss(self) -> None:
        # A: buy 10 @100, sell @120 (win); B: buy 5 @50, sell @40 (loss);
        # A: buy 10 @100, sell @130 (win). 2 wins / 3 closing trades.
        fills = [
            (_ts(1), Fill("A", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("A", Side.SELL, 10.0, 120.0)),
            (_ts(3), Fill("B", Side.BUY, 5.0, 50.0)),
            (_ts(4), Fill("B", Side.SELL, 5.0, 40.0)),
            (_ts(5), Fill("A", Side.BUY, 10.0, 100.0)),
            (_ts(6), Fill("A", Side.SELL, 10.0, 130.0)),
        ]
        assert win_rate(fills) == pytest.approx(2 / 3)

    def test_blended_cost_basis(self) -> None:
        # Buy 10 @100 then 10 @200 → avg cost 150; sell @160 beats 150 → a win.
        fills = [
            (_ts(1), Fill("A", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("A", Side.BUY, 10.0, 200.0)),
            (_ts(3), Fill("A", Side.SELL, 10.0, 160.0)),
        ]
        assert win_rate(fills) == 1.0

    def test_no_closing_trades_is_zero(self) -> None:
        fills = [(_ts(1), Fill("A", Side.BUY, 10.0, 100.0))]
        assert win_rate(fills) == 0.0


class TestSortino:
    def test_literal_two_returns(self) -> None:
        # Returns 0.20, -0.10; mean 0.05; the only downside shortfall is -0.10, so
        # downside deviation = sqrt(0.10**2 / (2 - 1)) = 0.10.
        curve = _curve([100.0, 120.0, 108.0])
        assert sortino(curve) == pytest.approx(0.05 / 0.10 * sqrt(252))

    def test_matches_hand_downside(self) -> None:
        curve = _curve([100.0, 110.0, 104.5, 125.4])
        rets = daily_returns(curve)
        mean = statistics.fmean(rets)
        downside_dev = sqrt(sum(min(r, 0.0) ** 2 for r in rets) / (len(rets) - 1))
        expected = mean / downside_dev * sqrt(252)
        assert sortino(curve) == pytest.approx(expected, rel=1e-12)

    def test_no_downside_is_zero(self) -> None:
        # Strictly rising: no negative returns → downside deviation 0 → 0.0.
        assert sortino(_curve([100.0, 110.0, 121.0])) == 0.0

    def test_single_return_is_zero(self) -> None:
        assert sortino(_curve([100.0, 110.0])) == 0.0


class TestCalmar:
    def test_annualized_over_max_drawdown(self) -> None:
        # Max drawdown 0.25 (peak 120 → trough 90); calmar = annualized / 0.25.
        curve = _curve([100.0, 120.0, 90.0, 110.0, 105.0])
        assert calmar(curve) == pytest.approx(annualized_return(curve) / 0.25)

    def test_zero_drawdown_is_zero(self) -> None:
        assert calmar(_curve([100.0, 101.0, 102.0])) == 0.0


class TestTurnover:
    def test_traded_notional_over_avg_equity_annualized(self) -> None:
        # Two bars (avg equity 100); buy 10 @100 then sell 10 @100 → traded 2,000.
        # turnover = 2000 / 100 * (252 / 2).
        curve = _curve([100.0, 100.0])
        fills = [
            (_ts(1), Fill("A", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("A", Side.SELL, 10.0, 100.0)),
        ]
        assert turnover(fills, curve) == pytest.approx(2000.0 / 100.0 * (252 / 2))

    def test_no_fills_is_zero(self) -> None:
        assert turnover([], _curve([100.0, 100.0])) == 0.0

    def test_empty_curve_is_zero(self) -> None:
        assert turnover([], []) == 0.0


class TestExposureHelpers:
    exposures: ClassVar[list[float]] = [0.5, 1.0, 0.75, 0.25]

    def test_avg_exposure(self) -> None:
        assert avg_exposure(self.exposures) == pytest.approx(0.625)

    def test_peak_exposure(self) -> None:
        assert peak_exposure(self.exposures) == 1.0

    def test_empty_series(self) -> None:
        assert avg_exposure([]) == 0.0
        assert peak_exposure([]) == 0.0


class TestPeriodsPerYear:
    """ADR-0022: annualization scales with periods_per_year; 252.0 is the default."""

    curve: ClassVar[list[EquityPoint]] = _curve([100.0, 110.0, 104.5, 125.4])

    def test_default_matches_252(self) -> None:
        # The explicit daily basis reproduces the default exactly (no behaviour drift).
        assert sharpe(self.curve) == pytest.approx(sharpe(self.curve, 252.0))
        assert annualized_return(self.curve) == pytest.approx(annualized_return(self.curve, 252.0))

    def test_sharpe_scales_by_sqrt_of_periods(self) -> None:
        # Sharpe annualizes by √periods_per_year, so the ratio of two bases is the
        # ratio of their square roots — independent of the return series.
        daily = sharpe(self.curve, 252.0)
        hourly_ppy = 252.0 * 6.5  # a 1-hour bar
        assert sharpe(self.curve, hourly_ppy) == pytest.approx(daily * sqrt(hourly_ppy / 252.0))

    def test_intraday_sharpe_differs_from_daily(self) -> None:
        assert sharpe(self.curve, 252.0 * 6.5) != pytest.approx(sharpe(self.curve, 252.0))

    def test_compute_threads_periods_per_year(self) -> None:
        from trading.engine import BacktestResult
        from trading.types import Portfolio

        result = BacktestResult(
            symbols=["A"],
            starting_cash=100.0,
            equity_curve=self.curve,
            final_portfolio=Portfolio(cash=125.4),
        )
        hourly_ppy = 252.0 * 6.5
        metrics = compute(result, periods_per_year=hourly_ppy)
        assert metrics.sharpe == pytest.approx(sharpe(self.curve, hourly_ppy))
        assert metrics.annualized_return == pytest.approx(annualized_return(self.curve, hourly_ppy))
        # The default still reproduces the daily numbers.
        assert compute(result).sharpe == pytest.approx(sharpe(self.curve, 252.0))


class TestCompute:
    def test_assembles_metrics_from_result(self) -> None:
        from trading.engine import BacktestResult
        from trading.types import Portfolio

        curve = _curve([100.0, 120.0, 90.0, 110.0, 105.0])
        fills = [
            (_ts(1), Fill("A", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("A", Side.SELL, 10.0, 120.0)),
        ]
        result = BacktestResult(
            symbols=["A"],
            starting_cash=100.0,
            equity_curve=curve,
            final_portfolio=Portfolio(cash=105.0),
            fills=fills,
        )
        metrics = compute(result)
        assert isinstance(metrics, PerformanceMetrics)
        assert metrics.total_return == pytest.approx(0.05)
        assert metrics.max_drawdown == 0.25
        assert metrics.win_rate == 1.0
        assert metrics.sharpe == pytest.approx(sharpe(curve))
        assert metrics.annualized_return == pytest.approx(annualized_return(curve))
        assert metrics.sortino == pytest.approx(sortino(curve))
        assert metrics.calmar == pytest.approx(calmar(curve))
        assert metrics.turnover == pytest.approx(turnover(result.fills, curve))

    def test_surfaces_exposure_from_curve(self) -> None:
        # Curve carries a known per-bar exposure series → avg 0.625, peak 1.0.
        from trading.engine import BacktestResult, EquityPoint
        from trading.types import Portfolio

        exposures = [0.5, 1.0, 0.75, 0.25]
        curve = [EquityPoint(_ts(i + 1), 100.0, e) for i, e in enumerate(exposures)]
        result = BacktestResult(
            symbols=["A"],
            starting_cash=100.0,
            equity_curve=curve,
            final_portfolio=Portfolio(cash=100.0),
        )
        metrics = compute(result)
        assert metrics.avg_exposure == pytest.approx(0.625)
        assert metrics.peak_exposure == 1.0

    def test_exposure_defaults_to_zero_when_curve_flat(self) -> None:
        from trading.engine import BacktestResult
        from trading.types import Portfolio

        # EquityPoint.exposure defaults to 0.0 → both exposure metrics are 0.
        result = BacktestResult(
            symbols=["A"],
            starting_cash=100.0,
            equity_curve=_curve([100.0, 101.0, 102.0]),
            final_portfolio=Portfolio(cash=102.0),
        )
        metrics = compute(result)
        assert metrics.avg_exposure == 0.0
        assert metrics.peak_exposure == 0.0


class TestEntryCount:
    """Position-opening entries, not raw fills (ADR-0029)."""

    def test_round_trip_counts_as_one_entry(self) -> None:
        fills = [
            (_ts(1), Fill(symbol="A", side=Side.BUY, qty=10.0, price=100.0)),
            (_ts(2), Fill(symbol="A", side=Side.SELL, qty=10.0, price=110.0)),
        ]
        assert entry_count(fills) == 1

    def test_reentry_after_a_full_exit_is_a_second_entry(self) -> None:
        fills = [
            (_ts(1), Fill(symbol="A", side=Side.BUY, qty=10.0, price=100.0)),
            (_ts(2), Fill(symbol="A", side=Side.SELL, qty=10.0, price=110.0)),
            (_ts(3), Fill(symbol="A", side=Side.BUY, qty=5.0, price=105.0)),
        ]
        assert entry_count(fills) == 2

    def test_adding_to_an_open_position_is_not_a_new_entry(self) -> None:
        """A rebalance top-up must not inflate the significance denominator."""
        fills = [
            (_ts(1), Fill(symbol="A", side=Side.BUY, qty=10.0, price=100.0)),
            (_ts(2), Fill(symbol="A", side=Side.BUY, qty=5.0, price=101.0)),
            (_ts(3), Fill(symbol="A", side=Side.BUY, qty=5.0, price=102.0)),
        ]
        assert entry_count(fills) == 1

    def test_partial_exit_then_top_up_is_not_a_new_entry(self) -> None:
        fills = [
            (_ts(1), Fill(symbol="A", side=Side.BUY, qty=10.0, price=100.0)),
            (_ts(2), Fill(symbol="A", side=Side.SELL, qty=4.0, price=110.0)),
            (_ts(3), Fill(symbol="A", side=Side.BUY, qty=2.0, price=108.0)),
        ]
        assert entry_count(fills) == 1

    def test_entries_are_counted_per_symbol(self) -> None:
        fills = [
            (_ts(1), Fill(symbol="A", side=Side.BUY, qty=1.0, price=10.0)),
            (_ts(1), Fill(symbol="B", side=Side.BUY, qty=1.0, price=10.0)),
            (_ts(1), Fill(symbol="C", side=Side.BUY, qty=1.0, price=10.0)),
        ]
        assert entry_count(fills) == 3

    def test_still_open_position_counts(self) -> None:
        fills = [(_ts(1), Fill(symbol="A", side=Side.BUY, qty=10.0, price=100.0))]
        assert entry_count(fills) == 1

    def test_no_fills_is_zero(self) -> None:
        assert entry_count([]) == 0


class TestTradesPerParameter:
    def test_ratio_is_entries_over_parameters(self) -> None:
        # Three separate round trips -> 3 entries; 2 free parameters -> 1.5.
        fills = []
        for day in (1, 3, 5):
            fills.append((_ts(day), Fill(symbol="A", side=Side.BUY, qty=1.0, price=10.0)))
            fills.append((_ts(day + 1), Fill(symbol="A", side=Side.SELL, qty=1.0, price=11.0)))
        assert trades_per_parameter(fills, 2) == pytest.approx(1.5)

    def test_unknown_parameter_count_is_none_not_zero(self) -> None:
        """An absent check must not read as a failed check."""
        fills = [(_ts(1), Fill(symbol="A", side=Side.BUY, qty=1.0, price=10.0))]
        assert trades_per_parameter(fills, None) is None

    def test_zero_parameter_strategy_reports_none(self) -> None:
        fills = [(_ts(1), Fill(symbol="A", side=Side.BUY, qty=1.0, price=10.0))]
        assert trades_per_parameter(fills, 0) is None


class TestUnderpowered:
    def _metrics(self, ratio: float | None) -> PerformanceMetrics:
        return PerformanceMetrics(
            total_return=0.0,
            annualized_return=0.0,
            sharpe=0.0,
            sortino=0.0,
            calmar=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            turnover=0.0,
            avg_exposure=0.0,
            peak_exposure=0.0,
            trade_count=0,
            trades_per_parameter=ratio,
        )

    def test_below_the_floor_is_underpowered(self) -> None:
        assert self._metrics(MIN_TRADES_PER_PARAMETER - 0.1).underpowered

    def test_at_the_floor_is_not_underpowered(self) -> None:
        assert not self._metrics(MIN_TRADES_PER_PARAMETER).underpowered

    def test_unknown_ratio_is_not_a_failure(self) -> None:
        assert not self._metrics(None).underpowered


class TestComputeSignificance:
    def _result(self, entries: int) -> object:
        from trading.engine import BacktestResult
        from trading.types import Portfolio

        fills = []
        for i in range(entries):
            fills.append((_ts(1), Fill(symbol=f"S{i}", side=Side.BUY, qty=1.0, price=10.0)))
        return BacktestResult(
            symbols=["A"],
            starting_cash=100.0,
            equity_curve=_curve([100.0, 101.0]),
            final_portfolio=Portfolio(cash=100.0),
            fills=fills,
        )

    def test_trade_count_always_populated(self) -> None:
        metrics = compute(self._result(4))  # type: ignore[arg-type]
        assert metrics.trade_count == 4
        assert metrics.trades_per_parameter is None

    def test_free_parameters_turns_on_the_ratio(self) -> None:
        metrics = compute(self._result(60), free_parameters=4)  # type: ignore[arg-type]
        assert metrics.trades_per_parameter == pytest.approx(15.0)
        assert metrics.underpowered

    def test_ample_sample_is_not_underpowered(self) -> None:
        metrics = compute(self._result(60), free_parameters=2)  # type: ignore[arg-type]
        assert metrics.trades_per_parameter == pytest.approx(30.0)
        assert not metrics.underpowered


# --- Benchmark-relative metrics (ADR-0037) -----------------------------------


def _bench_result(curve: list[EquityPoint]) -> BacktestResult:
    """A minimal :class:`BacktestResult` wrapper around a hand-built curve."""
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=curve[0].equity,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=curve[-1].equity),
        fills=[],
    )


class TestReturnPerUnitExposure:
    """Annualized return divided by average gross exposure — the comparability lens."""

    def test_divides_annualized_return_by_average_exposure(self) -> None:
        curve = _curve_with_exposure([100.0, 110.0, 120.0], [0.5, 0.5, 0.5])
        expected = annualized_return(curve) / 0.5
        assert return_per_unit_exposure(curve) == pytest.approx(expected)

    def test_never_invested_is_none_not_zero(self) -> None:
        # A book that never held anything has no "per unit of exposure" to report;
        # 0.0 would read as a *bad* result rather than an undefined one.
        assert return_per_unit_exposure(_curve_with_exposure([100.0, 110.0], [0.0, 0.0])) is None

    def test_reranks_a_lightly_invested_strategy_above_a_fully_invested_one(self) -> None:
        # The ticket's finding: the 17%-invested book earns less in raw terms but
        # far more per dollar actually at risk, and only the second view compares.
        # periods_per_year == the number of return periods, so the annualized
        # figure equals the total return and the arithmetic stays hand-checkable:
        # 3% / 0.17 = 17.6% beats 8% / 0.90 = 8.9%.
        light = _curve_with_exposure([100.0, 101.0, 102.0, 103.0], [0.17] * 4)
        heavy = _curve_with_exposure([100.0, 103.0, 105.0, 108.0], [0.90] * 4)
        assert total_return(light) < total_return(heavy)
        light_per_unit = return_per_unit_exposure(light, periods_per_year=3.0)
        heavy_per_unit = return_per_unit_exposure(heavy, periods_per_year=3.0)
        assert light_per_unit == pytest.approx(0.03 / 0.17)
        assert heavy_per_unit == pytest.approx(0.08 / 0.90)
        assert light_per_unit is not None and heavy_per_unit is not None
        assert light_per_unit > heavy_per_unit


class TestAlignment:
    """Curves are paired by timestamp, never positionally (the whole problem)."""

    def test_only_shared_timestamps_survive(self) -> None:
        own = _curve([100.0, 101.0, 102.0, 103.0])  # days 1-4
        bench = _curve_from(3, [50.0, 51.0, 52.0])  # days 3-5
        rows = align_curves(own, bench)
        assert [row[0] for row in rows] == [_ts(3), _ts(4)]
        assert [row[1] for row in rows] == [102.0, 103.0]
        assert [row[2] for row in rows] == [50.0, 51.0]

    def test_offset_curves_are_not_zipped_positionally(self) -> None:
        # A benchmark that is the strategy's own series shifted two days forward.
        # Zipping by index would pair every bar with an identical value and report
        # a perfect correlation; aligning by timestamp finds only two shared days.
        equities = [100.0, 120.0, 90.0, 130.0, 95.0, 140.0]
        own = _curve(equities)
        bench = _curve_from(3, equities)
        rows = align_curves(own, bench)
        assert len(rows) == 4  # days 3-6 of the strategy vs days 3-6 of the benchmark
        assert correlation(own, bench) != pytest.approx(1.0)
        # Sanity: zipped positionally the two series *are* identical.
        assert [p.equity for p in own] == [p.equity for p in bench]

    def test_a_gap_in_the_benchmark_bridges_the_step_on_both_sides(self) -> None:
        own = _curve([100.0, 110.0, 121.0, 133.1])
        bench = [EquityPoint(_ts(1), 10.0), EquityPoint(_ts(2), 11.0), EquityPoint(_ts(4), 13.31)]
        own_rets, bench_rets = aligned_returns(own, bench)
        # Three shared timestamps -> two return periods; the second spans days 2->4
        # on *both* sides, so the same calendar span is measured either way.
        assert len(own_rets) == len(bench_rets) == 2
        assert own_rets[1] == pytest.approx(133.1 / 110.0 - 1.0)
        assert bench_rets[1] == pytest.approx(13.31 / 11.0 - 1.0)

    def test_disjoint_curves_share_nothing(self) -> None:
        own = _curve([100.0, 101.0, 102.0])
        bench = _curve_from(10, [50.0, 51.0, 52.0])
        comparison = compare_to_benchmark(own, bench)
        assert comparison.shared_bars == 0
        assert comparison.beta is None
        assert comparison.alpha is None
        assert comparison.correlation is None
        assert comparison.information_ratio is None

    def test_a_single_shared_bar_yields_no_statistics(self) -> None:
        own = _curve([100.0, 101.0, 102.0])
        bench = _curve_from(3, [50.0, 51.0])
        comparison = compare_to_benchmark(own, bench)
        assert comparison.shared_bars == 1
        assert (comparison.beta, comparison.correlation) == (None, None)
        assert (comparison.alpha, comparison.information_ratio) == (None, None)


class TestBenchmarkStatistics:
    """Beta / alpha / correlation / information ratio against known values."""

    own: ClassVar[list[EquityPoint]] = _curve([100.0, 104.0, 101.0, 107.0, 106.0, 112.0])
    bench: ClassVar[list[EquityPoint]] = _curve([50.0, 51.0, 50.0, 52.0, 51.5, 53.0])

    def test_beta_matches_an_independent_covariance_reference(self) -> None:
        own_rets, bench_rets = aligned_returns(self.own, self.bench)
        expected = statistics.covariance(own_rets, bench_rets) / statistics.variance(bench_rets)
        assert beta(self.own, self.bench) == pytest.approx(expected)

    def test_correlation_matches_an_independent_reference(self) -> None:
        own_rets, bench_rets = aligned_returns(self.own, self.bench)
        expected = statistics.correlation(own_rets, bench_rets)
        assert correlation(self.own, self.bench) == pytest.approx(expected)

    def test_alpha_is_the_annualized_unexplained_mean_excess(self) -> None:
        own_rets, bench_rets = aligned_returns(self.own, self.bench)
        slope = beta(self.own, self.bench)
        assert slope is not None
        expected = (statistics.fmean(own_rets) - slope * statistics.fmean(bench_rets)) * 252.0
        assert alpha(self.own, self.bench) == pytest.approx(expected)

    def test_alpha_scales_linearly_with_periods_per_year(self) -> None:
        daily = alpha(self.own, self.bench, 252.0)
        hourly = alpha(self.own, self.bench, 1638.0)
        assert daily is not None and hourly is not None
        assert hourly == pytest.approx(daily * (1638.0 / 252.0))

    def test_information_ratio_is_the_sharpe_of_the_active_return(self) -> None:
        own_rets, bench_rets = aligned_returns(self.own, self.bench)
        active = [s - b for s, b in zip(own_rets, bench_rets, strict=True)]
        expected = statistics.fmean(active) / statistics.stdev(active) * sqrt(252.0)
        assert information_ratio(self.own, self.bench) == pytest.approx(expected)

    def test_identical_curves_give_unit_beta_and_correlation(self) -> None:
        curve = _curve([100.0, 104.0, 101.0, 107.0])
        assert beta(curve, curve) == pytest.approx(1.0)
        assert correlation(curve, curve) == pytest.approx(1.0)
        assert alpha(curve, curve) == pytest.approx(0.0)
        # No active return at all: the ratio is undefined, not zero.
        assert information_ratio(curve, curve) is None

    def test_doubled_returns_give_a_beta_of_two(self) -> None:
        bench = _curve([100.0, 110.0, 99.0, 108.9])  # +10%, -10%, +10%
        own = _curve([100.0, 120.0, 96.0, 115.2])  # +20%, -20%, +20%
        assert beta(own, bench) == pytest.approx(2.0)
        assert correlation(own, bench) == pytest.approx(1.0)

    def test_flat_benchmark_leaves_the_slope_undefined_but_not_the_info_ratio(self) -> None:
        # A zero-variance benchmark has no slope to regress against — beta and
        # correlation are undefined (None), yet the active return still exists.
        bench = _curve([50.0, 50.0, 50.0, 50.0])
        comparison = compare_to_benchmark(self.own[:4], bench)
        assert comparison.beta is None
        assert comparison.correlation is None
        assert comparison.alpha is None
        assert comparison.information_ratio is not None


class TestComputeSeparation:
    """``compute`` describes one run; the comparison is a separate value object."""

    @staticmethod
    def _result(curve: list[EquityPoint]) -> BacktestResult:
        return _bench_result(curve)

    def test_performance_metrics_carries_no_benchmark_field(self) -> None:
        # A benchmark comparison is a relation between two runs, so it is not a
        # property of this run's metrics (ADR-0037).
        assert not hasattr(compute(self._result(_curve([100.0, 101.0]))), "benchmark")

    def test_zero_beta_is_a_measurement_absence_is_a_missing_object(self) -> None:
        # Strategy and benchmark returns are exactly uncorrelated, so beta is 0.0 —
        # a real measurement. "No benchmark" is the *absence of a
        # BenchmarkComparison*, which no float can be mistaken for.
        bench = _curve([100.0, 110.0, 99.0, 108.9, 98.01])  # +10, -10, +10, -10
        own = _curve([100.0, 110.0, 121.0, 108.9, 98.01])  # +10, +10, -10, -10
        comparison = compare_to_benchmark(own, bench)
        assert comparison.beta == pytest.approx(0.0, abs=1e-12)
        assert comparison.shared_bars == 5

    def test_return_per_unit_exposure_needs_no_benchmark(self) -> None:
        curve = _curve_with_exposure([100.0, 110.0], [0.5, 0.5])
        metrics = compute(self._result(curve))
        assert metrics.return_per_unit_exposure == pytest.approx(annualized_return(curve) / 0.5)

    def test_return_per_unit_exposure_is_none_when_never_invested(self) -> None:
        metrics = compute(self._result(_curve([100.0, 110.0])))  # exposure defaults to 0.0
        assert metrics.return_per_unit_exposure is None
