"""Market calendars: how many bars a year a market actually produces (ADR-0054).

Annualization used to be two module constants in :mod:`trading.frequency` —
``TRADING_DAYS_PER_YEAR = 252`` and ``REGULAR_SESSION_MINUTES = 390`` — which is
the US-equity cash session written into the arithmetic rather than into a value.
That is correct for the only market the bench has ever traded and wrong for every
other one. A 24/7 market trades 365 days x 1440 minutes, so a crypto Sharpe
annualized on the equity calendar is out by ``sqrt(365/252) = 1.20x`` daily and by
``sqrt(105_120/19_656) = 2.31x`` at 5-minute bars.

The direction is worth being precise about, because it is not uniform. The equity
factor is the *smaller* one, so every annualized figure comes out smaller than the
truth: a profitable strategy is **understated** (conservative), while a **losing**
one is flattered — measured on a real 5m run, a -3.73% month scores Sharpe -8.34 on
252 x 390 against -19.28 on 365 x 1440. And total return and max drawdown do not
scale by ``periods_per_year`` at all, so a mis-annualized report puts an honest
drawdown next to a Sharpe and a Calmar from another market's year. See ADR-0054.

A :class:`MarketCalendar` is a frozen value with two numbers and a name;
:mod:`trading.frequency` derives ``periods_per_year`` from it. :data:`US_EQUITY` is
the former constants exactly (252 x 390), so every equity run is byte-identical;
:data:`CRYPTO_24_7` is 365 x 1440.

Two derivations, one rule. A **sub-daily** bar is one of
``minutes_per_day / interval_minutes`` bars in a trading day, so
``periods_per_year = days_per_year * (minutes_per_day / interval_minutes)``. A bar
of **one day or longer** covers a whole trading day however long that day's session
is, so it is ``days_per_year / days_per_bar`` — which is why a daily equity bar is
252 and not ``252 * (390/1440)``. On a continuous market the two agree at 1440
minutes (365 x 1440 one-minute bars = 525,600 = 365 daily bars x 1440), and on a
session market they deliberately do not.

Both calendars are **nominal**, not real: no holidays, no half-days, no leap years
(365 rather than 365.25 is a 0.07% understatement of a crypto year, far below the
error this module exists to remove), and no exchange maintenance window. A real
trading calendar is KAN-687 and needs a provider dependency; this module is the
seam it would slot behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

# A calendar day, in minutes: the ceiling on tradable minutes per day, and what a
# continuous market actually trades.
MINUTES_PER_CALENDAR_DAY = 1440.0

# The ceiling on trading days per year. 366 rather than 365 so a calendar that
# wants to model leap years is not rejected.
MAX_DAYS_PER_YEAR = 366.0

# A year of continuous trading, in days. The threshold `is_continuous` compares
# against, and CRYPTO_24_7's own figure.
CALENDAR_DAYS_PER_YEAR = 365.0

_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class MarketCalendar:
    """How much a market trades: ``days_per_year`` x ``minutes_per_day``.

    ``name`` is the registry key and what a report prints; the two numbers are
    nominal (see the module docstring). Instances are frozen and compared by value,
    so a :class:`~trading.frequency.Frequency` can carry one and two frequencies
    with the same label on different markets are correctly *unequal*.
    """

    name: str
    days_per_year: float
    minutes_per_day: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("MarketCalendar.name must not be blank")
        if self.days_per_year <= 0:
            raise ValueError(
                f"MarketCalendar.days_per_year must be positive, got {self.days_per_year}"
            )
        if self.days_per_year > MAX_DAYS_PER_YEAR:
            raise ValueError(
                f"MarketCalendar.days_per_year cannot exceed {MAX_DAYS_PER_YEAR}, "
                f"got {self.days_per_year}"
            )
        if self.minutes_per_day <= 0:
            raise ValueError(
                f"MarketCalendar.minutes_per_day must be positive, got {self.minutes_per_day}"
            )
        if self.minutes_per_day > MINUTES_PER_CALENDAR_DAY:
            raise ValueError(
                f"MarketCalendar.minutes_per_day cannot exceed {MINUTES_PER_CALENDAR_DAY}, "
                f"got {self.minutes_per_day}"
            )

    @property
    def is_continuous(self) -> bool:
        """Whether the market trades every minute of every day (24/7)."""
        return (
            self.minutes_per_day == MINUTES_PER_CALENDAR_DAY
            and self.days_per_year >= CALENDAR_DAYS_PER_YEAR
        )

    def periods_per_year(self, interval: timedelta) -> float:
        """Bars of length ``interval`` this market produces in a nominal year.

        Sub-daily: ``days_per_year * (minutes_per_day / interval_minutes)`` — the
        expression order is deliberate and must not be rearranged, because the
        equity result has to stay bit-for-bit what the old
        ``TRADING_DAYS_PER_YEAR * (REGULAR_SESSION_MINUTES / minutes)`` produced.

        One day or longer: ``days_per_year / days_per_bar``, so a daily bar is one
        trading day's worth however short the session (equity daily is exactly
        252.0, not ``252 * 390/1440``).
        """
        minutes = interval.total_seconds() / 60.0
        if minutes <= 0:
            raise ValueError(f"interval must be positive, got {interval!r}")
        if interval >= _ONE_DAY:
            return self.days_per_year / (minutes / MINUTES_PER_CALENDAR_DAY)
        return self.days_per_year * (self.minutes_per_day / minutes)


# The bench's original and default market: a 252-day year of 6.5-hour
# (390-minute) regular cash sessions, 9:30-16:00 ET. These two numbers ARE the
# former frequency.py constants; changing them changes every historical figure.
US_EQUITY = MarketCalendar("us_equity", 252.0, 390.0)

# A continuously-traded market: every day of the year, every minute of the day.
CRYPTO_24_7 = MarketCalendar("crypto_24_7", CALENDAR_DAYS_PER_YEAR, MINUTES_PER_CALENDAR_DAY)

# Named calendars, keyed by lowercase name.
CALENDARS: dict[str, MarketCalendar] = {cal.name: cal for cal in (US_EQUITY, CRYPTO_24_7)}


def get_calendar(name: str) -> MarketCalendar:
    """Resolve a named calendar (``"us_equity"``, ``"crypto_24_7"``).

    Case- and whitespace-insensitive. An unknown name raises ``ValueError`` naming
    the ones we have, rather than falling back to the equity calendar: silently
    annualizing a 24/7 market on 252 x 390 is exactly the defect this module
    exists to remove.
    """
    key = name.strip().lower()
    try:
        return CALENDARS[key]
    except KeyError:
        known = ", ".join(sorted(CALENDARS))
        raise ValueError(f"unknown market calendar {name!r}; known calendars: {known}") from None
