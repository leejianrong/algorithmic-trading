"""Recent-window feed for paper mode: only *completed* daily bars. Slice V5.

The core paper-mode risk is acting on a still-forming bar — the latest daily bar
for today is incomplete until the session closes, and trading on it means
deciding from unfinished data. This feed defends against that: it fetches recent
bars from the :class:`~trading.interfaces.DataAdapter`, drops any that are not
yet complete per the clock, and returns the completed cross-section in ascending
timestamp order (reusing the engine's :func:`~trading.engine.build_feed` merge).

Completeness rule (default): a daily bar dated ``D`` is complete once the clock
has moved into a later session than ``D`` — i.e. once ``now``'s UTC date is past
``D``. A bar whose date equals the clock's current, still-forming day is
excluded until the clock crosses into the next day. The policy is injectable so a
real market calendar can replace the plain date comparison later.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from trading.clock import Clock
from trading.engine import Feed, build_feed
from trading.interfaces import DataAdapter
from trading.types import Bar

# A pluggable completeness test: is ``bar`` finished given the current time?
CompletenessPolicy = Callable[[Bar, datetime], bool]

# Fetch far enough back to cover any reasonable lookback; timezone-aware so it
# compares cleanly against a :class:`~trading.types.Bar`'s tz-aware timestamp.
_FAR_PAST = datetime.min.replace(tzinfo=UTC)


def default_is_complete(bar: Bar, now: datetime) -> bool:
    """A daily bar dated ``D`` is complete once ``now`` is on a later UTC date.

    The bar's session (its calendar day) must lie strictly before the clock's
    current day; a bar dated on the clock's own still-forming day is not yet
    complete.
    """
    return now.astimezone(UTC).date() > bar.ts.astimezone(UTC).date()


class RecentWindowFeed:
    """Yields only completed recent daily bars, newest ``lookback`` per symbol.

    Wraps a :class:`~trading.interfaces.DataAdapter` and a
    :class:`~trading.clock.Clock`; the injectable ``is_complete`` policy decides
    which of the fetched bars count as finished (default:
    :func:`default_is_complete`).
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

    def poll(self, symbols: list[str], lookback: int) -> Feed:
        """Return the last ``lookback`` completed bars per symbol, merged & sorted.

        Fetches recent bars up to the clock's current time, discards any bar the
        completeness policy deems still forming, keeps the newest ``lookback`` per
        symbol, and merges them into one timestamp-ordered cross-section.
        """
        now = self._clock.now()
        series: dict[str, list[Bar]] = {}
        for symbol in symbols:
            bars = self._adapter.get_bars(symbol, _FAR_PAST, now, adjusted=self._adjusted)
            completed = [b for b in bars if self._is_complete(b, now)]
            series[symbol] = completed[-lookback:]
        return build_feed(series)
