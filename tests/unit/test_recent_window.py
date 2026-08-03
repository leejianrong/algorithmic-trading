"""Fast, no-infra unit tests for the V5 recent-window feed (dev-playbook layer 1).

Acceptance (SLICES V5 unit): a forming/partial latest bar is excluded until the
clock marks its session complete.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.clock import FakeClock
from trading.data.fake import FakeAdapter
from trading.data.recent_window import RecentWindowFeed, default_is_complete
from trading.types import Bar


def _ts(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=UTC)


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


class TestDefaultPolicy:
    def test_complete_only_on_a_strictly_later_date(self) -> None:
        bar = _bar("AAA", 5)
        assert not default_is_complete(bar, _ts(5))  # same day, forming
        assert not default_is_complete(bar, _ts(5, hour=23))  # still same day
        assert default_is_complete(bar, _ts(6))  # next day, complete
