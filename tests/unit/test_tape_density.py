"""Fast, offline tests for the tape-density screen (KAN-863, ADR-0073).

Mirrors ``tests/unit/test_liquidity.py``'s shape exactly: bars are hand-built so
every expected coverage ratio is a transcribed hand computation, and the
load-bearing test is the same look-ahead guard the ADV screen has.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.calendar import CRYPTO_24_7, US_EQUITY
from trading.data.fake import FakeAdapter
from trading.frequency import Frequency
from trading.liquidity import formation_window
from trading.tape_density import (
    DEFAULT_MIN_TAPE_DENSITY,
    DEFAULT_TAPE_DENSITY_FORMATION_DAYS,
    bar_coverage_ratio,
    expected_bar_count,
    screen_by_tape_density,
)
from trading.types import Bar

BACKTEST_START = datetime(2024, 6, 1, tzinfo=UTC)
FIVE_MIN = Frequency.parse("5m", calendar=CRYPTO_24_7)


def _bar(symbol: str, ts: datetime) -> Bar:
    return Bar(symbol=symbol, ts=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=1)


def _dense_series(symbol: str, window_start: datetime, count: int, freq: Frequency) -> list[Bar]:
    """``count`` consecutive bars starting at ``window_start``, spaced by ``freq``."""
    return [_bar(symbol, window_start + i * freq.delta) for i in range(count)]


class TestExpectedBarCount:
    def test_one_day_window_at_5m_on_a_continuous_calendar(self) -> None:
        start = datetime(2024, 5, 1, tzinfo=UTC)
        end = start + timedelta(days=1)
        # Matches the real measurement in ADR-0073: 288 5-minute bars in a day.
        assert expected_bar_count(start, end, FIVE_MIN) == pytest.approx(288.0)

    def test_scales_with_window_span(self) -> None:
        start = datetime(2024, 5, 1, tzinfo=UTC)
        one_day = expected_bar_count(start, start + timedelta(days=1), FIVE_MIN)
        one_week = expected_bar_count(start, start + timedelta(days=7), FIVE_MIN)
        assert one_week == pytest.approx(one_day * 7)

    def test_rejects_a_non_continuous_calendar(self) -> None:
        equity_5m = Frequency.parse("5m", calendar=US_EQUITY)
        start = datetime(2024, 5, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="continuous market calendar"):
            expected_bar_count(start, start + timedelta(days=1), equity_5m)

    def test_rejects_a_non_positive_window(self) -> None:
        start = datetime(2024, 5, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="window_end must be after window_start"):
            expected_bar_count(start, start, FIVE_MIN)
        with pytest.raises(ValueError, match="window_end must be after window_start"):
            expected_bar_count(start, start - timedelta(days=1), FIVE_MIN)


class TestBarCoverageRatio:
    def test_full_coverage_is_one(self) -> None:
        start = datetime(2024, 5, 1, tzinfo=UTC)
        end = start + timedelta(days=1)
        bars = _dense_series("BTC/USD", start, 288, FIVE_MIN)
        assert bar_coverage_ratio(bars, start, end, FIVE_MIN) == pytest.approx(1.0)

    def test_half_the_bars_is_half_coverage(self) -> None:
        start = datetime(2024, 5, 1, tzinfo=UTC)
        end = start + timedelta(days=1)
        bars = _dense_series("ETH/USD", start, 144, FIVE_MIN)
        assert bar_coverage_ratio(bars, start, end, FIVE_MIN) == pytest.approx(0.5)

    def test_no_bars_is_zero(self) -> None:
        start = datetime(2024, 5, 1, tzinfo=UTC)
        end = start + timedelta(days=1)
        assert bar_coverage_ratio([], start, end, FIVE_MIN) == 0.0


class TestScreenByTapeDensity:
    def test_keeps_dense_drops_thin_with_reasons(self) -> None:
        start, end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        expected = expected_bar_count(start, end, FIVE_MIN)
        adapter = FakeAdapter(
            _dense_series("LINK/USD", start, round(expected), FIVE_MIN)
            + _dense_series("ETH/USD", start, round(expected * 0.4), FIVE_MIN)
        )
        screen = screen_by_tape_density(
            adapter, ["LINK/USD", "ETH/USD"], BACKTEST_START, FIVE_MIN, min_density=0.80
        )

        assert screen.kept == ["LINK/USD"]
        assert [v.symbol for v in screen.dropped] == ["ETH/USD"]
        eth = screen.dropped[0]
        assert eth.coverage == pytest.approx(0.4, abs=0.01)
        assert "< floor" in eth.reason
        assert not eth.unverified

    def test_order_of_kept_symbols_follows_submission(self) -> None:
        start, end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        expected = round(expected_bar_count(start, end, FIVE_MIN))
        adapter = FakeAdapter(
            _dense_series("BBB", start, expected, FIVE_MIN)
            + _dense_series("AAA", start, expected, FIVE_MIN)
        )
        screen = screen_by_tape_density(
            adapter, ["BBB", "AAA"], BACKTEST_START, FIVE_MIN, min_density=0.01
        )
        assert screen.kept == ["BBB", "AAA"]

    def test_bars_inside_the_backtest_range_are_never_read(self) -> None:
        """The load-bearing look-ahead guard (ADR-0001 applied to universe selection)."""
        start, _end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        adapter = FakeAdapter(
            _dense_series("FUTURE", start, 1, FIVE_MIN)  # thin before the start line
            + _dense_series(  # enormously dense after it
                "FUTURE",
                BACKTEST_START + timedelta(days=10),
                10_000,
                FIVE_MIN,
            )
        )
        screen = screen_by_tape_density(
            adapter, ["FUTURE"], BACKTEST_START, FIVE_MIN, min_density=0.80
        )

        assert screen.kept == []
        assert screen.formation_end < BACKTEST_START
        coverage = screen.dropped[0].coverage
        assert coverage is not None and coverage < 0.01  # pre-start figure only

    def test_requested_range_never_reaches_the_backtest(self) -> None:
        asked: list[tuple[datetime, datetime]] = []

        class RecordingAdapter:
            def get_bars(
                self,
                symbol: str,
                start: datetime,
                end: datetime,
                *,
                adjusted: bool = True,
            ) -> list[Bar]:
                asked.append((start, end))
                return _dense_series(symbol, start, 10, FIVE_MIN)

        screen_by_tape_density(RecordingAdapter(), ["AAA", "BBB"], BACKTEST_START, FIVE_MIN)

        assert len(asked) == 2
        for _start, end in asked:
            assert end < BACKTEST_START

    def test_symbol_with_no_bars_is_unverified_and_dropped(self) -> None:
        adapter = FakeAdapter([])
        screen = screen_by_tape_density(adapter, ["GHOST"], BACKTEST_START, FIVE_MIN)

        assert screen.kept == []
        verdict = screen.dropped[0]
        assert verdict.unverified
        assert verdict.coverage is None
        assert "unverified" in verdict.reason
        assert [v.symbol for v in screen.unverified] == ["GHOST"]

    def test_unverified_is_distinct_from_a_density_failure(self) -> None:
        start, _end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        adapter = FakeAdapter(_dense_series("THIN", start, 1, FIVE_MIN))
        screen = screen_by_tape_density(adapter, ["THIN", "GHOST"], BACKTEST_START, FIVE_MIN)

        by_symbol = {v.symbol: v for v in screen.dropped}
        assert not by_symbol["THIN"].unverified
        assert by_symbol["GHOST"].unverified
        assert by_symbol["THIN"].reason != by_symbol["GHOST"].reason

    def test_one_failing_symbol_does_not_abort_the_screen(self) -> None:
        start, end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        expected = round(expected_bar_count(start, end, FIVE_MIN))
        good = _dense_series("GOOD", start, expected, FIVE_MIN)

        class ExplodingAdapter:
            def get_bars(
                self,
                symbol: str,
                start: datetime,
                end: datetime,
                *,
                adjusted: bool = True,
            ) -> list[Bar]:
                if symbol == "BOOM":
                    raise RuntimeError("upstream 500")
                return [b for b in good if b.symbol == symbol]

        screen = screen_by_tape_density(
            ExplodingAdapter(), ["BOOM", "GOOD"], BACKTEST_START, FIVE_MIN, min_density=0.5
        )

        assert screen.kept == ["GOOD"]
        boom = screen.dropped[0]
        assert boom.unverified
        assert "RuntimeError" in boom.reason

    def test_symbol_exactly_at_the_floor_passes(self) -> None:
        start, end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        expected = expected_bar_count(start, end, FIVE_MIN)
        count = round(expected * 0.5)
        adapter = FakeAdapter(_dense_series("EDGE", start, count, FIVE_MIN))
        floor = count / expected
        screen = screen_by_tape_density(
            adapter, ["EDGE"], BACKTEST_START, FIVE_MIN, min_density=floor
        )
        assert screen.kept == ["EDGE"]

    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_density_floor_outside_zero_one_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="min_density must be between 0 and 1"):
            screen_by_tape_density(
                FakeAdapter([]), ["AAA"], BACKTEST_START, FIVE_MIN, min_density=bad
            )

    def test_describe_names_every_dropped_symbol_and_the_window(self) -> None:
        start, end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        adapter = FakeAdapter(_dense_series("THIN", start, 1, FIVE_MIN))
        screen = screen_by_tape_density(
            adapter, ["THIN"], BACKTEST_START, FIVE_MIN, min_density=0.8
        )
        text = screen.describe()
        assert "5m" in text
        assert "THIN" in text
        assert str(start.date()) in text
        assert str(end.date()) in text

    def test_default_floor_and_formation_days_are_the_module_constants(self) -> None:
        start, end = formation_window(BACKTEST_START, DEFAULT_TAPE_DENSITY_FORMATION_DAYS)
        adapter = FakeAdapter(_dense_series("BTC/USD", start, 1, FIVE_MIN))
        screen = screen_by_tape_density(adapter, ["BTC/USD"], BACKTEST_START, FIVE_MIN)
        assert screen.min_density == DEFAULT_MIN_TAPE_DENSITY
        assert screen.formation_start == start
        assert screen.formation_end == end
