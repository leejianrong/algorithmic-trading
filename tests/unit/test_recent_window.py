"""Fast, no-infra unit tests for the V5 recent-window feed (dev-playbook layer 1).

Acceptance (SLICES V5 unit): a forming/partial latest bar is excluded until the
clock marks its session complete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading.clock import FakeClock
from trading.data.fake import FakeAdapter
from trading.data.recent_window import (
    RecentWindowFeed,
    default_is_complete,
    interval_is_complete,
)
from trading.types import Bar


def _ts(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=UTC)


def _bar(symbol: str, day: int, price: float = 100.0) -> Bar:
    return Bar(symbol, _ts(day), price, price, price, price, volume=1_000)


def _timestamps(feed: list[tuple[datetime, dict[str, Bar]]]) -> list[datetime]:
    return [ts for ts, _ in feed]


class TestForminBarExcluded:
    def test_excludes_todays_forming_bar(self) -> None:
        # Bars through D=5; clock sits mid-session on D=5 (still forming).
        adapter = FakeAdapter([_bar("AAA", d) for d in range(1, 6)])
        clock = FakeClock(_ts(5, hour=15))
        feed = RecentWindowFeed(adapter, clock)

        present = _timestamps(feed.poll(["AAA"], lookback=10))

        assert _ts(5) not in present
        assert present == [_ts(1), _ts(2), _ts(3), _ts(4)]

    def test_bar_included_after_clock_crosses_into_next_day(self) -> None:
        adapter = FakeAdapter([_bar("AAA", d) for d in range(1, 6)])
        clock = FakeClock(_ts(5, hour=15))
        feed = RecentWindowFeed(adapter, clock)

        assert _ts(5) not in _timestamps(feed.poll(["AAA"], lookback=10))

        clock.advance(_ts(6))
        present = _timestamps(feed.poll(["AAA"], lookback=10))
        assert _ts(5) in present
        assert present == [_ts(1), _ts(2), _ts(3), _ts(4), _ts(5)]


class TestPollShape:
    def test_returns_ascending_and_honours_lookback(self) -> None:
        adapter = FakeAdapter([_bar("AAA", d) for d in range(1, 6)])
        clock = FakeClock(_ts(10))  # all five bars complete
        feed = RecentWindowFeed(adapter, clock)

        present = _timestamps(feed.poll(["AAA"], lookback=2))

        assert present == [_ts(4), _ts(5)]  # newest two, ascending

    def test_merges_multiple_symbols_into_one_cross_section(self) -> None:
        adapter = FakeAdapter(
            [_bar("AAA", d) for d in range(1, 4)] + [_bar("BBB", d) for d in range(1, 4)]
        )
        clock = FakeClock(_ts(10))
        feed = RecentWindowFeed(adapter, clock)

        result = feed.poll(["AAA", "BBB"], lookback=10)

        assert _timestamps(result) == [_ts(1), _ts(2), _ts(3)]
        for _, slice_ in result:
            assert set(slice_) == {"AAA", "BBB"}


class _SpyAdapter:
    """Records the ``adjusted`` value each ``get_bars`` call received."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.adjusted_calls: list[bool] = []

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        self.adjusted_calls.append(adjusted)
        return [b for b in self._bars if b.symbol == symbol and start <= b.ts <= end]


class TestRawByDefault:
    """ADR-0021: the paper feed requests RAW actual quotes unless told otherwise."""

    def test_defaults_to_requesting_raw(self) -> None:
        adapter = _SpyAdapter([_bar("AAA", d) for d in range(1, 4)])
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA"], lookback=10)

        assert adapter.adjusted_calls == [False]

    def test_honors_an_explicit_adjusted_request(self) -> None:
        adapter = _SpyAdapter([_bar("AAA", d) for d in range(1, 4)])
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)), adjusted=True)

        feed.poll(["AAA"], lookback=10)

        assert adapter.adjusted_calls == [True]


class TestDefaultPolicy:
    def test_complete_only_on_a_strictly_later_date(self) -> None:
        bar = _bar("AAA", 5)
        assert not default_is_complete(bar, _ts(5))  # same day, forming
        assert not default_is_complete(bar, _ts(5, hour=23))  # still same day
        assert default_is_complete(bar, _ts(6))  # next day, complete


def _intraday_bar(symbol: str, ts: datetime, price: float = 100.0) -> Bar:
    return Bar(symbol, ts, price, price, price, price, volume=1_000)


class TestIntervalPolicy:
    """ADR-0022: a bar with START ts covers [ts, ts+interval), complete at ts+interval."""

    def test_forming_bar_excluded_until_the_interval_elapses(self) -> None:
        interval = timedelta(hours=1)
        is_complete = interval_is_complete(interval)
        bar = _intraday_bar("AAA", _ts(2, hour=14, minute=30))  # covers 14:30-15:30

        assert not is_complete(bar, _ts(2, hour=14, minute=30))  # just opened
        assert not is_complete(bar, _ts(2, hour=15, minute=29))  # still forming
        assert is_complete(bar, _ts(2, hour=15, minute=30))  # complete exactly at close
        assert is_complete(bar, _ts(2, hour=16))  # and after

    def test_feed_reveals_the_bar_exactly_at_ts_plus_interval(self) -> None:
        interval = timedelta(minutes=30)
        # Two 30-minute bars on 2024-01-02: 14:00 and 14:30.
        bars = [
            _intraday_bar("AAA", _ts(2, hour=14, minute=0)),
            _intraday_bar("AAA", _ts(2, hour=14, minute=30)),
        ]
        adapter = FakeAdapter(bars)
        # Clock sits at 14:45: the 14:00 bar closed (14:30 ≤ 14:45), the 14:30 bar
        # is still forming (closes 15:00).
        clock = FakeClock(_ts(2, hour=14, minute=45))
        feed = RecentWindowFeed(adapter, clock, interval_is_complete(interval))

        present = _timestamps(feed.poll(["AAA"], lookback=10))
        assert present == [_ts(2, hour=14, minute=0)]

        # Advance to 15:00 — the second bar's close — and it becomes visible.
        clock.advance(_ts(2, hour=15, minute=0))
        present = _timestamps(feed.poll(["AAA"], lookback=10))
        assert present == [_ts(2, hour=14, minute=0), _ts(2, hour=14, minute=30)]
