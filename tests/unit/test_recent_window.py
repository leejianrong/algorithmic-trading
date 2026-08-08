"""Fast, no-infra unit tests for the V5 recent-window feed (dev-playbook layer 1).

Acceptance (SLICES V5 unit): a forming/partial latest bar is excluded until the
clock marks its session complete.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from trading.broker import SimulatedBroker
from trading.clock import FakeClock
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.recent_window import (
    PERSISTENT_ABSENCE_POLLS,
    RecentWindowFeed,
    default_is_complete,
    interval_is_complete,
)
from trading.engine import REASON_FETCH_FAILED, REASON_NO_BARS, Engine, PaperSession
from trading.risk import Guardrails
from trading.strategies.buy_and_hold import BuyAndHold
from trading.types import Bar, Portfolio


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


class _FlakyAdapter:
    """Serves ``FakeAdapter`` bars, but raises for symbols in ``failing``.

    ``failing`` is mutable so a test can script a symbol that fails on one poll and
    recovers on the next, deterministically and with no clock involved.
    """

    def __init__(
        self,
        bars: list[Bar],
        failing: set[str] | None = None,
        exc: type[BaseException] = TimeoutError,
    ) -> None:
        self._inner = FakeAdapter(bars)
        self.failing = failing if failing is not None else set()
        self._exc = exc
        self.calls: list[str] = []

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        self.calls.append(symbol)
        if symbol in self.failing:
            raise self._exc(f"transport blew up for {symbol}")
        return self._inner.get_bars(symbol, start, end, adjusted=adjusted)


class TestPerSymbolFetchGuard:
    """ADR-0035: one bad symbol must not abort a whole poll (mirrors ADR-0032)."""

    def test_one_failing_symbol_does_not_abort_the_poll(self) -> None:
        adapter = _FlakyAdapter(
            [_bar("AAA", d) for d in range(1, 4)] + [_bar("BAD", d) for d in range(1, 4)],
            failing={"BAD"},
        )
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        result = feed.poll(["AAA", "BAD"], lookback=10)

        # The healthy symbol still trades; the broken one is simply not in the slice.
        assert _timestamps(result) == [_ts(1), _ts(2), _ts(3)]
        for _, slice_ in result:
            assert set(slice_) == {"AAA"}

    def test_a_failed_lookup_is_reported_as_fetch_failed(self) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1)], failing={"BAD"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA", "BAD"], lookback=10)

        assert [a.symbol for a in feed.absent] == ["BAD"]
        absence = feed.absent[0]
        assert absence.reason == REASON_FETCH_FAILED
        # The detail names the failure so the operator can tell an outage from a typo.
        assert "TimeoutError" in absence.detail
        assert "BAD" in absence.detail

    def test_an_empty_source_response_is_reported_as_no_bars(self) -> None:
        # GONE is known to the request but the source has nothing for it: absence,
        # not failure — the two reason codes ADR-0032 insists on keeping apart.
        adapter = _FlakyAdapter([_bar("AAA", 1)])
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA", "GONE"], lookback=10)

        assert [(a.symbol, a.reason) for a in feed.absent] == [("GONE", REASON_NO_BARS)]

    def test_a_still_forming_bar_is_not_an_absence(self) -> None:
        # ADR-0022: bars exist, none are complete yet. That is the normal state at
        # every interval boundary, not a missing symbol.
        adapter = _FlakyAdapter([_bar("AAA", 5)])
        feed = RecentWindowFeed(adapter, FakeClock(_ts(5, hour=15)))

        assert feed.poll(["AAA"], lookback=10) == []
        assert feed.absent == []
        assert feed.absence_streaks == {}

    def test_a_recovered_symbol_is_retried_and_comes_back(self) -> None:
        adapter = _FlakyAdapter(
            [_bar("AAA", d) for d in range(1, 4)] + [_bar("BAD", d) for d in range(1, 4)],
            failing={"BAD"},
        )
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA", "BAD"], lookback=10)
        assert feed.absence_streaks == {"BAD": 1}

        adapter.failing.clear()  # the outage ends mid-session
        result = feed.poll(["AAA", "BAD"], lookback=10)

        assert feed.absent == []
        assert feed.absence_streaks == {}  # the streak resets, not accumulates
        for _, slice_ in result:
            assert set(slice_) == {"AAA", "BAD"}

    def test_a_persistent_failure_is_retried_forever_and_escalated(self) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1), _bar("BAD", 1)], failing={"BAD"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        for _ in range(PERSISTENT_ABSENCE_POLLS + 2):
            feed.poll(["AAA", "BAD"], lookback=10)

        # Never quarantined: the feed asked for BAD on every single poll.
        assert adapter.calls.count("BAD") == PERSISTENT_ABSENCE_POLLS + 2
        assert feed.absence_streaks == {"BAD": PERSISTENT_ABSENCE_POLLS + 2}
        assert feed.persistently_absent == ["BAD"]
        assert "consecutive polls" in feed.absent[0].detail

    def test_below_the_threshold_nothing_is_called_persistent(self) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1)], failing={"BAD"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA", "BAD"], lookback=10)

        assert feed.persistently_absent == []

    def test_every_symbol_failing_returns_an_empty_feed_not_an_exception(self) -> None:
        # Diverges from Engine.run's EmptyUniverseError on purpose: a live session
        # must survive a total outage, and the paper loop already handles an empty poll.
        adapter = _FlakyAdapter([_bar("AAA", 1)], failing={"AAA", "BBB"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        assert feed.poll(["AAA", "BBB"], lookback=10) == []
        assert [a.symbol for a in feed.absent] == ["AAA", "BBB"]

    def test_keyboard_interrupt_is_never_swallowed(self) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1)], failing={"BAD"}, exc=KeyboardInterrupt)
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        with pytest.raises(KeyboardInterrupt):
            feed.poll(["AAA", "BAD"], lookback=10)

    def test_the_absence_list_is_a_copy(self) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1)], failing={"BAD"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA", "BAD"], lookback=10)
        feed.absent.clear()
        feed.absence_streaks.clear()

        assert [a.symbol for a in feed.absent] == ["BAD"]
        assert feed.absence_streaks == {"BAD": 1}

    def test_a_duplicate_symbol_is_fetched_once(self) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1)])
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        feed.poll(["AAA", "AAA"], lookback=10)

        assert adapter.calls == ["AAA"]


class TestGuardLeavesTheHappyPathAlone:
    """The guard must not change *which* bars get processed (ADR-0022 invariant)."""

    def test_a_clean_poll_is_byte_identical_to_the_unguarded_result(self) -> None:
        bars = [_bar("AAA", d, price=100.0 + d) for d in range(1, 6)] + [
            _bar("BBB", d, price=50.0 + d) for d in range(1, 6)
        ]
        clock = FakeClock(_ts(5, hour=15))  # D=5 still forming
        feed = RecentWindowFeed(FakeAdapter(bars), clock)

        result = feed.poll(["AAA", "BBB"], lookback=3)

        # Exactly what the pre-guard implementation produced: newest three completed
        # bars per symbol, merged ascending, forming bar excluded.
        assert result == [
            (_ts(2), {"AAA": _bar("AAA", 2, 102.0), "BBB": _bar("BBB", 2, 52.0)}),
            (_ts(3), {"AAA": _bar("AAA", 3, 103.0), "BBB": _bar("BBB", 3, 53.0)}),
            (_ts(4), {"AAA": _bar("AAA", 4, 104.0), "BBB": _bar("BBB", 4, 54.0)}),
        ]
        assert feed.absent == []

    def test_a_paper_session_survives_one_bad_symbol_and_still_trades_the_rest(self) -> None:
        bars = [_bar("AAA", d, price=100.0 + d) for d in range(1, 6)]
        clock = FakeClock(_ts(10))
        adapter = _FlakyAdapter(bars, failing={"BAD"})
        feed = RecentWindowFeed(adapter, clock)
        broker = SimulatedBroker(
            Portfolio(cash=10_000.0),
            CostConfig(commission_per_share=0.0, slippage_bps=0.0),
        )
        engine = Engine(adapter, broker, Guardrails(RiskConfig()))
        session = PaperSession(
            engine, BuyAndHold(), ["AAA", "BAD"], feed, clock, lookback=10, warmup=False
        )

        result = session.run(max_empty_polls=1)

        # Every completed bar of the healthy symbol was processed — the session did
        # not die on BAD.
        assert [o.ts for o in session.session_log] == [_ts(d) for d in range(1, 6)]
        assert len(result.equity_curve) == 5
        assert [a.symbol for a in feed.absent] == ["BAD"]


class TestAbsenceIsLogged:
    """A dropped symbol is never silent, even on a poll that yields no new bar."""

    def test_first_failure_warns_and_persistence_escalates_without_spamming(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1)], failing={"BAD"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            for _ in range(PERSISTENT_ABSENCE_POLLS + 2):
                feed.poll(["AAA", "BAD"], lookback=10)

        levels = [r.levelno for r in caplog.records]
        # One warning when it first goes missing, one error when it turns persistent,
        # and nothing for the polls in between — the structured record carries those.
        assert levels == [logging.WARNING, logging.ERROR]
        assert all("BAD" in r.getMessage() for r in caplog.records)

    def test_recovery_is_logged_too(self, caplog: pytest.LogCaptureFixture) -> None:
        adapter = _FlakyAdapter([_bar("AAA", 1), _bar("BAD", 1)], failing={"BAD"})
        feed = RecentWindowFeed(adapter, FakeClock(_ts(10)))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            feed.poll(["AAA", "BAD"], lookback=10)
            caplog.clear()
            adapter.failing.clear()
            feed.poll(["AAA", "BAD"], lookback=10)

        assert [r.levelno for r in caplog.records] == [logging.INFO]
        assert "BAD" in caplog.records[0].getMessage()


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
