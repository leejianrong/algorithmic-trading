"""Bar-frequency abstraction: interval length + annualization factor (ADR-0022).

The bench began daily-only (ADR-0005), but ``Bar`` always carried a full tz-aware
timestamp so intraday was never precluded. A :class:`Frequency` names one bar
cadence — a human ``label`` (``"1d"``, ``"1h"``, ``"30m"``, ``"5m"``, ``"1m"``),
the bar length ``delta``, the ``periods_per_year`` metrics annualize by, and the
:class:`~trading.calendar.MarketCalendar` that factor was derived from. The
interval is a property of the *adapter* (set at construction), never an argument to
:meth:`DataAdapter.get_bars`, so the engine and the ``DataAdapter`` protocol are
unchanged — the engine just iterates whatever bars the feed yields.

Timestamp convention (ADR-0022): a bar's ``ts`` is its START time; the bar covers
``[ts, ts + delta)`` and is complete at ``ts + delta``.

Annualization is a property of the market, not of this module (ADR-0054). The
factor comes from a calendar's ``days_per_year`` and ``minutes_per_day``:
:data:`~trading.calendar.US_EQUITY` is 252 x 390 — a 6.5-hour cash session,
9:30-16:00 ET — so :data:`DAILY` is exactly ``252.0`` and 5-minute bars are
``19_656``, bit-for-bit what this module has always produced. A 24/7 market
(:data:`~trading.calendar.CRYPTO_24_7`, 365 x 1440) gives ``365`` and ``105_120``
for the same two labels.

The default calendar is US equity everywhere, so :meth:`Frequency.parse` keeps its
single-argument call and every existing caller (``cli.py`` included) is untouched.
Another market is opt-in: ``Frequency.parse("5m", calendar=CRYPTO_24_7)`` or
:func:`frequencies_for`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from trading.calendar import US_EQUITY, MarketCalendar

# The US-equity calendar's two numbers, kept as module constants because callers
# import them (``report.py``'s frequency fallback is the only one left; the
# synthetic generator read them until ADR-0056 gave it the calendar directly).
# They are now a view onto US_EQUITY rather than the definition of annualization:
# see ADR-0054.
TRADING_DAYS_PER_YEAR = US_EQUITY.days_per_year
REGULAR_SESSION_MINUTES = US_EQUITY.minutes_per_day


@dataclass(frozen=True, slots=True)
class Frequency:
    """One bar cadence on one market: label, bar length, annualization, calendar.

    Instances are compared by value, so two ``Frequency.parse("1h")`` results are
    equal — and a ``"1h"`` on a 24/7 market is deliberately *not* equal to a
    ``"1h"`` on the equity calendar, because the two annualize differently.
    Construct standard ones via :meth:`parse`; :data:`DAILY` is the canonical daily
    equity frequency.
    """

    label: str
    delta: timedelta
    periods_per_year: float
    calendar: MarketCalendar = US_EQUITY

    def __post_init__(self) -> None:
        if self.delta <= timedelta(0):
            raise ValueError(f"Frequency.delta must be positive, got {self.delta!r}")
        if self.periods_per_year <= 0:
            raise ValueError(
                f"Frequency.periods_per_year must be positive, got {self.periods_per_year}"
            )

    @property
    def is_intraday(self) -> bool:
        """Whether this bar is shorter than a full day.

        A property of the *bar*, not of the market: a daily bar on a 24/7 market is
        still not intraday.
        """
        return self.delta < timedelta(days=1)

    @classmethod
    def parse(cls, label: str, *, calendar: MarketCalendar = US_EQUITY) -> Frequency:
        """Resolve a standard label (``"1d"``, ``"1h"``, ``"30m"``, ``"5m"``, ``"1m"``).

        Case- and whitespace-insensitive. An unknown label raises ``ValueError``
        naming the ones we support, rather than silently guessing an interval.

        ``calendar`` is keyword-only and defaults to US equity, so the one-argument
        call every existing caller makes returns exactly the frequency it always
        did. Pass another calendar to annualize on another market (ADR-0054).
        """
        key = label.strip().lower()
        try:
            return _registry(calendar)[key]
        except KeyError:
            known = ", ".join(sorted(_STANDARD_DELTAS))
            raise ValueError(f"unknown frequency {label!r}; known frequencies: {known}") from None


# The standard cadences, in order (daily first, then finer intraday). One table for
# every market: the interval is a duration, only the annualization is per-calendar.
_STANDARD_DELTAS: dict[str, timedelta] = {
    "1d": timedelta(days=1),
    "1h": timedelta(hours=1),
    "30m": timedelta(minutes=30),
    "5m": timedelta(minutes=5),
    "1m": timedelta(minutes=1),
}

# Built registries, keyed by calendar (a frozen value, so hashable). Memoized
# because a Frequency is immutable and building one is pure arithmetic.
_REGISTRIES: dict[MarketCalendar, dict[str, Frequency]] = {}


def _registry(calendar: MarketCalendar) -> dict[str, Frequency]:
    """The standard label -> :class:`Frequency` map for one market."""
    cached = _REGISTRIES.get(calendar)
    if cached is None:
        cached = {
            label: Frequency(label, delta, calendar.periods_per_year(delta), calendar)
            for label, delta in _STANDARD_DELTAS.items()
        }
        _REGISTRIES[calendar] = cached
    return cached


def frequencies_for(calendar: MarketCalendar) -> tuple[Frequency, ...]:
    """The standard set on ``calendar``, daily first then finer intraday."""
    registry = _registry(calendar)
    return tuple(registry[label] for label in _STANDARD_DELTAS)


# The canonical daily frequency: US equity, 252.0. That factor must equal the
# metrics basis so a daily run's annualized numbers are unchanged (ADR-0022).
DAILY = _registry(US_EQUITY)["1d"]

# Public, ordered view of the standard US-equity set.
STANDARD_FREQUENCIES: tuple[Frequency, ...] = frequencies_for(US_EQUITY)
