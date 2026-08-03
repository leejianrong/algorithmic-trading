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

from trading.engine import EquityPoint
from trading.metrics import (
    PerformanceMetrics,
    annualized_return,
    avg_exposure,
    calmar,
    compute,
    daily_returns,
    max_drawdown,
    peak_exposure,
    sharpe,
    sortino,
    total_return,
    turnover,
    win_rate,
)
from trading.types import Fill, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _curve(equities: list[float]) -> list[EquityPoint]:
    return [EquityPoint(_ts(i + 1), e) for i, e in enumerate(equities)]


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
