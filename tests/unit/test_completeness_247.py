"""Bar completeness for a market that never closes (ADR-0053, KAN-706).

``default_is_complete`` decides a daily bar is finished once the clock's UTC date
is strictly later than the bar's. That is a **session** rule — it asks whether the
venue's calendar day has turned over — and a venue that never closes has no
session, so its daily bar is a rolling 24-hour window whose closing instant is a
convention someone has to choose. The convention this bench chooses is **UTC
midnight**, and the point of this file is that it needs no new policy:
``interval_is_complete(timedelta(days=1))`` (ADR-0022) already *is* that rule,
because ``ts + interval`` needs no calendar.

What these tests pin, in order:

1. On a UTC-midnight-stamped daily bar the two rules are **indistinguishable** —
   swept minute by minute across three days, zero disagreements. That is the
   evidence for "no new code": the 24/7 rule is an existing one, named.
2. Off midnight they diverge, and in **one direction only** — the session rule is
   *early* by exactly the stamp's offset from midnight, i.e. it hands the strategy
   a bar that is still forming. Harmless for US equities (the session ends at
   20:00/21:00 UTC, so the date rollover is always after the real close); a real
   defect for a 24/7 venue, where there is no close to make it late.
3. The divergence at **feed level**, not merely policy level: the same 24/7 series
   through the same :class:`~trading.data.recent_window.RecentWindowFeed` yields a
   forming bar under the session rule and does not under the rolling-day rule.
4. ``fetch_span``'s hardcoded equity calendar (6.5 h sessions, 365/252 days) is
   **wrong wide** for 24/7 and therefore safe: a continuous lookback is never
   truncated. Assessed here, deliberately not refactored.

**Why nothing here uses ``SyntheticAdapter`` or ``FakeAdapter``** — ADR-0040's
lesson, third sighting (ADR-0047 was the second). ``SyntheticAdapter`` emits
weekday-only bars inside a 13:30-20:00 UTC session and clips an absurd start;
``FakeAdapter`` filters any range it is handed. Neither can represent a market that
never closes, and a stand-in more forgiving than the provider cannot test provider
behaviour. The 24/7 series and the clock states are therefore built explicitly, and
:class:`_ContinuousAdapter` reproduces the one provider pathology ADR-0047 measured
(an unanswerably early start comes back **empty**, not as an error) so this file
cannot quietly regress that either.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.clock import FakeClock
from trading.data.recent_window import (
    RecentWindowFeed,
    default_is_complete,
    fetch_span,
    interval_is_complete,
)
from trading.types import Bar

_ONE_DAY = timedelta(days=1)
_ONE_HOUR = timedelta(hours=1)
_FIVE_MIN = timedelta(minutes=5)
_ONE_MIN = timedelta(minutes=1)

# A Saturday, deliberately: a 24/7 market trades through it, and every equity
# stand-in in this repo would have no bar here at all.
_SATURDAY = datetime(2026, 8, 8, tzinfo=UTC)

# The rolling-day rule, i.e. the 24/7 daily convention (ADR-0053).
ROLLING_DAY = interval_is_complete(_ONE_DAY)


class _ContinuousAdapter:
    """A 24/7 provider stand-in: bars on every day, at every hour, plus a spy.

    Two behaviours it must have, both inherited from what ADR-0047 measured against
    the live API rather than assumed:

    * a start before :attr:`ANSWERABLE_FROM` comes back as an **empty list** — no
      exception, no partial answer;
    * any bounded start inside its data serves the slice normally.

    Every call lands on :attr:`requests` as ``(symbol, start, end)``, so a test can
    assert on the *request* and not only on what came back.
    """

    ANSWERABLE_FROM = datetime(1900, 1, 1, tzinfo=UTC)

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.requests: list[tuple[str, datetime, datetime]] = []

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        self.requests.append((symbol, start, end))
        if start < self.ANSWERABLE_FROM:
            return []
        return [b for b in self._bars if b.symbol == symbol and start <= b.ts <= end]


def _continuous_bars(
    symbol: str, count: int, interval: timedelta, *, ending: datetime
) -> list[Bar]:
    """``count`` back-to-back bars of ``interval``, the last one starting at ``ending``.

    No weekends dropped, no session window, no holidays — which is the whole point:
    this is what a market that never closes actually produces.
    """
    return [
        Bar(symbol, ending - i * interval, 100.0, 101.0, 99.0, 100.0, volume=1_000)
        for i in reversed(range(count))
    ]


class TestTheTwoDailyRulesAgreeAtUtcMidnight:
    """The evidence for "no new policy": the 24/7 rule is an existing one, named."""

    def test_a_midnight_stamped_daily_bar_gets_the_same_verdict_every_minute(self) -> None:
        # Swept rather than spot-checked, because "these two rules are the same
        # thing" is the claim the whole decision rests on. 4,320 minutes, three
        # days, both sides of the boundary.
        bar = Bar("BTCUSD", _SATURDAY, 100.0, 101.0, 99.0, 100.0, volume=1)

        disagreements = [
            now
            for minute in range(3 * 24 * 60)
            if default_is_complete(bar, (now := _SATURDAY + minute * _ONE_MIN))
            != ROLLING_DAY(bar, now)
        ]

        assert disagreements == []

    def test_the_shared_boundary_is_utc_midnight_to_the_second(self) -> None:
        bar = Bar("BTCUSD", _SATURDAY, 100.0, 101.0, 99.0, 100.0, volume=1)
        just_before = _SATURDAY + _ONE_DAY - timedelta(seconds=1)
        midnight = _SATURDAY + _ONE_DAY

        assert not default_is_complete(bar, just_before)
        assert not ROLLING_DAY(bar, just_before)
        assert default_is_complete(bar, midnight)
        assert ROLLING_DAY(bar, midnight)

    def test_the_convention_is_stated_by_the_policy_itself(self) -> None:
        # A rolling 24-hour window, readable as such (ADR-0047 made the interval
        # public for window sizing; ADR-0053 leans on the same fact for clarity).
        assert ROLLING_DAY.interval == _ONE_DAY


class TestTheSessionRuleIsEarlyOffMidnight:
    """Where the rules part, and why the direction is what makes it a defect."""

    @pytest.mark.parametrize("offset_hours", [4, 8, 13])
    def test_the_session_rule_calls_an_unfinished_rolling_day_complete(
        self, offset_hours: int
    ) -> None:
        # A provider that anchors its 24/7 daily bar somewhere other than UTC
        # midnight (an 08:00 "trading day", say) breaks the session rule outright:
        # at UTC midnight the bar has `offset_hours` still to run.
        bar = Bar(
            "BTCUSD", _SATURDAY + offset_hours * _ONE_HOUR, 100.0, 101.0, 99.0, 100.0, volume=1
        )
        utc_midnight = _SATURDAY + _ONE_DAY

        assert default_is_complete(bar, utc_midnight), "the session rule sees a new UTC date"
        assert not ROLLING_DAY(bar, utc_midnight), "but the 24-hour window has not elapsed"

        early_by = [
            minute
            for minute in range(3 * 24 * 60)
            if default_is_complete(bar, (now := _SATURDAY + minute * _ONE_MIN))
            and not ROLLING_DAY(bar, now)
        ]
        # Early by exactly the stamp's offset from midnight — not approximately.
        assert len(early_by) == offset_hours * 60

    @pytest.mark.parametrize("offset_hours", [0, 4, 8, 13, 23])
    def test_the_rolling_day_rule_is_never_early_whatever_the_stamp(
        self, offset_hours: int
    ) -> None:
        # The asymmetry that makes this the safe default for 24/7: it can be later
        # than a session rule, never earlier than the bar's own close.
        ts = _SATURDAY + offset_hours * _ONE_HOUR
        bar = Bar("BTCUSD", ts, 100.0, 101.0, 99.0, 100.0, volume=1)

        for minute in range(3 * 24 * 60):
            now = _SATURDAY + minute * _ONE_MIN
            assert ROLLING_DAY(bar, now) == (now >= ts + _ONE_DAY)

    def test_the_equity_path_is_why_the_session_rule_is_safe_there(self) -> None:
        # Not a 24/7 test: it records *why* this is not a bug for US equities and
        # therefore why the equity default must stay. A session ending 20:00 UTC is
        # over before the UTC date turns, so the session rule errs late whatever
        # hour the provider stamps the daily bar at.
        session_close = _ONE_HOUR * 20
        for stamp_hours in (0, 4, 5, 13):
            bar = Bar("AAPL", _SATURDAY + stamp_hours * _ONE_HOUR, 1.0, 1.0, 1.0, 1.0, volume=1)
            assert not default_is_complete(bar, _SATURDAY + session_close)
            assert default_is_complete(bar, _SATURDAY + _ONE_DAY)


class TestAContinuousFeedGatesOnTheRollingDay:
    """The same divergence through the real feed, since that is what trades."""

    def _bars(self) -> list[Bar]:
        # A 24/7 daily series anchored at 08:00 UTC: the convention-violating case,
        # which is exactly where the choice of rule becomes visible.
        return _continuous_bars("BTCUSD", 40, _ONE_DAY, ending=_SATURDAY + 8 * _ONE_HOUR)

    def test_the_session_rule_yields_the_still_forming_bar(self) -> None:
        # Pinned as the *defect*, not as desired behaviour: a feed left on the
        # equity default hands the strategy a bar with 8 hours still to run.
        adapter = _ContinuousAdapter(self._bars())
        now = _SATURDAY + _ONE_DAY + _ONE_HOUR  # 01:00 the next UTC day
        feed = RecentWindowFeed(adapter, FakeClock(now), default_is_complete)

        groups = feed.poll(["BTCUSD"], lookback=5)

        assert groups[-1][0] == _SATURDAY + 8 * _ONE_HOUR
        assert not ROLLING_DAY(groups[-1][1]["BTCUSD"], now), "and it is not finished"

    def test_the_rolling_day_rule_holds_it_back_until_it_closes(self) -> None:
        adapter = _ContinuousAdapter(self._bars())
        forming = _SATURDAY + 8 * _ONE_HOUR
        withheld = RecentWindowFeed(
            adapter, FakeClock(_SATURDAY + _ONE_DAY + _ONE_HOUR), ROLLING_DAY
        ).poll(["BTCUSD"], lookback=5)

        assert [ts for ts, _ in withheld][-1] == forming - _ONE_DAY

        # ...and released the instant the 24 hours are up, not a moment later.
        released = RecentWindowFeed(adapter, FakeClock(forming + _ONE_DAY), ROLLING_DAY).poll(
            ["BTCUSD"], lookback=5
        )
        assert [ts for ts, _ in released][-1] == forming

    def test_a_weekend_is_an_ordinary_pair_of_bars(self) -> None:
        # Nothing in the completeness path drops a Saturday or Sunday — the calendar
        # never enters it. Worth pinning because every other bar-producing thing in
        # this repo does drop them.
        friday = _SATURDAY - _ONE_DAY
        adapter = _ContinuousAdapter(
            _continuous_bars("ETHUSD", 5, _ONE_DAY, ending=_SATURDAY + _ONE_DAY)
        )
        feed = RecentWindowFeed(adapter, FakeClock(_SATURDAY + 2 * _ONE_DAY), ROLLING_DAY)

        stamps = [ts for ts, _ in feed.poll(["ETHUSD"], lookback=3)]

        assert {friday, _SATURDAY, _SATURDAY + _ONE_DAY} == set(stamps)
        assert feed.absent == []


class TestTheEquityCalendarWindowDoesNotTruncateAContinuousLookback:
    """``fetch_span`` hardcodes the equity calendar; for 24/7 it errs wide (ADR-0053)."""

    @pytest.mark.parametrize(
        ("interval", "expected_ratio"),
        [
            (_ONE_DAY, 5.79),
            (_ONE_HOUR, 21.39),
            (_FIVE_MIN, 21.39),
            (_ONE_MIN, 21.39),
        ],
    )
    def test_the_span_exceeds_what_a_continuous_market_needs(
        self, interval: timedelta, expected_ratio: float
    ) -> None:
        # A 24/7 source needs exactly `lookback x interval` of wall clock. The
        # measured over-ask is the safe direction (a short window silently truncates
        # the ADR-0042 warmup); the numbers are pinned so a future tightening of
        # WINDOW_SLACK cannot quietly cross zero for a continuous market.
        needed = 512 * interval
        span = fetch_span(512, interval)

        assert span > needed
        assert span / needed == pytest.approx(expected_ratio, rel=0.01)

    @pytest.mark.parametrize("interval", [_ONE_DAY, _ONE_HOUR, _FIVE_MIN, _ONE_MIN])
    def test_a_continuous_poll_still_gets_a_full_lookback(self, interval: timedelta) -> None:
        # End to end through the feed at every supported interval, against bars that
        # exist on every day and hour. 600 > 512, so a truncated window shows up as
        # a short answer.
        bars = _continuous_bars("BTCUSD", 600, interval, ending=_SATURDAY)
        adapter = _ContinuousAdapter(bars)
        feed = RecentWindowFeed(
            adapter, FakeClock(_SATURDAY + interval), interval_is_complete(interval)
        )

        assert len(feed.poll(["BTCUSD"], lookback=512)) == 512
        _, start, _ = adapter.requests[0]
        assert start == _SATURDAY + interval - fetch_span(512, interval)

    def test_adopting_the_rolling_day_rule_leaves_the_request_unchanged(self) -> None:
        # The two daily policies size the window identically, so a 24/7 feed swapping
        # onto the rolling-day rule changes *which bars are complete* and nothing
        # about what it asks the provider for (ADR-0047's sizing seam is untouched).
        session, rolling = _ContinuousAdapter([]), _ContinuousAdapter([])
        now = _SATURDAY + _ONE_DAY

        RecentWindowFeed(session, FakeClock(now), default_is_complete).poll(
            ["BTCUSD"], lookback=200
        )
        RecentWindowFeed(rolling, FakeClock(now), ROLLING_DAY).poll(["BTCUSD"], lookback=200)

        assert session.requests == rolling.requests
        assert session.requests[0][1] == now - fetch_span(200, _ONE_DAY)

    def test_the_bounded_window_still_matters_for_a_continuous_source(self) -> None:
        # ADR-0047 must not regress on this path either: the stand-in refuses an
        # unanswerably early start with an empty answer, so a poll that reached for
        # datetime.min would come back with nothing at all.
        adapter = _ContinuousAdapter(_continuous_bars("BTCUSD", 30, _ONE_DAY, ending=_SATURDAY))

        assert adapter.get_bars("BTCUSD", datetime.min.replace(tzinfo=UTC), _SATURDAY) == []
        assert len(adapter.get_bars("BTCUSD", _SATURDAY - 30 * _ONE_DAY, _SATURDAY)) == 30


class TestTheOfflineStandInsCannotProveA247Claim:
    """ADR-0040's lesson, third sighting — pinned so this file cannot be "simplified"."""

    def test_the_synthetic_adapter_emits_no_weekend_bars(self) -> None:
        from trading.data.synthetic import SyntheticAdapter

        bars = SyntheticAdapter(seed=7).get_bars(
            "AAA", datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 10, tzinfo=UTC)
        )

        assert bars, "expected a week of synthetic daily bars"
        assert all(b.ts.weekday() < 5 for b in bars), (
            "SyntheticAdapter is weekday-only, so it cannot stand in for a 24/7 market"
        )

    def test_the_synthetic_adapter_confines_intraday_bars_to_an_equity_session(self) -> None:
        from trading.data.synthetic import SyntheticAdapter
        from trading.frequency import Frequency

        bars = SyntheticAdapter(seed=7, frequency=Frequency.parse("1h")).get_bars(
            "AAA", datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 5, tzinfo=UTC)
        )

        assert bars
        # 13:30-20:00 UTC = 09:30-16:00 ET. A continuous market has bars at 03:00.
        assert all(
            timedelta(hours=13, minutes=30) <= _time_of_day(b.ts) < _ONE_HOUR * 20 for b in bars
        )


def _time_of_day(ts: datetime) -> timedelta:
    return timedelta(hours=ts.hour, minutes=ts.minute, seconds=ts.second)
