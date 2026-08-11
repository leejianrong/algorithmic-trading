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

Which rule a market gets is a **choice, not an inheritance** (ADR-0053). The daily
default above is a *session* rule: it asks whether the venue's calendar day has
turned over, which is a coherent question only for a venue that closes. A market
that never closes has no session, so its daily bar is just a rolling 24-hour window
and the instant it closes on is a convention someone has to pick. **The convention
here is UTC midnight** — and it needs no new policy, because
:func:`interval_is_complete` with a one-day interval already expresses exactly that.
For a bar stamped at UTC midnight the two rules agree at *every* instant (swept
minute by minute over three days in ``tests/unit/test_completeness_247.py``). Off
midnight they diverge in one direction only: :func:`default_is_complete` calls the
bar complete **early**, by exactly the stamp's offset from midnight, which for a
24/7 venue means handing a still-forming bar to the strategy. So a continuous
market drops the daily special case rather than adding a policy — every interval,
daily included, gates on ``ts + interval``.

Nothing in this module detects which kind of market a symbol trades on, and it
should not: the completeness rule is an axis of its own, independent of any market
calendar, and joining the two is a later slice's job. Today the CLI selects
``default_is_complete`` for daily and ``interval_is_complete`` for intraday, so a
24/7 daily feed built through the CLI would still inherit the session rule — the
seam is proved and documented here, not yet wired.

The second risk is a *fetch* that fails. A poll asks the adapter for every symbol
in the universe, and one broken symbol used to take the whole poll — and therefore
the whole live session — down with it (ADR-0032 recorded this as a known gap).
:meth:`RecentWindowFeed.poll` now fetches per symbol inside a guard, exactly as
:func:`trading.engine.load_series` does for a backtest, and reports what it lost
on :attr:`RecentWindowFeed.absent`. A symbol is never permanently dropped: every
poll retries every requested symbol, because a paper session outlives the outage
that broke it (ADR-0035).

The third risk is a *request* no provider will answer. This feed used to ask for
``[datetime.min, now]`` — year 1 to now — on the reasoning that a wide net cannot
miss anything. Alpaca answers that with an **empty response** rather than an
error, so every symbol read absent, ADR-0035 recorded a legitimate-looking
``REASON_NO_BARS``, and a live session stopped on ``max_empty_polls`` having
primed nothing and traded nothing (ADR-0047). A poll now asks for a **bounded**
window sized from ``lookback`` and the bar interval — see :func:`fetch_span` —
which is all a poll ever keeps anyway.
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

# --- How far back a poll asks (ADR-0047) -------------------------------------
#
# Converting "``lookback`` bars" into wall-clock time has to pay for the hours and
# days the market is shut, or the window comes back short and the ADR-0042 warmup
# is silently truncated.

# US regular trading hours, 09:30-16:00 ET. Also `synthetic._SESSION_LENGTH`; kept
# as a local constant so this module keeps its single-purpose import list.
#
# Both constants below describe the **US equity calendar** specifically. For a
# market that never closes they are wrong in the safe direction — see the 24/7
# paragraph in `fetch_span` (ADR-0053), which measures by how much.
RTH_SESSION = timedelta(hours=6, minutes=30)

# 365 calendar days hold ~252 trading sessions; weekends and the ~9 market
# holidays a year are the whole difference. Sizing a daily window on `lookback`
# *calendar* days would therefore come back ~30% short.
CALENDAR_DAYS_PER_SESSION = 365.0 / 252.0

# Slack on top of that exact conversion. Four means a window would have to be a
# quarter as dense as a normal market calendar before it truncated a lookback —
# and it costs almost nothing, because at every supported interval it lands near
# 4x`lookback` bars, which is one page from any provider (Alpaca's limit is
# 10,000). Over-asking is cheap here in a way that under-asking is not: a poll
# discards everything past the newest `lookback` bars regardless, while a short
# window quietly shortens the history a strategy warms up on.
WINDOW_SLACK = 4.0

# A floor, so a tiny lookback at a fine interval still spans a weekend.
MIN_FETCH_SPAN = timedelta(days=5)

# The hard floor on a window start. Measured against the live Alpaca paper API on
# 2026-08-09: a 1900-01-01 start returns 1,516 daily AAPL bars while
# ``datetime.min`` returns none, so this is a bound the provider demonstrably
# answers — and no US equity series predates it. It also keeps `now - span` from
# overflowing whatever a caller passes as ``lookback``.
EARLIEST_START = datetime(1900, 1, 1, tzinfo=UTC)

# Cap the computed span before it becomes a timedelta, so an absurd lookback
# clamps to EARLIEST_START instead of raising OverflowError.
_MAX_SPAN_DAYS = 200 * 365


def fetch_span(lookback: int, interval: timedelta) -> timedelta:
    """How far back a poll must reach to contain ``lookback`` bars of ``interval``.

    Sub-daily bars pack into the 6.5-hour regular session, so a 5-minute bar is
    one of ~78 in a day rather than one of 288: the wall-clock span of
    ``lookback x interval`` understates the calendar reach by the ratio of the
    closed day to the open one, and then again by the weekend/holiday ratio. Both
    are paid here, and then :data:`WINDOW_SLACK` on top.

    Worked, for the ``lookback=512`` default: daily -> ~2,967 days (~8 years,
    ~2,050 bars); 1h -> ~456 days; 5m -> ~38 days; 1m -> ~7.6 days. Each lands
    near four times the requested bars, which is one provider page.

    **On a market that never closes both conversions are wrong, and both are wrong
    wide** (ADR-0053, assessed rather than changed). A continuous day holds 24 h of
    bars, not 6.5, and every calendar day is a session, so at ``lookback=512`` the
    span exceeds the ``512 x interval`` a 24/7 source actually needs by **5.79x** at
    1d (2,966 days for 512) and **21.39x** at every sub-daily interval
    (``(24/6.5) x (365/252) x 4``). Wide is the direction this function is
    deliberately tuned toward — a short window silently truncates the ADR-0042
    warmup, an over-wide one costs a fetch — so nothing is adjusted here for 24/7,
    and no market-specific branch is introduced. The one cost worth naming: a 24/7
    sub-daily window holds ~10,953 bars per symbol per poll against ADR-0047's
    "one provider page" reasoning (Alpaca's limit is 10,000), so such a poll becomes
    two pages rather than one. That is a fetch cost, not a correctness problem.
    """
    if interval <= timedelta(0):
        raise ValueError(f"interval must be positive, got {interval!r}")
    if interval < timedelta(days=1):
        bars_per_session = max(RTH_SESSION / interval, 1.0)
    else:
        bars_per_session = timedelta(days=1) / interval
    sessions = max(lookback, 1) / bars_per_session
    days = min(sessions * CALENDAR_DAYS_PER_SESSION * WINDOW_SLACK, _MAX_SPAN_DAYS)
    return max(timedelta(days=days), MIN_FETCH_SPAN)


class IntervalCompleteness:
    """A completeness policy for fixed-``interval`` bars that *states* its interval.

    Callable exactly as ADR-0022 specified (see :func:`interval_is_complete`, the
    factory callers use), and additionally readable: the feed needs to know how
    long a bar is to size its fetch window, and the completeness policy is already
    the object that knows. Asking it beats threading the interval through a second
    constructor argument every caller would have to remember to keep in sync.
    """

    def __init__(self, interval: timedelta) -> None:
        self.interval = interval

    def __call__(self, bar: Bar, now: datetime) -> bool:
        return now.astimezone(UTC) >= bar.ts.astimezone(UTC) + self.interval


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

    **This is a session rule, and it is for a venue that closes** (ADR-0053). It is
    safe for US equities whatever hour the provider stamps a daily bar at, because
    the session ends at 20:00/21:00 UTC and the UTC date always turns over *after*
    that — the rule errs late, never early. It is the wrong rule for a market that
    never closes: there the daily bar is a rolling 24-hour window, and unless the
    provider anchors it at UTC midnight this rule declares it complete early by
    exactly the stamp's offset from midnight (measured: 4 h, 8 h and 13 h stamps
    give 240, 480 and 780 minutes of disagreement). Use
    ``interval_is_complete(timedelta(days=1))`` for a continuous market; on a
    midnight-anchored bar the two are indistinguishable, and off midnight it is the
    one that waits for the window to actually elapse.
    """
    return now.astimezone(UTC).date() > bar.ts.astimezone(UTC).date()


def interval_is_complete(interval: timedelta) -> IntervalCompleteness:
    """A completeness policy for bars of a fixed ``interval`` (ADR-0022).

    A bar with START ``ts`` covers ``[ts, ts + interval)`` and is complete exactly
    when ``now >= ts + interval`` — the moment the whole window has elapsed. This
    is the sub-daily counterpart to :func:`default_is_complete`; the CLI passes
    ``interval_is_complete(freq.delta)`` to :class:`RecentWindowFeed` for intraday
    paper trading. Comparisons are done in UTC so a naive-vs-aware mix can't slip
    through (a ``Bar.ts`` is always tz-aware).

    Returns an :class:`IntervalCompleteness`, which behaves identically to the
    closure this used to return and additionally carries ``interval`` so the feed
    can size its fetch window from it (ADR-0047).

    ``ts + interval`` needs no calendar, so this is **also the daily rule for a
    market that never closes**: ``interval_is_complete(timedelta(days=1))`` is a
    rolling 24-hour window closing at UTC midnight, which is the convention ADR-0053
    picks. No separate 24/7 policy exists, because this one already is it.
    """
    return IntervalCompleteness(interval)


def _policy_interval(policy: CompletenessPolicy) -> timedelta:
    """The bar length ``policy`` implies, for window sizing (ADR-0047).

    A policy that does not state one — :func:`default_is_complete`, or a custom
    calendar policy someone injects later — is read as daily, which is the widest
    (most forgiving) window of the supported set. Erring wide costs a larger fetch;
    erring narrow costs history the strategy needed, so the fallback goes wide. A
    caller who knows better passes ``interval=`` to :class:`RecentWindowFeed`.

    Note that the 24/7 daily policy — ``interval_is_complete(timedelta(days=1))``,
    ADR-0053 — *states* one day, and the fallback reads one day too, so swapping a
    continuous market onto it changes which bars are complete and leaves the window
    the poll requests byte-identical.
    """
    if isinstance(policy, IntervalCompleteness):
        return policy.interval
    return timedelta(days=1)


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
        interval: timedelta | None = None,
    ) -> None:
        self._adapter = adapter
        self._clock = clock
        self._is_complete = is_complete
        # How long one bar is, used only to size the fetch window (ADR-0047). It
        # is *not* a second completeness rule: `is_complete` remains the sole judge
        # of which bars a poll yields, so this cannot change what gets traded.
        self._interval = interval if interval is not None else _policy_interval(is_complete)
        # Paper/live trades on RAW actual quotes, not adjusted total-return prices
        # (ADR-0021): the strategy must decide and the book must mark in the same
        # dollars the live broker reconciles from the real account, so this feed
        # defaults to raw. Backtest keeps adjusted (ADR-0008) via its own feed.
        self._adjusted = adjusted
        # What the most recent poll could not get, and for how long (ADR-0035).
        self._absent: list[AbsentSymbol] = []
        self._streaks: dict[str, int] = {}
        # Whether the *whole* universe was silent on the last poll, so the alarm
        # below fires once per outage instead of once per poll (ADR-0047).
        self._universe_silent = False

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

    def window_start(self, now: datetime, lookback: int) -> datetime:
        """The earliest timestamp this poll will ask for (ADR-0047).

        :func:`fetch_span` back from ``now``, clamped at :data:`EARLIEST_START`.
        Public because the request is the thing that was wrong: a test asserting on
        a bar count would have passed throughout the KAN-714 outage.
        """
        span = fetch_span(lookback, self._interval)
        if now - EARLIEST_START <= span:
            return EARLIEST_START
        return now - span

    def _warn_if_the_whole_universe_went_quiet(
        self, requested: int, absent: list[AbsentSymbol], start: datetime, end: datetime
    ) -> None:
        """Escalate a *universe-wide* clean-but-empty answer (ADR-0047).

        Per symbol, "the source returned no bars" is an ordinary absence and
        ADR-0035 handles it. All of them at once, with no fetch failing, is a
        different claim: twenty mega-caps do not delist on the same poll, so the
        request is the likelier suspect — which is exactly what KAN-714 was, and
        exactly what nothing said out loud for months. Logged once per outage, not
        per poll, on the same state-change discipline as an absence; the per-symbol
        records are untouched, so this makes absence *louder*, never quieter.
        """
        silent = (
            requested > 0
            and len(absent) == requested
            and all(a.reason == REASON_NO_BARS for a in absent)
        )
        if silent and not self._universe_silent:
            logger.error(
                "all %d requested symbols returned no bars for [%s, %s]: a whole "
                "universe going quiet at once is far likelier to be a window this "
                "source will not answer than %d simultaneous delistings — check the "
                "request before believing the absence (ADR-0047)",
                requested,
                start.isoformat(),
                end.isoformat(),
                requested,
            )
        self._universe_silent = silent

    def poll(self, symbols: list[str], lookback: int) -> Feed:
        """Return the last ``lookback`` completed bars per symbol, merged & sorted.

        Fetches recent bars up to the clock's current time, discards any bar the
        completeness policy deems still forming, keeps the newest ``lookback`` per
        symbol, and merges them into one timestamp-ordered cross-section.

        The fetch window is **bounded**: ``[self.window_start(now, lookback), now]``,
        which is :func:`fetch_span` wide (ADR-0047). It used to start at
        ``datetime.min``, and Alpaca answers that with an empty response rather
        than an error — so every symbol read absent, forever. A poll discards
        everything older than the newest ``lookback`` bars anyway, so the unbounded
        request bought nothing; it also made a 1-minute synthetic poll fabricate
        3.7 million bars per symbol.

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
        start = self.window_start(now, lookback)
        series: dict[str, list[Bar]] = {}
        absent: list[AbsentSymbol] = []
        seen: set[str] = set()

        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            try:
                bars = self._adapter.get_bars(symbol, start, now, adjusted=self._adjusted)
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
        self._warn_if_the_whole_universe_went_quiet(len(seen), absent, start, now)
        return build_feed(series)
