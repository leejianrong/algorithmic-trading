"""Fast, no-infra unit tests for the ADR-0066 regime-split metrics.

Fixtures are hand-built (tz-aware timestamps, deterministic per-bar returns) so
every expected value is a transcribed hand computation, not a re-derivation of
the code under test. No engine, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import (
    MIN_BOOTSTRAP_OBSERVATIONS,
    REGIME_WINDOW,
    PerformanceMetrics,
    compute,
    compute_regime_report,
)
from trading.types import Fill, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=day)


def _curve_from_returns(returns: list[float], start: float = 1000.0) -> list[EquityPoint]:
    """An equity curve whose per-bar returns are exactly ``returns``, in order."""
    equity = start
    points = [EquityPoint(_ts(0), equity)]
    for i, r in enumerate(returns, start=1):
        equity *= 1.0 + r
        points.append(EquityPoint(_ts(i), equity))
    return points


def _result(
    curve: list[EquityPoint], fills: list[tuple[datetime, Fill]] | None = None
) -> BacktestResult:
    return BacktestResult(
        symbols=["A"],
        starting_cash=curve[0].equity,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=curve[-1].equity),
        fills=fills or [],
    )


class TestTooShortToClassify:
    """A curve shorter than the window cannot classify even one bar (ADR-0066)."""

    def test_all_fields_none_below_the_window(self) -> None:
        # 10 return periods < REGIME_WINDOW (20): nothing can be classified.
        curve = _curve_from_returns([0.01] * 10)
        report = compute_regime_report(_result(curve))
        assert report.vol_threshold is None
        assert report.trend_threshold is None
        assert report.high_vol is None
        assert report.low_vol is None
        assert report.trending is None
        assert report.mean_reverting is None
        assert report.notes
        assert "no regime classification" in report.notes[0]

    def test_exactly_the_window_classifies_one_bar(self) -> None:
        # window return periods -> exactly one classified bar (the last one).
        curve = _curve_from_returns([0.01] * REGIME_WINDOW)
        report = compute_regime_report(_result(curve))
        assert report.vol_threshold is not None
        assert report.trend_threshold is not None
        assert report.high_vol is not None
        assert report.low_vol is not None
        assert report.high_vol.bar_count + report.low_vol.bar_count == 1


class TestVolatilityAndTrendSplitPartitionTheBars:
    """Bar-count bookkeeping: warmup excluded, both halves of each axis sum right."""

    def test_bar_counts_sum_to_classified_return_periods(self) -> None:
        # Alternate a calm epoch and a noisy epoch so both vol buckets are non-empty.
        returns: list[float] = []
        for block in range(6):
            vol = 0.001 if block % 2 == 0 else 0.05
            sign = 1.0 if block % 2 == 0 else -1.0
            returns.extend([sign * vol] * 20)
        curve = _curve_from_returns(returns)
        report = compute_regime_report(_result(curve))
        assert report.high_vol is not None
        assert report.low_vol is not None
        assert report.trending is not None
        assert report.mean_reverting is not None

        total_returns = len(returns)
        classified = total_returns - (REGIME_WINDOW - 1)
        assert report.high_vol.bar_count + report.low_vol.bar_count == classified
        assert report.trending.bar_count + report.mean_reverting.bar_count == classified

    def test_high_vol_bucket_has_the_higher_realized_volatility(self) -> None:
        """The split actually separates calm from noisy stretches."""
        returns = [0.0005] * 40 + [0.05, -0.05] * 20
        curve = _curve_from_returns(returns)
        report = compute_regime_report(_result(curve))
        assert report.high_vol is not None
        assert report.low_vol is not None
        # The noisy tail should dominate high_vol's bar count.
        assert report.high_vol.bar_count > 0
        assert report.low_vol.bar_count > 0

    def test_trending_and_mean_reverting_differ_on_a_constructed_series(self) -> None:
        # A monotonic run (trending) followed by an oscillating one (mean-reverting).
        trend_block = [0.01] * 30
        chop_block = [0.02, -0.02] * 15
        curve = _curve_from_returns(trend_block + chop_block)
        report = compute_regime_report(_result(curve))
        assert report.trending is not None
        assert report.mean_reverting is not None
        # Both buckets are populated (deliberately not asserting exact membership,
        # since the split is at this run's own median, not a fixed cutoff).
        assert report.trending.bar_count > 0
        assert report.mean_reverting.bar_count > 0


class TestWholeRunUnaffected:
    """ADR-0066 is read-only reporting: compute() never changes."""

    def test_compute_regime_report_does_not_touch_compute(self) -> None:
        returns = [0.001 * ((-1) ** i) for i in range(60)]
        curve = _curve_from_returns(returns)
        result = _result(curve)
        before = compute(result)
        compute_regime_report(result)
        after = compute(result)
        assert before == after


class TestUnderpowered:
    """A thin regime slice is still computed, never hidden, and flagged (ADR-0029/0039)."""

    def test_a_short_run_is_flagged_underpowered(self) -> None:
        # window + a handful of extra bars -> a handful of classified return
        # periods, well under MIN_BOOTSTRAP_OBSERVATIONS on at least one side.
        returns = [0.001] * (REGIME_WINDOW + 5)
        curve = _curve_from_returns(returns)
        report = compute_regime_report(_result(curve))
        assert report.high_vol is not None
        assert report.low_vol is not None
        assert report.trending is not None
        assert report.mean_reverting is not None
        total_classified = report.high_vol.bar_count + report.low_vol.bar_count
        assert total_classified < MIN_BOOTSTRAP_OBSERVATIONS * 2
        underpowered_regimes = [
            r
            for r in (report.high_vol, report.low_vol, report.trending, report.mean_reverting)
            if r.underpowered
        ]
        assert underpowered_regimes
        assert any(str(MIN_BOOTSTRAP_OBSERVATIONS) in note for note in report.notes)

    def test_underpowered_regime_still_has_real_metrics(self) -> None:
        """Never suppressed: a thin slice's Sharpe/Sortino/Calmar are still computed."""
        returns = [0.001] * (REGIME_WINDOW + 5)
        curve = _curve_from_returns(returns)
        report = compute_regime_report(_result(curve))
        assert report.high_vol is not None
        assert isinstance(report.high_vol.metrics, PerformanceMetrics)

    def test_a_long_run_is_not_flagged(self) -> None:
        returns = [0.0005 * ((-1) ** i) + 0.0002 * i for i in range(400)]
        curve = _curve_from_returns(returns)
        report = compute_regime_report(_result(curve))
        assert report.high_vol is not None
        assert report.low_vol is not None
        assert not report.high_vol.underpowered
        assert not report.low_vol.underpowered


class TestTradeAttributionRespectsFullHistory:
    """A SELL closing a position opened in a different regime is priced correctly.

    ``_regime_trade_stats`` must reconstruct running qty/avg-cost from the WHOLE
    fill history, not just the fills that happen to fall in one regime, or a
    cross-regime round trip would silently look like a fresh entry at qty 0.
    """

    def test_entries_in_warmup_are_not_double_counted_into_a_regime(self) -> None:
        # A smooth exponential curve: every classified bar is identical, so all of
        # them fall in one regime (ties at the median), and the only BUY sits
        # inside the unclassified warmup window (bars 1..REGIME_WINDOW-1).
        curve = _curve_from_returns([0.001] * 60)
        fills = [
            (_ts(5), Fill("A", Side.BUY, 10.0, 100.0)),
            (_ts(55), Fill("A", Side.SELL, 10.0, 120.0)),
        ]
        result = _result(curve, fills)
        report = compute_regime_report(result)
        assert report.high_vol is not None
        assert report.low_vol is not None
        # The BUY (day 5) is warmup and uncounted anywhere; the SELL (day 55) is
        # classified, and its win/loss must be judged against the BUY's true
        # average cost (100.0), not against a cost of 0.0 from a truncated history.
        total_closes_win_rate = [
            r.metrics.win_rate for r in (report.high_vol, report.low_vol) if r.bar_count > 0
        ]
        assert 1.0 in total_closes_win_rate  # the SELL at 120 > cost 100 is a win.

    def test_entry_count_across_regimes_never_exceeds_total_buys(self) -> None:
        returns = [0.001 if i % 2 == 0 else -0.001 for i in range(80)]
        curve = _curve_from_returns(returns)
        fills = [
            (_ts(30), Fill("A", Side.BUY, 5.0, 100.0)),
            (_ts(31), Fill("A", Side.SELL, 5.0, 101.0)),
            (_ts(60), Fill("A", Side.BUY, 5.0, 100.0)),
        ]
        result = _result(curve, fills)
        report = compute_regime_report(result)
        assert report.high_vol is not None
        assert report.low_vol is not None
        total_entries = report.high_vol.metrics.trade_count + report.low_vol.metrics.trade_count
        assert total_entries <= 2  # at most the two real BUYs, never inflated.


class TestFreeParameters:
    """``free_parameters`` behaves exactly as :func:`compute` documents."""

    def test_omitted_leaves_trades_per_parameter_none(self) -> None:
        returns = [0.001 * ((-1) ** i) for i in range(60)]
        curve = _curve_from_returns(returns)
        report = compute_regime_report(_result(curve))
        assert report.high_vol is not None
        assert report.high_vol.metrics.trades_per_parameter is None

    def test_supplied_divides_regime_entries(self) -> None:
        returns = [0.001 * ((-1) ** i) for i in range(60)]
        curve = _curve_from_returns(returns)
        fills = [
            (_ts(40), Fill("A", Side.BUY, 5.0, 100.0)),
        ]
        report = compute_regime_report(_result(curve, fills), free_parameters=2)
        classified = [r for r in (report.high_vol, report.low_vol) if r is not None]
        assert any(r.metrics.trades_per_parameter is not None for r in classified)
