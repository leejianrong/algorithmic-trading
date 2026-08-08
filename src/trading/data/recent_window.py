"""Recent-window feed for paper mode: only *completed* bars. Slice V5 / ADR-0022.

The core paper-mode risk is acting on a still-forming bar — the latest bar for the
current period is incomplete until that period closes, and trading on it means
deciding from unfinished data. This feed defends against that: it fetches recent
bars from the :class:`~trading.interfaces.DataAdapter`, drops any that are not
yet complete per the clock, and returns the completed cross-section in ascending
timestamp order (reusing the engine's :func:`~trading.engine.build_feed` merge).

Completeness rule (default): a daily bar dated ``D`` is complete once the clock
has moved into a later session than ``D`` — i.e. once ``now``'s UTC date is past
``D``. A bar whose date equals the clock's current, still-forming day is
excluded until the clock crosses into the next day. For sub-daily bars, use
:func:`interval_is_complete`: a bar with START ``ts`` covers ``[ts, ts + interval)``
and is complete once ``now >= ts + interval`` (ADR-0022). The policy is injectable
so a real market calendar can replace either comparison later.

The second risk is a *fetch* that fails. A poll asks the adapter for every symbol
in the universe, and one broken symbol used to take the whole poll — and therefore
the whole live session — down with it (ADR-0032 recorded this as a known gap).
:meth:`RecentWindowFeed.poll` now fetches per symbol inside a guard, exactly as
:func:`trading.engine.load_series` does for a backtest, and reports what it lost
on :attr:`RecentWindowFeed.absent`. A symbol is never permanently dropped: every
poll retries every requested symbol, because a paper session outlives the outage
that broke it (ADR-0035).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from trading.clock import Clock
from trading.engine import (
    REASON_FETCH_FAILED,
    REASON_NO_BARS,
    AbsentSymbol,
    Feed,
    build_feed,
)
from trading.interfaces import DataAdapter
from trading.types import Bar

logger = logging.getLogger(__name__)

# A pluggable completeness test: is ``bar`` finished given the current time?
CompletenessPolicy = Callable[[Bar, datetime], bool]

# Fetch far enough back to cover any reasonable lookback; timezone-aware so it
# compares cleanly against a :class:`~trading.types.Bar`'s tz-aware timestamp.
_FAR_PAST = datetime.min.replace(tzinfo=UTC)

# How many consecutive polls a symbol must be absent from before the feed calls it
# persistent rather than a blip. One failed fetch is a network hiccup; three in a
# row, at whatever cadence the session polls, is a symbol the operator has to look
# at. Escalation changes the *loudness* only — the symbol is still retried forever
# (ADR-0035).
PERSISTENT_ABSENCE_POLLS = 3


def default_is_complete(bar: Bar, now: datetime) -> bool:
    """A daily bar dated ``D`` is complete once ``now`` is on a later UTC date.

    The bar's session (its calendar day) must lie strictly before the clock's
    current day; a bar dated on the clock's own still-forming day is not yet
    complete.
    """
    return now.astimezone(UTC).date() > bar.ts.astimezone(UTC).date()


def interval_is_complete(interval: timedelta) -> CompletenessPolicy:
    """A completeness policy for bars of a fixed ``interval`` (ADR-0022).

    A bar with START ``ts`` covers ``[ts, ts + interval)`` and is complete exactly
    when ``now >= ts + interval`` — the moment the whole window has elapsed. This
    is the sub-daily counterpart to :func:`default_is_complete`; the CLI passes
    ``interval_is_complete(freq.delta)`` to :class:`RecentWindowFeed` for intraday
    paper trading. Comparisons are done in UTC so a naive-vs-aware mix can't slip
    through (a ``Bar.ts`` is always tz-aware).
    """

    def _policy(bar: Bar, now: datetime) -> bool:
        return now.astimezone(UTC) >= bar.ts.astimezone(UTC) + interval

    return _policy


class RecentWindowFeed:
    """Yields only completed recent daily bars, newest ``lookback`` per symbol.

    Wraps a :class:`~trading.interfaces.DataAdapter` and a
    :class:`~trading.clock.Clock`; the injectable ``is_complete`` policy decides
    which of the fetched bars count as finished (default:
    :func:`default_is_complete`).

    Symbols that produce nothing are tolerated and *reported*, never silently
    dropped: :attr:`absent` carries one
    :class:`~trading.engine.AbsentSymbol` per missing symbol from the most recent
    poll, and :attr:`absence_streaks` counts how many consecutive polls each has
    been missing from (ADR-0035).
    """

    def __init__(
        self,
        adapter: DataAdapter,
        clock: Clock,
        is_complete: CompletenessPolicy = default_is_complete,
        *,
        adjusted: bool = False,
    ) -> None:
        self._adapter = adapter
        self._clock = clock
        self._is_complete = is_complete
        # Paper/live trades on RAW actual quotes, not adjusted total-return prices
        # (ADR-0021): the strategy must decide and the book must mark in the same
        # dollars the live broker reconciles from the real account, so this feed
        # defaults to raw. Backtest keeps adjusted (ADR-0008) via its own feed.
        self._adjusted = adjusted
        # What the most recent poll could not get, and for how long (ADR-0035).
        self._absent: list[AbsentSymbol] = []
        self._streaks: dict[str, int] = {}

    @property
    def absent(self) -> list[AbsentSymbol]:
        """Symbols missing from the most recent :meth:`poll`, in request order.

        Empty on a clean poll, and rebuilt from scratch by every poll — a symbol
        that recovers leaves this list on the poll it comes back. Each entry uses
        the same two reason codes as :func:`trading.engine.load_series`
        (:data:`~trading.engine.REASON_NO_BARS` /
        :data:`~trading.engine.REASON_FETCH_FAILED`), so "this ticker has no bars"
        and "we could not ask" stay distinguishable in a report.
        """
        return list(self._absent)

    @property
    def absence_streaks(self) -> dict[str, int]:
        """Per symbol, how many *consecutive* polls it has been absent from.

        A symbol drops out of this mapping the moment it returns bars again, so a
        one-poll blip cannot accumulate into a false persistent absence.
        """
        return dict(self._streaks)

    @property
    def persistently_absent(self) -> list[str]:
        """Symbols absent for at least :data:`PERSISTENT_ABSENCE_POLLS` polls running.

        The session keeps retrying them regardless; this is the "look at this"
        signal, not a quarantine list (ADR-0035).
        """
        return [s for s, n in self._streaks.items() if n >= PERSISTENT_ABSENCE_POLLS]

    def _note_absence(self, symbol: str, reason: str, detail: str) -> AbsentSymbol:
        """Count this poll's absence, log it once per state change, and record it."""
        streak = self._streaks.get(symbol, 0) + 1
        self._streaks[symbol] = streak
        if streak >= PERSISTENT_ABSENCE_POLLS:
            detail = f"{detail}; absent from {streak} consecutive polls"
        # Log on state change only. The structured record below is emitted every
        # poll, so nothing is lost; re-logging a permanently dead ticker on every
        # poll would drown a multi-hour session's log in one repeated line.
        if streak == 1:
            logger.warning("%s dropped from this poll: %s", symbol, detail)
        elif streak == PERSISTENT_ABSENCE_POLLS:
            logger.error(
                "%s is persistently absent (%d consecutive polls) and the session is "
                "trading without it: %s",
                symbol,
                streak,
                detail,
            )
        return AbsentSymbol(symbol=symbol, reason=reason, detail=detail)

    def poll(self, symbols: list[str], lookback: int) -> Feed:
        """Return the last ``lookback`` completed bars per symbol, merged & sorted.

        Fetches recent bars up to the clock's current time, discards any bar the
        completeness policy deems still forming, keeps the newest ``lookback`` per
        symbol, and merges them into one timestamp-ordered cross-section.

        Each symbol is fetched inside its own guard (ADR-0035, mirroring
        :func:`trading.engine.load_series`): a symbol whose lookup raises, or which
        the source has no bars for at all, is left out of the cross-section and
        recorded on :attr:`absent` instead of aborting the poll. That matters more
        here than in a backtest — a backtest that dies gets re-run, while a live
        paper session that dies is a session lost.

        A symbol whose bars are all still *forming* is **not** absent: it fetched
        fine and simply has nothing complete yet, which is the normal state at every
        interval boundary (ADR-0022). Only a failed lookup or a genuinely empty
        source response counts.

        Unlike :meth:`trading.engine.Engine.run`, a poll where *every* symbol is
        absent is not an error: it returns an empty feed, which the paper loop
        already treats as "nothing new yet". A backtest over no data is a lie; a
        single bad poll in a long session is a moment to survive.

        Duplicate symbols collapse to one fetch and request order is preserved.
        ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is never caught.
        """
        now = self._clock.now()
        series: dict[str, list[Bar]] = {}
        absent: list[AbsentSymbol] = []
        seen: set[str] = set()

        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            try:
                bars = self._adapter.get_bars(symbol, _FAR_PAST, now, adjusted=self._adjusted)
            except Exception as exc:  # one bad symbol must never abort the whole poll
                absent.append(
                    self._note_absence(
                        symbol,
                        REASON_FETCH_FAILED,
                        f"data lookup failed ({type(exc).__name__}: {exc})",
                    )
                )
                continue
            if not bars:
                absent.append(
                    self._note_absence(
                        symbol,
                        REASON_NO_BARS,
                        "the source returned no bars — delisted, renamed, or never listed",
                    )
                )
                continue
            if symbol in self._streaks:
                del self._streaks[symbol]
                logger.info("%s is back in the feed", symbol)
            completed = [b for b in bars if self._is_complete(b, now)]
            series[symbol] = completed[-lookback:]

        self._absent = absent
        return build_feed(series)
