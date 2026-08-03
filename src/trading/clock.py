"""Clock seam for the shared engine loop (ADR-0002). Slice V5.

Backtest and paper trading run the *same* loop; only the feed and the clock
differ. In a backtest the feed drives time, so the clock never really waits
(:class:`ImmediateClock`). In paper mode the calendar drives time, so the clock
sleeps until the next completed daily bar is due (:class:`WallClock`). Tests
substitute :class:`FakeClock` to script time deterministically with no real
delay.

The :class:`Clock` protocol lives here (not in :mod:`trading.interfaces`) because
it is V5's own seam; the engine imports it only once paper mode is wired.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A source of the current time plus the ability to wait until a moment.

    Implementations: :class:`WallClock` (real time, paced by the calendar),
    :class:`ImmediateClock` (feed-driven backtests, never waits), and
    :class:`FakeClock` (deterministic tests). All times are timezone-aware UTC.
    """

    def now(self) -> datetime:
        """The current instant, timezone-aware in UTC."""
        ...

    def sleep_until(self, ts: datetime) -> None:
        """Block (or advance) until :attr:`now` is at or past ``ts``."""
        ...


class WallClock:
    """Real wall-clock time, paced by the operating system (paper mode).

    Kept intentionally thin: :meth:`now` reads the OS clock and
    :meth:`sleep_until` waits the remaining real seconds. It must never be
    exercised with a real wait in the fast test layer — tests use
    :class:`FakeClock`.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep_until(self, ts: datetime) -> None:
        remaining = (ts - self.now()).total_seconds()
        if remaining > 0:
            time.sleep(remaining)


class ImmediateClock:
    """A non-waiting clock for feed-driven backtests (ADR-0002).

    The feed already carries every timestamp, so there is nothing to wait for:
    :meth:`sleep_until` is a no-op. :meth:`now` returns the last timestamp the
    caller advanced to, letting backtest code read a coherent "current" time
    without a real clock.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, ts: datetime) -> None:
        """Set the current time to ``ts`` (typically the bar being processed)."""
        self._now = ts

    def sleep_until(self, ts: datetime) -> None:
        """No-op: the feed, not the clock, drives time in a backtest."""


class FakeClock:
    """A deterministic, controllable clock for tests — never really sleeps.

    Constructed with a start time and, optionally, a queue of times that
    successive :meth:`now` calls step through (each call pops the next; once the
    queue drains, :meth:`now` returns the persistent current time). :meth:`advance`
    sets the current time directly. :meth:`sleep_until` advances the current time
    to its target *without* any real delay and records the target in
    :attr:`sleep_calls`, so a test can assert exactly what was waited on.
    """

    def __init__(self, start: datetime, queue: list[datetime] | None = None) -> None:
        self._now = start
        self._queue: list[datetime] = list(queue) if queue is not None else []
        self.sleep_calls: list[datetime] = []

    def now(self) -> datetime:
        if self._queue:
            self._now = self._queue.pop(0)
        return self._now

    def advance(self, ts: datetime) -> None:
        """Set the current time to ``ts`` directly."""
        self._now = ts

    def sleep_until(self, ts: datetime) -> None:
        """Record ``ts`` and jump the current time to it — no real waiting."""
        self.sleep_calls.append(ts)
        self._now = ts
