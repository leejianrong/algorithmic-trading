"""Tape-density screening: a venue-observed bar-coverage floor (KAN-863).

Sibling to :mod:`trading.liquidity`'s ADV screen, and answering a different
question. ``screen_by_adv`` asks "did enough dollars trade" — a measure of
*global* market depth. This module asks "did Alpaca actually print a bar for
every interval a continuous market implies" — a measure of *this venue's own
order flow*, which is not the same thing and does not rank the same way.

Measured directly against the real paper venue on 2026-08-15 (see ADR-0073),
crypto bar coverage tracks Alpaca's own tape, not the coin's global liquidity:
ETH — the second-largest cryptoasset in the world — printed only 47.6% of its
possible 5-minute bars and 12.8% of its possible 1-minute bars over a full day,
while LINK printed 100.3% and 79.6% respectively, and the meme coin BONK
printed 95.5% at 5m. A universe picked by market cap would keep ETH and drop
LINK — exactly backwards for a strategy that trades Alpaca's tape.

**Why this cannot be folded into ``screen_by_adv``.** ADV divides dollars by
bar count; a missing bar there just means one less sample in an average, and a
thin-but-present tape still produces a (small) number. Tape density instead
answers "was there a bar to sample at all" — a structural completeness
question, not a size question — and the two disagree in practice: BTC/USD is
the venue's deepest market by dollar volume and still misses more than a
third of its 1-minute bars.

**Reused rather than hand-rolled (ADR-0054/0056).** "How many bars should a
continuous market produce in this window at this interval" is exactly
:meth:`~trading.calendar.MarketCalendar.periods_per_year`'s question scaled to
the window's own span, so :func:`expected_bar_count` calls it rather than
re-deriving the 24/7 day shape (1440 minutes, 365 days) a third time. The
:class:`~trading.frequency.Frequency` that already carries an interval's
calendar is the input, not a bare ``timedelta`` — a caller cannot silently
score a 24/7 tape on the equity calendar's session shape.

**No look-ahead (ADR-0001), reusing the ADV screen's own guarantee.** Tape
density is measured over :func:`trading.liquidity.formation_window` — the same
function ``screen_by_adv`` uses, spanning a window that ends strictly before
the backtest starts — so a universe decision here cannot see how densely a
symbol will trade during the run itself.

**Point-in-time, not rolling — and noisier day to day than ADV.** Like the ADV
screen, this is a decision taken once, before the run; a symbol whose tape
thins out mid-run stays in the universe (the same limit ADR-0029 already
documents). It is *also* noisier: BTC/USD measured 98.6% coverage at 5m on
2026-08-15 but 100.0% on a quieter day eight days later, and ETH/USD measured
47.6% on 2026-08-15 against 89.6% averaged over the trailing week ending
2026-08-23. A single-day formation window (the default, matching the exact
methodology this module's numbers were validated against) can therefore read
more pessimistically or more optimistically than a symbol's typical behavior;
pass a larger ``formation_days`` to average over more days at the cost of a
larger fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from trading.liquidity import formation_window

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from trading.frequency import Frequency
    from trading.interfaces import DataAdapter
    from trading.types import Bar

# Chosen from the gap in a real measurement (ADR-0073): screening the venue's 32
# non-stablecoin USD pairs at 5m on 2026-08-15 gave a cluster of 19 symbols at or
# above 84.0% and a second, clearly worse cluster starting at 78.5% — this floor
# sits in that gap. At 1m the same floor keeps **zero** of the 32 candidates
# (LINK, the best, measured 79.6%), which is not a bug in the floor: it is the
# measurement this module exists to surface, matching the ticket's own finding
# that fine-interval crypto is not viable on this venue at all.
DEFAULT_MIN_TAPE_DENSITY = 0.80

# One calendar day, matching the exact single-day methodology this module's
# default was validated against (ADR-0073). Short on purpose: unlike ADV, which
# wants a quarter to average out an earnings spike, tape density is a per-symbol
# venue-flow property that can be read from a single day, and a shorter window
# costs a smaller fetch at fine intervals (a week of 1-minute bars for 32 symbols
# is ~322k rows; a day is ~46k). The cost of "short" is documented above: pass a
# larger value to average over more days.
DEFAULT_TAPE_DENSITY_FORMATION_DAYS = 1


def expected_bar_count(window_start: datetime, window_end: datetime, freq: Frequency) -> float:
    """How many ``freq``-length bars a continuous market produces in this window.

    Delegates to :meth:`~trading.calendar.MarketCalendar.periods_per_year`
    scaled by the window's share of a nominal year, rather than re-deriving the
    24/7 day shape — the reuse ADR-0056 already established. Requires a
    continuous calendar (:attr:`~trading.calendar.MarketCalendar.is_continuous`):
    a session market's "expected bars" also depends on which hours are open,
    which ``periods_per_year`` alone does not capture, so silently answering
    that question would be the exact kind of wrong-market arithmetic ADR-0054
    exists to prevent.
    """
    if window_end <= window_start:
        raise ValueError(
            f"window_end must be after window_start, got {window_start} .. {window_end}"
        )
    if not freq.calendar.is_continuous:
        raise ValueError(
            f"expected_bar_count needs a continuous market calendar, got "
            f"{freq.calendar.name!r}; a session market's expected bar count also "
            "depends on which hours are open, which this function does not model"
        )
    span = window_end - window_start
    years = span / timedelta(days=freq.calendar.days_per_year)
    return freq.calendar.periods_per_year(freq.delta) * years


def bar_coverage_ratio(
    bars: Sequence[Bar], window_start: datetime, window_end: datetime, freq: Frequency
) -> float:
    """``len(bars) / expected_bar_count(...)`` — 1.0 is a complete tape.

    Counts every bar it is given rather than re-filtering by timestamp: callers
    are expected to have already fetched exactly ``[window_start, window_end]``
    (:meth:`~trading.interfaces.DataAdapter.get_bars`'s own contract), and
    re-filtering here would silently mask a caller bug instead of over-counting
    visibly.
    """
    expected = expected_bar_count(window_start, window_end, freq)
    return len(bars) / expected


@dataclass(frozen=True, slots=True)
class TapeDensityVerdict:
    """One symbol's screen outcome, carrying the coverage ratio it was judged on.

    ``coverage`` is ``None`` only when the formation window yielded no bars at
    all — reported as :attr:`unverified` rather than as a failure, mirroring
    :class:`~trading.liquidity.LiquidityVerdict`: "no data" and "measured thin"
    are different facts.
    """

    symbol: str
    coverage: float | None
    passed: bool
    reason: str

    @property
    def unverified(self) -> bool:
        """Whether the screen had no data to judge this symbol on."""
        return self.coverage is None


@dataclass(frozen=True, slots=True)
class TapeDensityScreen:
    """The full outcome of screening a candidate universe by tape density.

    Carries the floor, the interval it was measured at, and the exact formation
    span used, so a run's universe decision is reproducible and auditable after
    the fact — the same contract :class:`~trading.liquidity.LiquidityScreen`
    keeps for ADV.
    """

    min_density: float
    interval_label: str
    formation_start: datetime
    formation_end: datetime
    verdicts: tuple[TapeDensityVerdict, ...]

    @property
    def kept(self) -> list[str]:
        """Symbols that met the floor, in the order they were submitted."""
        return [v.symbol for v in self.verdicts if v.passed]

    @property
    def dropped(self) -> list[TapeDensityVerdict]:
        """Verdicts for every symbol that did not make it, reasons included."""
        return [v for v in self.verdicts if not v.passed]

    @property
    def unverified(self) -> list[TapeDensityVerdict]:
        """Verdicts the screen could not judge — no bars in the window."""
        return [v for v in self.verdicts if v.unverified]

    def describe(self) -> str:
        """A human-readable, multi-line report of what was kept and dropped."""
        window = f"{self.formation_start.date()}..{self.formation_end.date()}"
        head = (
            f"Tape-density screen ({self.interval_label}): coverage >= "
            f"{self.min_density:.1%} over {window} (pre-backtest, no look-ahead)"
        )
        lines = [head, f"  kept {len(self.kept)}/{len(self.verdicts)}: {', '.join(self.kept)}"]
        for verdict in self.dropped:
            coverage = "no data" if verdict.coverage is None else f"{verdict.coverage:.1%}"
            lines.append(f"  dropped {verdict.symbol}: {verdict.reason} (coverage {coverage})")
        return "\n".join(lines)


def screen_by_tape_density(
    adapter: DataAdapter,
    symbols: Sequence[str],
    backtest_start: datetime,
    freq: Frequency,
    *,
    min_density: float = DEFAULT_MIN_TAPE_DENSITY,
    formation_days: int = DEFAULT_TAPE_DENSITY_FORMATION_DAYS,
    adjusted: bool = False,
) -> TapeDensityScreen:
    """Screen ``symbols`` by venue tape density measured *before* the backtest.

    Fetches each symbol's bars over :func:`~trading.liquidity.formation_window`
    at ``freq``'s interval, computes :func:`bar_coverage_ratio`, and keeps the
    symbols at or above ``min_density``. Requires ``freq.calendar.is_continuous``
    (see :func:`expected_bar_count`).

    A symbol the adapter has no bars for in that window is marked *unverified*
    and **dropped**, exactly as :func:`~trading.liquidity.screen_by_adv` treats
    an unverifiable symbol — a data lookup failure never aborts the screen.

    ``adjusted`` defaults to ``False``: a crypto pair has no splits or dividends
    (ADR-0058), so raw and adjusted are the same series, and ``False`` matches
    what a paper/live crypto feed actually asks for (ADR-0021).
    """
    if not 0.0 <= min_density <= 1.0:
        raise ValueError(f"min_density must be between 0 and 1, got {min_density}")
    start, end = formation_window(backtest_start, formation_days)

    verdicts: list[TapeDensityVerdict] = []
    for symbol in symbols:
        try:
            bars = adapter.get_bars(symbol, start, end, adjusted=adjusted)
        except Exception as exc:  # one bad ticker must never abort the whole screen
            verdicts.append(
                TapeDensityVerdict(
                    symbol=symbol,
                    coverage=None,
                    passed=False,
                    reason=f"unverified: data lookup failed ({type(exc).__name__})",
                )
            )
            continue
        if not bars:
            verdicts.append(
                TapeDensityVerdict(
                    symbol=symbol,
                    coverage=None,
                    passed=False,
                    reason="unverified: no bars in the formation window",
                )
            )
            continue
        coverage = bar_coverage_ratio(bars, start, end, freq)
        if coverage >= min_density:
            verdicts.append(
                TapeDensityVerdict(symbol=symbol, coverage=coverage, passed=True, reason="ok")
            )
        else:
            verdicts.append(
                TapeDensityVerdict(
                    symbol=symbol,
                    coverage=coverage,
                    passed=False,
                    reason=f"coverage {coverage:.1%} < floor {min_density:.1%}",
                )
            )

    return TapeDensityScreen(
        min_density=min_density,
        interval_label=freq.label,
        formation_start=start,
        formation_end=end,
        verdicts=tuple(verdicts),
    )
