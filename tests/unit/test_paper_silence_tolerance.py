"""A live session tolerates silence in proportion to its bar interval (ADR-0049).

The bug (KAN-671): ``PaperSession.run`` stops after ``max_empty_polls`` consecutive
polls that reveal no new bar, the default is ``2``, and ``trading paper`` overrode it
only on the ``--once`` path. So the ``--live`` path inherited ``2``, and ``2`` means
two *polls* — which is ten minutes at ``--interval 5m`` and two days at ``1d``. One
constant cannot mean the same thing at both cadences, and both ends of it were wrong:

* a daily live session started on a Thursday exited on Monday at 00:00 UTC, before
  Monday's session had happened at all; and
* a 5-minute live session that hit a twenty-minute data gap at 11:00 exited there,
  having traded 17 bars of a 77-bar day.

Both were measured on the real live wiring (``RecentWindowFeed`` +
``interval_is_complete``/``default_is_complete`` + ``PaperSession`` + a ``FakeClock``
that advances on ``sleep_until``) and both are pinned below.

The fix is a *duration* — :data:`LIVE_SILENCE_TOLERANCE` of wall-clock quiet,
converted at the session's poll interval by :func:`silence_tolerance_polls` and
floored at :data:`MIN_LIVE_EMPTY_POLLS` — chosen at the CLI, where the live/once
distinction lives. ``PaperSession.run``'s own default is deliberately untouched.

Everything here is offline and deterministic. No clock ever really waits.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner, Result

from trading.broker import SimulatedBroker
from trading.cli import app
from trading.clock import FakeClock, WallClock
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.recent_window import (
    RecentWindowFeed,
    default_is_complete,
    interval_is_complete,
)
from trading.engine import (
    LIVE_SILENCE_TOLERANCE,
    MIN_LIVE_EMPTY_POLLS,
    Engine,
    PaperSession,
    silence_tolerance_polls,
)
from trading.frequency import Frequency
from trading.interfaces import StrategyContext
from trading.risk import Guardrails
from trading.types import Bar, Order, Portfolio, TargetWeight

runner = CliRunner()

_ZERO_COST = CostConfig(commission_per_share=0.0, slippage_bps=0.0)

# US regular session in UTC: 09:30-16:00 ET is 13:30-20:00 UTC in August (EDT).
_OPEN_UTC = (13, 30)
_CLOSE_UTC = (20, 0)


class _Flat:
    """A strategy that never trades: this slice is about the loop, not the orders."""

    name = "flat"

    def on_bar(
        self, ts: datetime, bars: dict[str, Bar], context: StrategyContext
    ) -> list[Order | TargetWeight]:
        return []


def _intraday_bars(
    day: datetime, step: timedelta, *, gap: tuple[datetime, datetime] | None = None
) -> list[Bar]:
    """One regular session of ``step`` bars, optionally with a hole punched in it."""
    out: list[Bar] = []
    ts = day.replace(hour=_OPEN_UTC[0], minute=_OPEN_UTC[1])
    close = day.replace(hour=_CLOSE_UTC[0], minute=_CLOSE_UTC[1])
    price = 100.0
    while ts < close:
        if gap is None or not (gap[0] <= ts < gap[1]):
            out.append(Bar("AAA", ts, price, price, price, price, 1_000))
        ts += step
        price += 0.01
    return out


def _weekday_daily_bars(
    first: datetime, days: int, *, holidays: frozenset[int] = frozenset()
) -> list[Bar]:
    """Daily bars for weekdays only, skipping ``holidays`` (day-of-month numbers)."""
    out: list[Bar] = []
    price = 100.0
    for i in range(days):
        day = first + timedelta(days=i)
        if day.weekday() < 5 and day.day not in holidays:
            out.append(Bar("AAA", day, price, price, price, price, 1_000))
        price += 1.0
    return out


def _live_session(
    bars: list[Bar],
    freq: Frequency,
    start: datetime,
) -> tuple[PaperSession, FakeClock]:
    """The real live wiring, offline: recent-window feed, fake clock, warmup on."""
    adapter = FakeAdapter(bars)
    clock = FakeClock(start)
    policy = interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete
    feed = RecentWindowFeed(adapter, clock, policy)
    engine = Engine(
        adapter, SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST), Guardrails(RiskConfig())
    )
    session = PaperSession(
        engine, _Flat(), ["AAA"], feed, clock, lookback=512, frequency=freq, warmup=True
    )
    return session, clock


class TestTheToleranceIsDerivedFromTheInterval:
    """A count of polls is the wrong unit; the policy is a duration."""

    def test_five_minute_and_daily_sessions_get_different_poll_counts(self) -> None:
        """The whole point: one constant cannot serve both cadences."""
        five_minute = silence_tolerance_polls(timedelta(minutes=5))
        daily = silence_tolerance_polls(timedelta(days=1))

        assert five_minute != daily
        assert five_minute == 12  # 60 minutes / 5 minutes
        assert daily == MIN_LIVE_EMPTY_POLLS

    @pytest.mark.parametrize(
        ("interval", "expected"),
        [
            (timedelta(minutes=1), 60),  # the duration binds
            (timedelta(minutes=5), 12),  # the duration binds
            (timedelta(minutes=30), MIN_LIVE_EMPTY_POLLS),  # 2 polls -> the floor binds
            (timedelta(hours=1), MIN_LIVE_EMPTY_POLLS),  # 1 poll -> the floor binds
            (timedelta(days=1), MIN_LIVE_EMPTY_POLLS),  # <1 poll -> the floor binds
        ],
    )
    def test_every_standard_interval(self, interval: timedelta, expected: int) -> None:
        assert silence_tolerance_polls(interval) == expected

    def test_the_duration_is_never_rounded_down(self) -> None:
        """A tolerance shorter than asked for is the failure being fixed."""
        polls = silence_tolerance_polls(timedelta(minutes=7))

        assert polls * timedelta(minutes=7) >= LIVE_SILENCE_TOLERANCE

    def test_a_non_positive_interval_falls_back_to_the_floor(self) -> None:
        """Defensive: a bad interval must not divide by zero or return zero polls."""
        assert silence_tolerance_polls(timedelta(0)) == MIN_LIVE_EMPTY_POLLS
        assert silence_tolerance_polls(timedelta(seconds=-1)) == MIN_LIVE_EMPTY_POLLS

    def test_the_floor_is_a_floor_not_a_cap(self) -> None:
        assert silence_tolerance_polls(timedelta(minutes=5)) > MIN_LIVE_EMPTY_POLLS


class TestAnIntradaySessionSurvivesAGap:
    """The measured failure: a 5m session died at 11:10 on a 20-minute data gap."""

    _DAY = datetime(2026, 8, 10, tzinfo=UTC)
    _FIVE_MIN = Frequency.parse("5m")

    def _run(self, bars: list[Bar], max_empty_polls: int) -> tuple[PaperSession, FakeClock]:
        session, clock = _live_session(bars, self._FIVE_MIN, self._DAY.replace(hour=13, minute=32))
        session.run(max_empty_polls=max_empty_polls, max_polls=4_000)
        return session, clock

    def test_the_old_default_of_two_ends_the_day_at_the_gap(self) -> None:
        """Pin the bug, so the fix is measured against a number and not a feeling."""
        gap = (self._DAY.replace(hour=15, minute=0), self._DAY.replace(hour=15, minute=20))
        session, clock = self._run(_intraday_bars(self._DAY, timedelta(minutes=5), gap=gap), 2)

        assert len(session.session_log) == 17
        assert clock.now() == self._DAY.replace(hour=15, minute=10)

    def test_the_derived_tolerance_trades_the_whole_day_through_the_gap(self) -> None:
        gap = (self._DAY.replace(hour=15, minute=0), self._DAY.replace(hour=15, minute=20))
        bars = _intraday_bars(self._DAY, timedelta(minutes=5), gap=gap)
        polls = silence_tolerance_polls(self._FIVE_MIN.delta)

        session, _clock = self._run(bars, polls)

        # Every bar in the day except the one absorbed as warmup.
        assert len(session.session_log) == len(bars) - session.warmup_bars
        # And it kept going *past* the gap: the last bar traded is the day's last.
        assert session.session_log[-1].ts == self._DAY.replace(hour=19, minute=55)

    def test_it_still_ends_after_the_close_rather_than_hanging(self) -> None:
        """Ending late is the cheap error; never ending is not an option."""
        polls = silence_tolerance_polls(self._FIVE_MIN.delta)

        session, clock = self._run(_intraday_bars(self._DAY, timedelta(minutes=5)), polls)

        # 16:00 ET = 20:00 UTC is the close, and the last bar (19:55) is revealed by
        # the 20:00 poll; the session then waits out the full 60-minute tolerance.
        assert clock.now() == self._DAY.replace(hour=21, minute=0)
        assert len(session.session_log) == 77


class TestADailySessionSurvivesAWeekend:
    """The other half of the same bug: 2 polls is 2 days at ``--interval 1d``."""

    _FREQ = Frequency.parse("1d")
    # Thursday 2026-08-06, mid-morning: a session started before a normal weekend.
    _START = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)

    def _run(self, bars: list[Bar], max_empty_polls: int) -> tuple[PaperSession, FakeClock]:
        session, clock = _live_session(bars, self._FREQ, self._START)
        session.run(max_empty_polls=max_empty_polls, max_polls=4_000)
        return session, clock

    def test_the_old_default_of_two_dies_on_monday_before_the_open(self) -> None:
        bars = _weekday_daily_bars(datetime(2026, 8, 3, tzinfo=UTC), 19)

        _session, clock = self._run(bars, 2)

        assert clock.now() == datetime(2026, 8, 10, tzinfo=UTC)  # Monday 00:00 UTC

    def test_the_derived_tolerance_carries_it_into_the_next_week(self) -> None:
        bars = _weekday_daily_bars(datetime(2026, 8, 3, tzinfo=UTC), 19)
        polls = silence_tolerance_polls(self._FREQ.delta)

        session, _clock = self._run(bars, polls)

        # It survived the weekend and went on trading the following week.
        traded = [outcome.ts for outcome in session.session_log]
        assert datetime(2026, 8, 10, tzinfo=UTC) in traded  # Monday
        assert datetime(2026, 8, 14, tzinfo=UTC) in traded  # the following Friday

    def test_it_also_survives_a_three_day_weekend(self) -> None:
        """A Monday holiday is three quiet polls, and the floor of 4 clears it.

        Measured rather than assumed: with a Monday holiday the empty polls are
        Sunday (no Saturday bar), Monday (no Sunday bar) and Tuesday (no Monday
        bar) — three — and Wednesday reveals Tuesday's bar.
        """
        bars = _weekday_daily_bars(datetime(2026, 8, 3, tzinfo=UTC), 19, holidays=frozenset({10}))
        polls = silence_tolerance_polls(self._FREQ.delta)

        session, _clock = self._run(bars, polls)

        traded = [outcome.ts for outcome in session.session_log]
        assert datetime(2026, 8, 10, tzinfo=UTC) not in traded  # the holiday
        assert datetime(2026, 8, 11, tzinfo=UTC) in traded  # and it kept running

    def test_sustained_silence_still_ends_it(self) -> None:
        """The backstop stays a backstop: a dead feed must not run forever."""
        bars = _weekday_daily_bars(datetime(2026, 8, 3, tzinfo=UTC), 5)  # nothing after Friday
        polls = silence_tolerance_polls(self._FREQ.delta)

        _session, clock = self._run(bars, polls)

        assert clock.now() == datetime(2026, 8, 12, tzinfo=UTC)
        assert clock.now() < datetime(2026, 8, 20, tzinfo=UTC)


class TestTheCliChoosesThePolicy:
    """The live/once distinction lives at the CLI, so the tolerance is chosen there."""

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        seen: dict[str, object] = {}
        real_run = PaperSession.run

        def spy(self: PaperSession, **kwargs: object) -> object:
            seen.update(kwargs)
            return real_run(self, max_new_bars=1, max_empty_polls=1)

        monkeypatch.setattr(WallClock, "sleep_until", lambda self, when: None)
        monkeypatch.setattr(PaperSession, "run", spy)
        return seen

    def _invoke(self, tmp_path: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "paper",
                "--strategy",
                "buy_and_hold",
                "--symbols",
                "AAA",
                "--from",
                "2024-01-02",
                "--to",
                "2024-01-10",
                "--source",
                "synthetic",
                "--out",
                str(tmp_path),
                *extra,
            ],
        )

    def test_live_gets_the_interval_derived_tolerance(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        result = self._invoke(tmp_path, "--live", "--interval", "5m")

        assert result.exit_code == 0, result.output
        assert captured["max_empty_polls"] == 12

    def test_a_daily_live_session_gets_the_floor(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        result = self._invoke(tmp_path, "--live")

        assert result.exit_code == 0, result.output
        assert captured["max_empty_polls"] == MIN_LIVE_EMPTY_POLLS

    def test_once_still_passes_one(self, tmp_path: Path, captured: dict[str, object]) -> None:
        """The hard constraint: ``--once`` keeps its explicit 1."""
        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert captured["max_empty_polls"] == 1

    def test_the_operator_override_wins_on_the_live_path(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        result = self._invoke(tmp_path, "--live", "--interval", "5m", "--max-empty-polls", "99")

        assert result.exit_code == 0, result.output
        assert captured["max_empty_polls"] == 99

    def test_the_override_also_applies_to_a_replay(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        result = self._invoke(tmp_path, "--max-empty-polls", "7")

        assert result.exit_code == 0, result.output
        assert captured["max_empty_polls"] == 7

    def test_a_zero_override_is_refused(self, tmp_path: Path) -> None:
        """Zero would break instantly; it must not be a way to silently do nothing."""
        result = self._invoke(tmp_path, "--max-empty-polls", "0")

        assert result.exit_code == 2
        assert "--max-empty-polls" in result.output

    def test_a_live_session_announces_its_stop_policy(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        """A ~17:00 exit must read as policy, not as a hang — so it says so up front."""
        result = self._invoke(tmp_path, "--live", "--interval", "5m")

        assert result.exit_code == 0, result.output
        assert "Stops after 12 consecutive poll(s)" in result.output
        assert "1 hour of silence at 5m" in result.output

    def test_a_daily_live_session_announces_four_days(
        self, tmp_path: Path, captured: dict[str, object]
    ) -> None:
        result = self._invoke(tmp_path, "--live")

        assert result.exit_code == 0, result.output
        assert "Stops after 4 consecutive poll(s)" in result.output
        assert "4 days of silence at 1d" in result.output

    def test_a_replay_says_nothing_new(self, tmp_path: Path, captured: dict[str, object]) -> None:
        """The announcement is live-only: ``--once`` stdout must not move."""
        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert "Stops after" not in result.output


class TestOnceIsByteIdentical:
    """``--once`` must be untouched by this slice, proved rather than argued.

    The digest is the same golden ADR-0042 pinned from ``origin/main`` at ``dbb845f``:
    a ``--once`` replay's equity curve has not moved since, and must not move here.
    """

    _ARGS: ClassVar[list[str]] = [
        "paper",
        "--strategy",
        "sma_crossover",
        "--symbols",
        "AAA,BBB",
        "--from",
        "2024-01-02",
        "--to",
        "2024-03-01",
        "--source",
        "synthetic",
        "--seed",
        "7",
    ]
    _GOLDEN = "50946899eca0d84d43a65dd096a3a58cd32a1ecad28dc3aff1334bee3f252eaf"

    def test_the_equity_curve_still_matches_the_pre_change_golden(self, tmp_path: Path) -> None:
        result = runner.invoke(app, [*self._ARGS, "--out", str(tmp_path)])

        assert result.exit_code == 0, result.output
        raw = (tmp_path / "equity_curve.csv").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == self._GOLDEN
        assert "Processed 44 completed bar(s)." in result.output
