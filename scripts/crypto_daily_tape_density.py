#!/usr/bin/env python3
"""Measure Alpaca's *daily* crypto bar coverage -- the check ADR-0073 never ran (KAN-1078).

ADR-0073 (`src/trading/tape_density.py`) measured `crypto10`'s tape-density at 5m
and 1m only: 4/10 symbols fail the default 0.80 coverage floor at 5m, 10/10 fail at
1m. `docs/crypto-research-pass-2026-09-02.md` §2 named the resulting gap explicitly
before scoping KAN-1079 on it: does Alpaca's *daily* crypto tape have the same
holes, or do daily bars aggregate over whatever a coin's tape skipped intraday and
come out complete anyway? Nobody had checked -- this script is that check, run once
for KAN-1078 and left here so KAN-1079 (or any later crypto pass) can re-run it for
whatever range it actually settles on, rather than trusting a stale, one-off answer
frozen into a doc (see `docs/crypto-daily-tape-density-2026-09-09.md` for the result
of the first run).

Read-only: two `AlpacaAdapter.get_bars` calls per symbol (a fixed-window fetch, then
a since-first-bar re-check), nothing submitted, nothing cancelled. Needs the `alpaca`
extra and `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` (paper credentials are enough --
this only reads market data, never the account).

Reuses `trading.tape_density.expected_bar_count` / `bar_coverage_ratio` -- the exact
arithmetic ADR-0073 already built and validated, just handed a `1d` `Frequency`
instead of `5m`/`1m`. No new coverage math; this module's own contribution is the
late-listing-vs-genuine-hole split (`coverage_since_first_bar` below), which
`screen_by_tape_density` does not need for its own purpose (a symbol screen just
wants one pass/fail number) but this diagnostic does, to answer "is a low headline
number a real hole or just a coin that listed after the window started".

Usage::

    uv run --env-file .env python scripts/crypto_daily_tape_density.py
    uv run --env-file .env python scripts/crypto_daily_tape_density.py \\
        --universe crypto10 --start 2021-01-01 --end 2026-09-08
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from trading.calendar import CRYPTO_24_7
from trading.frequency import Frequency
from trading.tape_density import bar_coverage_ratio, expected_bar_count

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trading.interfaces import DataAdapter
    from trading.types import Bar

# A symbol whose first bar lands more than this far past the requested window start
# is "listed late", not "has a hole at the very start" -- one bar interval of slack
# for whatever rounding the venue's own timestamp does at the boundary. At `1d` this
# is one calendar day; the AAVE/AVAX cases this script exists to distinguish (listed
# 2021-07-15 / 2021-11-18 against a 2021-01-01 window) are months late, far past any
# rounding noise this threshold could mask.
LATE_LISTING_SLACK = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class SymbolTapeDensity:
    """One symbol's daily coverage, split into "over the fixed window" and "since
    this symbol's own first bar" -- the split that tells a late listing apart from
    a genuine hole. Pure data: nothing here does I/O.
    """

    symbol: str
    bar_count: int
    first_ts: datetime | None
    last_ts: datetime | None
    coverage_full_window: float | None
    coverage_since_first_bar: float | None
    listed_late: bool

    @property
    def unverified(self) -> bool:
        """No bars at all in the requested window."""
        return self.bar_count == 0


def summarize_coverage(
    symbol: str,
    bars: Sequence[Bar],
    window_start: datetime,
    window_end: datetime,
    freq: Frequency,
) -> SymbolTapeDensity:
    """Score one symbol's already-fetched bars two ways, with no network access.

    ``coverage_full_window`` is `bar_coverage_ratio` over the requested
    ``[window_start, window_end]`` exactly as ADR-0073 scores a symbol screen --
    penalizes a late listing along with a genuine hole, which is the number that
    makes AAVE/AVAX look ~85-91% complete when they are actually 100.1% complete
    since they started trading.

    ``coverage_since_first_bar`` reruns the same arithmetic (`bar_coverage_ratio`,
    not reimplemented) with the window re-anchored at this symbol's own first bar,
    which is what separates "not listed yet" from "was listed and dropped bars".
    ``None`` when there are no bars to anchor on.

    ``listed_late`` is `True` when the first bar arrives more than
    `LATE_LISTING_SLACK` after ``window_start`` -- the signal that a low
    ``coverage_full_window`` number is a listing-date artifact rather than a real
    gap, to be read *alongside* ``coverage_since_first_bar`` rather than instead of
    it (a symbol can be both late-listed *and* leaky since listing).
    """
    if not bars:
        return SymbolTapeDensity(
            symbol=symbol,
            bar_count=0,
            first_ts=None,
            last_ts=None,
            coverage_full_window=None,
            coverage_since_first_bar=None,
            listed_late=False,
        )
    ordered = sorted(bars, key=lambda b: b.ts)
    first_ts, last_ts = ordered[0].ts, ordered[-1].ts
    coverage_full_window = bar_coverage_ratio(ordered, window_start, window_end, freq)
    listed_late = first_ts > window_start + LATE_LISTING_SLACK
    coverage_since_first_bar = (
        bar_coverage_ratio(ordered, first_ts, window_end, freq) if first_ts < window_end else None
    )
    return SymbolTapeDensity(
        symbol=symbol,
        bar_count=len(ordered),
        first_ts=first_ts,
        last_ts=last_ts,
        coverage_full_window=coverage_full_window,
        coverage_since_first_bar=coverage_since_first_bar,
        listed_late=listed_late,
    )


def fetch_and_summarize(
    adapter: DataAdapter,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    freq: Frequency,
) -> SymbolTapeDensity:
    """`adapter.get_bars` for one symbol, then `summarize_coverage` on the result.

    A crypto pair has no splits or dividends (ADR-0058), so raw and adjusted are
    the same series; ``adjusted=False`` matches what a paper/live crypto feed
    actually asks for (ADR-0021) and costs nothing extra to verify (ADR-0045).
    """
    bars = adapter.get_bars(symbol, window_start, window_end, adjusted=False)
    return summarize_coverage(symbol, bars, window_start, window_end, freq)


def _parse_date(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def _format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _format_ts(value: datetime | None) -> str:
    return "n/a" if value is None else value.date().isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--universe",
        default="crypto10",
        help="Curated basket name to screen (default: crypto10).",
    )
    parser.add_argument(
        "--start",
        type=_parse_date,
        default=datetime(2021, 1, 1, tzinfo=UTC),
        help="Window start, YYYY-MM-DD (default: 2021-01-01, Alpaca's crypto tape start).",
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=None,
        help="Window end, YYYY-MM-DD (default: yesterday, to avoid today's forming bar).",
    )
    args = parser.parse_args()
    window_end = args.end or (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    window_start = args.start

    from trading.data.alpaca_adapter import AlpacaAdapter
    from trading.universe import get_universe

    freq = Frequency.parse("1d", calendar=CRYPTO_24_7)
    adapter = AlpacaAdapter(calendar=CRYPTO_24_7)
    symbols = get_universe(args.universe)
    expected = expected_bar_count(window_start, window_end, freq)

    print("Daily crypto tape-density check (KAN-1078) -- read-only, market-data GETs only")
    print()
    print(f"Universe        {args.universe} ({len(symbols)} symbols)")
    print(f"Window          {window_start.date()} .. {window_end.date()}")
    print(f"Expected bars   {expected:.2f} (CRYPTO_24_7, 365 bars/year daily)")
    print()
    header = (
        f"  {'symbol':10s} {'bars':>6s} {'full-window':>12s} {'since-listing':>14s} "
        f"{'late?':>6s} {'first':>11s} {'last':>11s}"
    )
    print(header)

    results: list[SymbolTapeDensity] = []
    for symbol in symbols:
        result = fetch_and_summarize(adapter, symbol, window_start, window_end, freq)
        results.append(result)
        late = "yes" if result.listed_late else "no"
        print(
            f"  {result.symbol:10s} {result.bar_count:6d} "
            f"{_format_pct(result.coverage_full_window):>12s} "
            f"{_format_pct(result.coverage_since_first_bar):>14s} "
            f"{late:>6s} {_format_ts(result.first_ts):>11s} {_format_ts(result.last_ts):>11s}"
        )

    verified = [r for r in results if not r.unverified]
    if verified:
        mean_full = sum(r.coverage_full_window or 0.0 for r in verified) / len(verified)
        since = [r.coverage_since_first_bar for r in verified if r.coverage_since_first_bar]
        mean_since = sum(since) / len(since) if since else None
        print()
        print(f"Mean coverage (full window):    {mean_full:.1%} over {len(verified)} symbols")
        print(
            f"Mean coverage (since listing):   {_format_pct(mean_since)} over {len(since)} symbols"
        )
        late_listed = [r.symbol for r in results if r.listed_late]
        if late_listed:
            print(f"Late-listed (not a hole):       {', '.join(late_listed)}")
        genuine_holes = [
            r.symbol
            for r in results
            if not r.listed_late
            and r.coverage_full_window is not None
            and r.coverage_full_window < 0.95
        ]
        if genuine_holes:
            print(f"Genuine daily-level gaps:       {', '.join(genuine_holes)}")
    unverified = [r.symbol for r in results if r.unverified]
    if unverified:
        print(f"Unverified (no bars at all):     {', '.join(unverified)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
