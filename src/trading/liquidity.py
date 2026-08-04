"""Liquidity screening: an average-dollar-volume floor on the candidate universe.

Every adapter in this bench already parses ``volume`` onto each
:class:`~trading.types.Bar` and then nothing reads it. This module is what reads
it. A technical signal on a $500bn mega-cap and the same signal on a thinly traded
micro-cap are not the same trade: the second one moves the market against you, so
a backtest that fills it at the next open ± a few basis points is quietly lying.
Screening the universe by average dollar volume (ADV) keeps the bench honest about
what it could actually have traded.

**The look-ahead hazard, and why the formation window exists.** The obvious
implementation — compute each symbol's ADV over the backtest range and keep the
liquid ones — is *look-ahead bias wearing a hard hat*. It selects symbols using
volume data from days the strategy has not reached yet, which is exactly what
ADR-0001 forbids everywhere else in the bench. So the screen never touches the
backtest range: :func:`formation_window` returns a span that ends strictly
*before* the first backtest bar, and :func:`screen_by_adv` fetches bars only from
that span. A symbol qualifies on what was knowable at the start line.

That makes the screen a *point-in-time* decision taken once, before the run. It is
deliberately not a per-bar rolling filter: a symbol whose liquidity dries up
mid-run stays in the universe. Re-screening per bar would be more faithful and is
a larger change (it needs a rolling, per-bar universe the engine does not model);
this module's docstrings and ADR-0029 record that limit rather than hiding it.

Nothing here is stateful, random, or clock-dependent: given the same adapter and
the same window, a screen is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from trading.interfaces import DataAdapter
    from trading.types import Bar

# A $20M/day dollar-volume floor: comfortably tradable for a small account
# without the order being a meaningful share of the day's flow. A starting point,
# not a law — pass an explicit floor to suit the account size (ADR-0029).
DEFAULT_MIN_ADV = 20_000_000.0

# How many calendar days of history the screen looks at, ending before the
# backtest starts. ~90 calendar days is roughly a quarter (~60 trading days):
# long enough to average out a single earnings-day volume spike, short enough to
# still describe the symbol's *current* liquidity.
DEFAULT_FORMATION_DAYS = 90


def average_dollar_volume(bars: Sequence[Bar]) -> float:
    """Mean ``close x volume`` across ``bars`` (0.0 for an empty series).

    Dollar volume rather than share volume, because share counts are not
    comparable across symbols: a million shares of a $3 stock and a million
    shares of a $300 stock are different markets. Uses each bar's own close, so
    the figure is in the same (adjusted, ADR-0008) dollars as the rest of a
    backtest.
    """
    if not bars:
        return 0.0
    return sum(bar.close * bar.volume for bar in bars) / len(bars)


def formation_window(
    backtest_start: datetime,
    formation_days: int = DEFAULT_FORMATION_DAYS,
) -> tuple[datetime, datetime]:
    """The ``(start, end)`` span the screen may look at, ending before the run.

    ``end`` is ``backtest_start - 1 day``, so the whole window lies strictly
    before the backtest's first bar and the screen cannot see a single bar the
    strategy will trade on (ADR-0001). Excluding the full day rather than shaving
    a microsecond keeps the guarantee independent of how a given adapter stamps
    its bars — a bar timestamped at the session open on ``backtest_start`` is
    still excluded.

    Raises ``ValueError`` for a non-positive ``formation_days``, since an empty
    window would silently pass or fail every symbol depending on the floor.
    """
    if formation_days <= 0:
        raise ValueError(f"formation_days must be positive, got {formation_days}")
    end = backtest_start - timedelta(days=1)
    return end - timedelta(days=formation_days), end


@dataclass(frozen=True, slots=True)
class LiquidityVerdict:
    """One symbol's screen outcome, carrying the number it was judged on.

    ``adv`` is ``None`` only when the formation window yielded no bars at all —
    reported as :attr:`unverified` rather than as a failure, because "the data
    source had nothing for this symbol" and "this symbol is too thin to trade"
    are different facts and must not be conflated.
    """

    symbol: str
    adv: float | None
    passed: bool
    reason: str

    @property
    def unverified(self) -> bool:
        """Whether the screen had no data to judge this symbol on."""
        return self.adv is None


@dataclass(frozen=True, slots=True)
class LiquidityScreen:
    """The full outcome of screening a candidate universe by ADV.

    Carries the floor and the exact formation span it used, so a run's universe
    decision is reproducible and auditable after the fact.
    """

    min_adv: float
    formation_start: datetime
    formation_end: datetime
    verdicts: tuple[LiquidityVerdict, ...]

    @property
    def kept(self) -> list[str]:
        """Symbols that met the floor, in the order they were submitted."""
        return [v.symbol for v in self.verdicts if v.passed]

    @property
    def dropped(self) -> list[LiquidityVerdict]:
        """Verdicts for every symbol that did not make it, reasons included."""
        return [v for v in self.verdicts if not v.passed]

    @property
    def unverified(self) -> list[LiquidityVerdict]:
        """Verdicts the screen could not judge — no bars in the window."""
        return [v for v in self.verdicts if v.unverified]

    def describe(self) -> str:
        """A human-readable, multi-line report of what was kept and dropped.

        Every dropped symbol is named with its reason. A screen that silently
        shrank the universe would be indistinguishable from a typo in
        ``--symbols``, so the caller is always given something to print.
        """
        window = f"{self.formation_start.date()}..{self.formation_end.date()}"
        head = (
            f"Liquidity screen: ADV >= ${self.min_adv:,.0f} "
            f"over {window} (pre-backtest, no look-ahead)"
        )
        lines = [head, f"  kept {len(self.kept)}/{len(self.verdicts)}: {', '.join(self.kept)}"]
        for verdict in self.dropped:
            adv = "no data" if verdict.adv is None else f"${verdict.adv:,.0f}"
            lines.append(f"  dropped {verdict.symbol}: {verdict.reason} (ADV {adv})")
        return "\n".join(lines)


def screen_by_adv(
    adapter: DataAdapter,
    symbols: Sequence[str],
    backtest_start: datetime,
    *,
    min_adv: float = DEFAULT_MIN_ADV,
    formation_days: int = DEFAULT_FORMATION_DAYS,
    adjusted: bool = True,
) -> LiquidityScreen:
    """Screen ``symbols`` by average dollar volume measured *before* the backtest.

    Fetches each symbol's bars over :func:`formation_window` — a span ending the
    day before ``backtest_start`` — computes :func:`average_dollar_volume`, and
    keeps the symbols at or above ``min_adv``.

    A symbol the adapter has no bars for in that window is marked *unverified*
    and **dropped**, with a reason distinguishing it from a genuine liquidity
    failure. Dropping is the conservative choice: an unverifiable symbol in a
    live universe is a symbol you cannot size a real order in. An adapter that
    raises for an unknown symbol is treated the same way, so one bad ticker never
    aborts the screen.

    ``adjusted`` is forwarded to the adapter to match the run's price policy
    (ADR-0021); the default matches a backtest's adjusted series (ADR-0008).
    """
    if min_adv < 0:
        raise ValueError(f"min_adv must be non-negative, got {min_adv}")
    start, end = formation_window(backtest_start, formation_days)

    verdicts: list[LiquidityVerdict] = []
    for symbol in symbols:
        try:
            bars = adapter.get_bars(symbol, start, end, adjusted=adjusted)
        except Exception as exc:  # one bad ticker must never abort the whole screen
            verdicts.append(
                LiquidityVerdict(
                    symbol=symbol,
                    adv=None,
                    passed=False,
                    reason=f"unverified: data lookup failed ({type(exc).__name__})",
                )
            )
            continue
        if not bars:
            verdicts.append(
                LiquidityVerdict(
                    symbol=symbol,
                    adv=None,
                    passed=False,
                    reason="unverified: no bars in the formation window",
                )
            )
            continue
        adv = average_dollar_volume(bars)
        if adv >= min_adv:
            verdicts.append(LiquidityVerdict(symbol=symbol, adv=adv, passed=True, reason="ok"))
        else:
            verdicts.append(
                LiquidityVerdict(
                    symbol=symbol,
                    adv=adv,
                    passed=False,
                    reason=f"ADV ${adv:,.0f} < floor ${min_adv:,.0f}",
                )
            )

    return LiquidityScreen(
        min_adv=min_adv,
        formation_start=start,
        formation_end=end,
        verdicts=tuple(verdicts),
    )
