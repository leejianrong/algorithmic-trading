"""The paper feed asks for a window a provider will actually answer (ADR-0047).

The bug (KAN-714): ``RecentWindowFeed.poll`` asked its adapter for
``[datetime.min, now]`` — year 1 to now. **Alpaca answers that with an empty
response**, not an error, so every symbol read absent on every poll, ADR-0035
recorded them as ``REASON_NO_BARS``, and a live session stopped on
``max_empty_polls`` having primed nothing and traded nothing. Measured against the
real paper API on 2026-08-09 (AAPL, IEX, raw)::

    start=datetime.min   1d ->      0 bars     5m ->      0 bars
    start=1900-01-01     1d ->   1516 bars     5m -> 121662 bars
    start=now-5d         1d ->      4 bars     5m ->    348 bars

So the window was not too *wide* for the data plan; ``datetime.min`` specifically
is what the provider refuses.

**Why no offline test could catch it, and what that means for this file.**
``SyntheticAdapter`` *clips* a ``datetime.min`` start to its 1990 epoch —
ADR-0030 documents the clipping as deliberate — and ``FakeAdapter`` filters a
range without caring how absurd it is. Both are more forgiving than the provider,
so the fast layer exercised a start the adapter silently rewrote and the bug could
not appear (ADR-0040's lesson again). Every test here that stands in for the
provider therefore uses :class:`_AlpacaLikeAdapter`, which **reproduces the
refusal**, and asserts on the range the feed actually *requested* rather than on a
bar count alone (the spy pattern ADR-0029's look-ahead test uses).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from trading.clock import FakeClock
from trading.data.fake import FakeAdapter
from trading.data.recent_window import (
    EARLIEST_START,
    MIN_FETCH_SPAN,
    RecentWindowFeed,
    default_is_complete,
    fetch_span,
    interval_is_complete,
)
from trading.engine import REASON_NO_BARS
from trading.types import Bar

NOW = datetime(2026, 8, 9, 17, 0, tzinfo=UTC)
FAR_PAST = datetime.min.replace(tzinfo=UTC)

_FIVE_MIN = timedelta(minutes=5)
_ONE_DAY = timedelta(days=1)
# 09:30-16:00 ET on 2026-08-09 is 13:30-20:00 UTC.
_SESSION_OPEN = timedelta(hours=13, minutes=30)
_SESSION_BARS_5M = 78


class _AlpacaLikeAdapter:
    """A fake carrying the provider's real pathology, plus a request spy.

    Two behaviours, both measured against the live API rather than assumed:

    * a start before :attr:`ANSWERABLE_FROM` yields an **empty list** — no
      exception, no partial answer — which is exactly what Alpaca does with
      ``datetime.min``;
    * any bounded start inside its data serves bars normally.

    Every call is recorded on :attr:`requests` as ``(symbol, start, end)``, so a
    test can assert on the *request* and not merely on what came back. Setting
    :attr:`blackout` makes it answer everything with nothing, so a test can script
    an outage and its end without reaching into the feed.
    """

    ANSWERABLE_FROM = datetime(1900, 1, 1, tzinfo=UTC)

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.blackout = False
        self.requests: list[tuple[str, datetime, datetime]] = []

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        self.requests.append((symbol, start, end))
        if self.blackout or start < self.ANSWERABLE_FROM:
            return []
        return [b for b in self._bars if b.symbol == symbol and start <= b.ts <= end]


def _trading_days(count: int, *, ending: datetime) -> list[datetime]:
    """The ``count`` most recent weekdays at or before ``ending``, ascending.

    Weekends only — a real calendar also drops ~9 holidays a year, which
    :func:`_with_holidays` removes where a test wants the harsher case.
    """
    days: list[datetime] = []
    day = ending.replace(hour=0, minute=0, second=0, microsecond=0)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= _ONE_DAY
    return sorted(days)


def _with_holidays(days: list[datetime], every: int = 25) -> list[datetime]:
    """Drop one session in ``every`` — a deliberately harsher-than-real calendar.

    The NYSE closes ~9 days a year out of ~261 weekdays, i.e. one in 29. One in 25
    is stricter, so a window that survives this survives the real calendar.
    """
    return [d for i, d in enumerate(days) if i % every != 0]


def _daily_bars(symbol: str, days: list[datetime]) -> list[Bar]:
    return [Bar(symbol, d, 100.0, 100.0, 100.0, 100.0, volume=1_000) for d in days]


def _five_minute_bars(symbol: str, days: list[datetime]) -> list[Bar]:
    """A full 78-bar regular-hours session for each day, START-stamped (ADR-0022)."""
    return [
        Bar(symbol, d + _SESSION_OPEN + i * _FIVE_MIN, 100.0, 100.0, 100.0, 100.0, volume=1_000)
        for d in days
        for i in range(_SESSION_BARS_5M)
    ]


class TestTheProviderRefusalIsReproduced:
    """The fake earns its place: it must fail the way the real API failed."""

    def test_the_fake_answers_datetime_min_with_nothing(self) -> None:
        adapter = _AlpacaLikeAdapter(_daily_bars("AAA", _trading_days(30, ending=NOW)))

        assert adapter.get_bars("AAA", FAR_PAST, NOW) == []
        assert len(adapter.get_bars("AAA", NOW - timedelta(days=30), NOW)) > 0

    def test_the_synthetic_adapter_cannot_show_this_bug(self) -> None:
        # ADR-0030: SyntheticAdapter clips a datetime.min start to its 1990 epoch,
        # so it answers the impossible request happily. Pinned here so nobody
        # "simplifies" this file back onto the forgiving stand-in.
        from trading.data.synthetic import SyntheticAdapter

        adapter = SyntheticAdapter(seed=1)
        clipped = adapter.get_bars("AAA", FAR_PAST, datetime(1990, 2, 1, tzinfo=UTC))

        assert clipped, "SyntheticAdapter clips rather than refusing — it cannot reproduce KAN-714"


class TestPollAsksForABoundedWindow:
    """KAN-714: the request itself, not the bar count, is what was broken."""

    def test_a_daily_poll_never_asks_from_datetime_min(self) -> None:
        days = _trading_days(600, ending=NOW)
        adapter = _AlpacaLikeAdapter(_daily_bars("AAA", days))
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        feed.poll(["AAA"], lookback=512)

        symbol, start, end = adapter.requests[0]
        assert (symbol, end) == ("AAA", NOW)
        assert start > FAR_PAST
        assert start >= EARLIEST_START
        assert start == NOW - fetch_span(512, _ONE_DAY)

    def test_an_intraday_poll_asks_a_window_sized_by_its_interval(self) -> None:
        adapter = _AlpacaLikeAdapter([])
        feed = RecentWindowFeed(adapter, FakeClock(NOW), interval_is_complete(_FIVE_MIN))

        feed.poll(["AAA"], lookback=512)

        _, start, _ = adapter.requests[0]
        assert start == NOW - fetch_span(512, _FIVE_MIN)
        # A 5-minute bar packs ~78 to a session, so its window is far shorter than
        # the daily one for the same lookback. Asking daily-sized here would drag
        # SyntheticAdapter into millions of bars (the second KAN-714 pathology).
        assert fetch_span(512, _FIVE_MIN) < fetch_span(512, _ONE_DAY) / 50

    def test_the_bug_itself_a_refusing_provider_now_yields_bars(self) -> None:
        # The whole ticket in one assertion: same adapter, same universe, same
        # lookback. On the unbounded request every symbol read absent.
        days = _trading_days(30, ending=NOW)
        bars = [b for s in ("AAA", "BBB", "CCC") for b in _five_minute_bars(s, days)]
        adapter = _AlpacaLikeAdapter(bars)
        feed = RecentWindowFeed(adapter, FakeClock(NOW), interval_is_complete(_FIVE_MIN))

        groups = feed.poll(["AAA", "BBB", "CCC"], lookback=512)

        assert feed.absent == []
        assert len(groups) == 512
        for _, slice_ in groups:
            assert set(slice_) == {"AAA", "BBB", "CCC"}


class TestTheWindowIsBigEnough:
    """Under-sizing silently truncates the ADR-0042 warmup — the failure just fixed."""

    def test_a_daily_window_holds_a_full_lookback_of_real_calendar(self) -> None:
        days = _with_holidays(_trading_days(4_000, ending=NOW))
        adapter = _AlpacaLikeAdapter(_daily_bars("AAA", days))
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        assert len(feed.poll(["AAA"], lookback=512)) == 512

    def test_a_five_minute_window_holds_a_full_lookback_of_real_calendar(self) -> None:
        days = _with_holidays(_trading_days(200, ending=NOW))
        adapter = _AlpacaLikeAdapter(_five_minute_bars("AAA", days))
        feed = RecentWindowFeed(adapter, FakeClock(NOW), interval_is_complete(_FIVE_MIN))

        assert len(feed.poll(["AAA"], lookback=512)) == 512

    def test_a_one_minute_window_holds_a_full_lookback_of_real_calendar(self) -> None:
        one_min = timedelta(minutes=1)
        days = _with_holidays(_trading_days(40, ending=NOW))
        bars = [
            Bar("AAA", d + _SESSION_OPEN + i * one_min, 1.0, 1.0, 1.0, 1.0, volume=1)
            for d in days
            for i in range(390)
        ]
        adapter = _AlpacaLikeAdapter(bars)
        feed = RecentWindowFeed(adapter, FakeClock(NOW), interval_is_complete(one_min))

        assert len(feed.poll(["AAA"], lookback=512)) == 512

    def test_an_hourly_window_holds_a_full_lookback_of_real_calendar(self) -> None:
        hour = timedelta(hours=1)
        days = _with_holidays(_trading_days(400, ending=NOW))
        bars = [
            Bar("AAA", d + _SESSION_OPEN + i * hour, 1.0, 1.0, 1.0, 1.0, volume=1)
            for d in days
            for i in range(7)  # a 6.5h session gives 7 hourly bars, the last short
        ]
        adapter = _AlpacaLikeAdapter(bars)
        feed = RecentWindowFeed(adapter, FakeClock(NOW), interval_is_complete(hour))

        assert len(feed.poll(["AAA"], lookback=512)) == 512


class TestFetchSpan:
    """The multiplier is derived, not guessed — so it is asserted, not described."""

    def test_a_daily_span_pays_for_weekends_and_holidays(self) -> None:
        # 512 sessions is ~742 calendar days before any slack at all; anything at
        # or below 512 days would truncate by ~30% on the real calendar.
        span = fetch_span(512, _ONE_DAY)
        assert span.days > 742
        assert span.days == pytest.approx(512 * (365 / 252) * 4, rel=0.01)

    def test_an_intraday_span_pays_for_the_closed_17_5_hours_too(self) -> None:
        # 512 five-minute bars is 42.7 hours of *wall clock* but 6.6 trading
        # sessions, i.e. ~9.5 calendar days. A span sized on 512 x 5min would be
        # ~4.5x short — which is exactly the kind of under-sizing that would have
        # truncated the warmup silently.
        span = fetch_span(512, _FIVE_MIN)
        assert span > 512 * _FIVE_MIN * 4
        assert span.days == pytest.approx(512 / 78 * (365 / 252) * 4, rel=0.05)

    def test_a_tiny_lookback_still_gets_a_usable_floor(self) -> None:
        assert fetch_span(1, timedelta(minutes=1)) == MIN_FETCH_SPAN

    def test_an_absurd_lookback_cannot_overflow_the_window(self) -> None:
        # A bounded request must stay a datetime, whatever it is handed.
        adapter = _AlpacaLikeAdapter([])
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        feed.poll(["AAA"], lookback=10**9)

        _, start, _ = adapter.requests[0]
        assert start == EARLIEST_START  # clamped to the floor, never year 1

    def test_the_floor_is_a_start_the_provider_answers(self) -> None:
        # Measured 2026-08-09: Alpaca answers 1900-01-01 with 1516 daily bars and
        # datetime.min with none. The floor is the former, on purpose.
        assert datetime(1900, 1, 1, tzinfo=UTC) == EARLIEST_START


class TestTheIntervalComesFromTheCompletenessPolicy:
    """The policy already knows the bar length; the feed asks it rather than guessing."""

    def test_the_default_policy_means_daily(self) -> None:
        adapter = _AlpacaLikeAdapter([])
        RecentWindowFeed(adapter, FakeClock(NOW), default_is_complete).poll(["AAA"], lookback=100)

        _, start, _ = adapter.requests[0]
        assert start == NOW - fetch_span(100, _ONE_DAY)

    def test_an_explicit_interval_wins_over_the_policy(self) -> None:
        adapter = _AlpacaLikeAdapter([])
        feed = RecentWindowFeed(
            adapter, FakeClock(NOW), interval_is_complete(_FIVE_MIN), interval=_ONE_DAY
        )

        feed.poll(["AAA"], lookback=100)

        _, start, _ = adapter.requests[0]
        assert start == NOW - fetch_span(100, _ONE_DAY)

    def test_a_custom_policy_falls_back_to_daily_and_can_be_told_otherwise(self) -> None:
        # The policy seam stays open (a market calendar could replace it); a policy
        # that does not state an interval gets the conservative daily window, and
        # the explicit kwarg is the way to narrow it.
        adapter = _AlpacaLikeAdapter([])
        RecentWindowFeed(adapter, FakeClock(NOW), lambda b, now: True).poll(["AAA"], lookback=100)
        assert adapter.requests[0][1] == NOW - fetch_span(100, _ONE_DAY)

        other = _AlpacaLikeAdapter([])
        RecentWindowFeed(other, FakeClock(NOW), lambda b, now: True, interval=_FIVE_MIN).poll(
            ["AAA"], lookback=100
        )
        assert other.requests[0][1] == NOW - fetch_span(100, _FIVE_MIN)

    def test_the_completeness_behaviour_is_unchanged(self) -> None:
        # interval_is_complete's contract (ADR-0022) must survive it also carrying
        # its interval: complete exactly at ts + interval, not before.
        policy = interval_is_complete(_FIVE_MIN)
        bar = Bar("AAA", NOW, 1.0, 1.0, 1.0, 1.0, volume=1)

        assert not policy(bar, NOW)
        assert not policy(bar, NOW + timedelta(minutes=4, seconds=59))
        assert policy(bar, NOW + _FIVE_MIN)
        assert policy.interval == _FIVE_MIN


class TestAbsenceIsNotMasked:
    """ADR-0035 must keep working: a real absence is still reported as one."""

    def test_a_symbol_with_no_bars_is_still_absent(self) -> None:
        days = _trading_days(600, ending=NOW)
        adapter = _AlpacaLikeAdapter(_daily_bars("AAA", days))
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        feed.poll(["AAA", "GONE"], lookback=512)

        assert [(a.symbol, a.reason) for a in feed.absent] == [("GONE", REASON_NO_BARS)]
        assert feed.absence_streaks == {"GONE": 1}

    def test_a_symbol_older_than_the_window_is_absent_not_silently_extended(self) -> None:
        # OLD's bars all predate the bounded window. That is a genuine absence from
        # the window the feed asked for, and it is reported — the fix makes the
        # request sane, it does not make absence quieter.
        old = _daily_bars("OLD", _trading_days(5, ending=NOW - timedelta(days=20_000)))
        adapter = _AlpacaLikeAdapter(old)
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        feed.poll(["OLD"], lookback=512)

        assert [(a.symbol, a.reason) for a in feed.absent] == [("OLD", REASON_NO_BARS)]


def _universe_alarms(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Only the universe-wide alarm — ADR-0035's per-symbol escalation is ERROR too."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "requested symbols returned no bars" in r.getMessage()
    ]


class TestAWholeUniverseGoingQuietIsLoud:
    """The silence is what hid KAN-714 for months, so it now says something."""

    def test_every_symbol_absent_at_once_logs_the_window_it_asked_for(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _AlpacaLikeAdapter([])  # the source answers, with nothing, for all
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            feed.poll(["AAA", "BBB", "CCC"], lookback=512)

        errors = _universe_alarms(caplog)
        assert len(errors) == 1
        message = errors[0]
        # The message must name the window, because the window is the suspect.
        assert (NOW - fetch_span(512, _ONE_DAY)).isoformat() in message
        assert NOW.isoformat() in message

    def test_it_is_logged_once_per_outage_not_once_per_poll(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _AlpacaLikeAdapter([])
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            for _ in range(5):
                feed.poll(["AAA", "BBB"], lookback=512)

        assert len(_universe_alarms(caplog)) == 1

    def test_a_recovered_universe_re_arms_the_alarm(self, caplog: pytest.LogCaptureFixture) -> None:
        # Once per *outage*, not once per session: a second outage must be heard.
        adapter = _AlpacaLikeAdapter(_daily_bars("AAA", _trading_days(30, ending=NOW)))
        adapter.blackout = True
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            feed.poll(["AAA"], lookback=10)
            adapter.blackout = False  # the outage ends
            feed.poll(["AAA"], lookback=10)
            adapter.blackout = True  # and comes back
            feed.poll(["AAA"], lookback=10)

        assert len(_universe_alarms(caplog)) == 2

    def test_one_healthy_symbol_is_enough_to_stay_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _AlpacaLikeAdapter(_daily_bars("AAA", _trading_days(30, ending=NOW)))
        feed = RecentWindowFeed(adapter, FakeClock(NOW))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            feed.poll(["AAA", "GONE"], lookback=10)

        assert [r.levelno for r in caplog.records] == [logging.WARNING]  # ADR-0035 only

    def test_a_still_forming_universe_is_not_silence(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Bars exist, none complete yet: the normal state at an interval boundary
        # (ADR-0022), and emphatically not a suspect request.
        bar_ts = NOW - timedelta(minutes=1)
        bars = [Bar(s, bar_ts, 1.0, 1.0, 1.0, 1.0, volume=1) for s in ("AAA", "BBB")]
        adapter = _AlpacaLikeAdapter(bars)
        feed = RecentWindowFeed(adapter, FakeClock(NOW), interval_is_complete(_FIVE_MIN))

        with caplog.at_level(logging.INFO, logger="trading.data.recent_window"):
            assert feed.poll(["AAA", "BBB"], lookback=512) == []

        assert caplog.records == []


class TestTheHappyPathIsUndisturbed:
    """The bounded window must not change *which* bars a poll yields."""

    def test_a_forgiving_adapter_gives_exactly_what_it_gave_before(self) -> None:
        days = _trading_days(6, ending=datetime(2024, 1, 8, tzinfo=UTC))
        bars = _daily_bars("AAA", days) + _daily_bars("BBB", days)
        clock = FakeClock(datetime(2024, 1, 10, tzinfo=UTC))

        assert RecentWindowFeed(FakeAdapter(bars), clock).poll(["AAA", "BBB"], lookback=3) == [
            (
                d,
                {
                    "AAA": Bar("AAA", d, 100.0, 100.0, 100.0, 100.0, 1_000),
                    "BBB": Bar("BBB", d, 100.0, 100.0, 100.0, 100.0, 1_000),
                },
            )
            for d in days[-3:]
        ]
