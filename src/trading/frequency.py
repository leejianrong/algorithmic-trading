"""Bar-frequency abstraction: interval length + annualization factor (ADR-0022).

The bench began daily-only (ADR-0005), but ``Bar`` always carried a full tz-aware
timestamp so intraday was never precluded. A :class:`Frequency` names one bar
cadence — a human ``label`` (``"1d"``, ``"1h"``, ``"30m"``, ``"5m"``, ``"1m"``),
the bar length ``delta``, and ``periods_per_year`` (the factor metrics annualize
by). The interval is a property of the *adapter* (set at construction), never an
argument to :meth:`DataAdapter.get_bars`, so the engine and the ``DataAdapter``
protocol are unchanged — the engine just iterates whatever bars the feed yields.

Timestamp convention (ADR-0022): a bar's ``ts`` is its START time; the bar covers
``[ts, ts + delta)`` and is complete at ``ts + delta``.

Annualization. :data:`DAILY` uses ``periods_per_year = 252.0`` to match the
metrics module exactly. Intraday frequencies scale it by the number of bars in a
regular US-equity session — a 6.5-hour / 390-minute cash session (9:30-16:00 ET),
so ``periods_per_year = 252.0 * (390 / interval_minutes)``. That session length is
an explicit modeling assumption: it ignores half-days and the opening/closing
auctions, which is fine for annualizing a synthetic offline series.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

# Trading days per year (matches metrics' 252 basis, Q17) and the length of a
# regular US-equity cash session in minutes (9:30-16:00 ET = 390 minutes).
TRADING_DAYS_PER_YEAR = 252.0
REGULAR_SESSION_MINUTES = 390.0


def _intraday_periods_per_year(delta: timedelta) -> float:
    """Annualization factor for a sub-daily ``delta``: 252 x bars per session.

    ``bars_per_session = 390 / interval_minutes`` (fractional allowed — a 1-hour
    bar is 6.5 bars per 6.5-hour session). See the module docstring for the
    session-length assumption.
    """
    minutes = delta.total_seconds() / 60.0
    if minutes <= 0:
        raise ValueError(f"interval must be positive, got {delta!r}")
    return TRADING_DAYS_PER_YEAR * (REGULAR_SESSION_MINUTES / minutes)


@dataclass(frozen=True, slots=True)
class Frequency:
    """One bar cadence: a label, the bar length, and its annualization factor.

    Instances are compared by value, so two ``Frequency.parse("1h")`` results are
    equal. Construct standard ones via :meth:`parse`; :data:`DAILY` is the
    canonical daily frequency.
    """

    label: str
    delta: timedelta
    periods_per_year: float

    def __post_init__(self) -> None:
        if self.delta <= timedelta(0):
            raise ValueError(f"Frequency.delta must be positive, got {self.delta!r}")
        if self.periods_per_year <= 0:
            raise ValueError(
                f"Frequency.periods_per_year must be positive, got {self.periods_per_year}"
            )

    @property
    def is_intraday(self) -> bool:
        """Whether this bar is shorter than a full day."""
        return self.delta < timedelta(days=1)

    @classmethod
    def parse(cls, label: str) -> Frequency:
        """Resolve a standard label (``"1d"``, ``"1h"``, ``"30m"``, ``"5m"``, ``"1m"``).

        Case- and whitespace-insensitive. An unknown label raises ``ValueError``
        naming the ones we support, rather than silently guessing an interval.
        """
        key = label.strip().lower()
        try:
            return _REGISTRY[key]
        except KeyError:
            known = ", ".join(sorted(_REGISTRY))
            raise ValueError(f"unknown frequency {label!r}; known frequencies: {known}") from None


# The canonical daily frequency. Its 252.0 factor must equal the metrics basis so
# a daily run's annualized numbers are unchanged by this module (ADR-0022).
DAILY = Frequency("1d", timedelta(days=1), TRADING_DAYS_PER_YEAR)

_INTRADAY_DELTAS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "30m": timedelta(minutes=30),
    "5m": timedelta(minutes=5),
    "1m": timedelta(minutes=1),
}

# Standard frequencies, keyed by lowercase label. DAILY is registered explicitly
# so its factor stays exactly 252.0; intraday factors are derived from the session.
_REGISTRY: dict[str, Frequency] = {DAILY.label: DAILY}
for _label, _delta in _INTRADAY_DELTAS.items():
    _REGISTRY[_label] = Frequency(_label, _delta, _intraday_periods_per_year(_delta))

# Public, ordered view of the standard set (daily first, then finer intraday).
STANDARD_FREQUENCIES: tuple[Frequency, ...] = (DAILY, *(_REGISTRY[k] for k in _INTRADAY_DELTAS))
