"""Fast, no-infra unit tests for the ADV liquidity screen (ADR-0029).

Bars are hand-built with known closes and volumes so every expected ADV is a
transcribed hand computation. The load-bearing test here is the look-ahead guard:
the screen must never read a bar inside the backtest range.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.data.fake import FakeAdapter
from trading.liquidity import (
    DEFAULT_FORMATION_DAYS,
    average_dollar_volume,
    formation_window,
    screen_by_adv,
)
from trading.types import Bar

BACKTEST_START = datetime(2024, 6, 1, tzinfo=UTC)


def _bar(symbol: str, ts: datetime, close: float, volume: int) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
    )


def _series(symbol: str, close: float, volume: int, *, days: int, ending: datetime) -> list[Bar]:
    """``days`` daily bars of constant close/volume, the last one at ``ending``."""
    return [
        _bar(symbol, ending - timedelta(days=offset), close, volume)
        for offset in reversed(range(days))
    ]


class TestAverageDollarVolume:
    def test_mean_of_close_times_volume(self) -> None:
        bars = [
            _bar("AAA", datetime(2024, 1, 1, tzinfo=UTC), 10.0, 100),
            _bar("AAA", datetime(2024, 1, 2, tzinfo=UTC), 20.0, 300),
        ]
        # (10*100 + 20*300) / 2 = (1_000 + 6_000) / 2 = 3_500
        assert average_dollar_volume(bars) == pytest.approx(3_500.0)

    def test_empty_series_is_zero(self) -> None:
        assert average_dollar_volume([]) == 0.0

    def test_dollar_volume_not_share_volume(self) -> None:
        """A million shares of a $3 stock is not a million shares of a $300 stock."""
        cheap = [_bar("CHEAP", datetime(2024, 1, 1, tzinfo=UTC), 3.0, 1_000_000)]
        pricey = [_bar("RICH", datetime(2024, 1, 1, tzinfo=UTC), 300.0, 1_000_000)]
        assert average_dollar_volume(cheap) == pytest.approx(3_000_000.0)
        assert average_dollar_volume(pricey) == pytest.approx(300_000_000.0)


class TestFormationWindow:
    def test_ends_strictly_before_the_backtest_start(self) -> None:
        start, end = formation_window(BACKTEST_START)
        assert end < BACKTEST_START
        # The whole calendar day of BACKTEST_START is excluded, not just an instant.
        assert end <= BACKTEST_START - timedelta(days=1)
        assert start < end

    def test_spans_the_requested_number_of_days(self) -> None:
        start, end = formation_window(BACKTEST_START, 30)
        assert (end - start).days == 30

    def test_default_is_about_a_quarter(self) -> None:
        start, end = formation_window(BACKTEST_START)
        assert (end - start).days == DEFAULT_FORMATION_DAYS

    @pytest.mark.parametrize("days", [0, -1])
    def test_non_positive_window_rejected(self, days: int) -> None:
        with pytest.raises(ValueError, match="formation_days must be positive"):
            formation_window(BACKTEST_START, days)


class TestScreenByAdv:
    def test_keeps_liquid_drops_thin_with_reasons(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(
            # $50M/day: comfortably over a $20M floor.
            _series("LIQUID", 100.0, 500_000, days=10, ending=window_end)
            # $1M/day: far under it.
            + _series("THIN", 10.0, 100_000, days=10, ending=window_end)
        )
        screen = screen_by_adv(adapter, ["LIQUID", "THIN"], BACKTEST_START, min_adv=20_000_000.0)

        assert screen.kept == ["LIQUID"]
        assert [v.symbol for v in screen.dropped] == ["THIN"]
        thin = screen.dropped[0]
        assert thin.adv == pytest.approx(1_000_000.0)
        assert "< floor" in thin.reason
        assert not thin.unverified

    def test_order_of_kept_symbols_follows_submission(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(
            _series("BBB", 100.0, 500_000, days=5, ending=window_end)
            + _series("AAA", 100.0, 500_000, days=5, ending=window_end)
        )
        screen = screen_by_adv(adapter, ["BBB", "AAA"], BACKTEST_START, min_adv=1.0)
        assert screen.kept == ["BBB", "AAA"]

    def test_bars_inside_the_backtest_range_are_never_read(self) -> None:
        """The load-bearing look-ahead guard (ADR-0001 applied to selection).

        ``FUTURE`` is thin before the start line and enormously liquid after it. A
        screen that peeked at the backtest range would keep it; an honest one drops
        it on what was knowable beforehand.
        """
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(
            _series("FUTURE", 10.0, 10_000, days=10, ending=window_end)
            + _series(
                "FUTURE",
                1_000.0,
                10_000_000,
                days=10,
                ending=BACKTEST_START + timedelta(days=10),
            )
        )
        screen = screen_by_adv(adapter, ["FUTURE"], BACKTEST_START, min_adv=20_000_000.0)

        assert screen.kept == []
        assert screen.dropped[0].adv == pytest.approx(100_000.0)  # pre-start figure only
        assert screen.formation_end < BACKTEST_START

    def test_requested_range_never_reaches_the_backtest(self) -> None:
        """Prove it at the seam: record every range the screen asks the adapter for."""
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
                return _series(symbol, 100.0, 500_000, days=5, ending=end)

        screen_by_adv(RecordingAdapter(), ["AAA", "BBB"], BACKTEST_START)

        assert len(asked) == 2
        for _start, end in asked:
            assert end < BACKTEST_START

    def test_symbol_with_no_bars_is_unverified_and_dropped(self) -> None:
        adapter = FakeAdapter([])
        screen = screen_by_adv(adapter, ["GHOST"], BACKTEST_START)

        assert screen.kept == []
        verdict = screen.dropped[0]
        assert verdict.unverified
        assert verdict.adv is None
        assert "unverified" in verdict.reason
        assert [v.symbol for v in screen.unverified] == ["GHOST"]

    def test_unverified_is_distinct_from_a_liquidity_failure(self) -> None:
        """A data gap must never be reported as "too thin to trade"."""
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(_series("THIN", 1.0, 1_000, days=5, ending=window_end))
        screen = screen_by_adv(adapter, ["THIN", "GHOST"], BACKTEST_START)

        by_symbol = {v.symbol: v for v in screen.dropped}
        assert not by_symbol["THIN"].unverified
        assert by_symbol["GHOST"].unverified
        assert by_symbol["THIN"].reason != by_symbol["GHOST"].reason

    def test_one_failing_symbol_does_not_abort_the_screen(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        good = _series("GOOD", 100.0, 500_000, days=5, ending=window_end)

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

        screen = screen_by_adv(ExplodingAdapter(), ["BOOM", "GOOD"], BACKTEST_START, min_adv=1.0)

        assert screen.kept == ["GOOD"]
        boom = screen.dropped[0]
        assert boom.unverified
        assert "RuntimeError" in boom.reason

    def test_symbol_exactly_at_the_floor_passes(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(_series("EDGE", 10.0, 100_000, days=5, ending=window_end))
        screen = screen_by_adv(adapter, ["EDGE"], BACKTEST_START, min_adv=1_000_000.0)
        assert screen.kept == ["EDGE"]

    def test_negative_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_adv must be non-negative"):
            screen_by_adv(FakeAdapter([]), ["AAA"], BACKTEST_START, min_adv=-1.0)

    def test_describe_names_every_dropped_symbol_and_the_window(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(
            _series("LIQUID", 100.0, 500_000, days=5, ending=window_end)
            + _series("THIN", 1.0, 1_000, days=5, ending=window_end)
        )
        screen = screen_by_adv(
            adapter, ["LIQUID", "THIN", "GHOST"], BACKTEST_START, min_adv=20_000_000.0
        )
        text = screen.describe()

        assert "LIQUID" in text
        assert "dropped THIN" in text
        assert "dropped GHOST" in text
        assert "no look-ahead" in text
        assert "kept 1/3" in text

    def test_screen_is_reproducible(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        bars = _series("AAA", 100.0, 500_000, days=5, ending=window_end)
        first = screen_by_adv(FakeAdapter(bars), ["AAA"], BACKTEST_START, min_adv=1.0)
        second = screen_by_adv(FakeAdapter(bars), ["AAA"], BACKTEST_START, min_adv=1.0)
        assert first == second
