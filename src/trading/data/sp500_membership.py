"""Point-in-time S&P 500 membership, reconstructed from a committed fixture (ADR-0064).

Answers one question `universe.py`'s ``blue20`` cannot: "who was actually in the
S&P 500 on this historical date?" ``blue20`` is 2026 hindsight (ADR-0027) — its
symbols are chosen *because* they are today's mega-caps, so backtesting it over
2015 asks "how would this have done if I had known in 2015 which names would be
giants in 2026", not "how would this have done in 2015". This module answers the
real question, for one specific piece of it: **index membership**, not price.

What this fixes, and what it does not
--------------------------------------
**Fixed:** the *selection* half of survivorship bias. Given a date,
:func:`members_as_of` (or :class:`PointInTimeSP500` for repeated queries) returns
the S&P 500 constituents as they actually stood then — including names that have
since been removed, acquired, or renamed. A backtest universe built this way was
not chosen with hindsight.

**Not fixed, and it cannot be from this fixture alone:** ADR-0027's "mechanism 2"
— yfinance (and every free adapter in this repo) serves price history only for
tickers that are *currently listed*. This module tells you a delisted name such as
a 2015 constituent later acquired was genuinely in the index; it does not, and
cannot, hand you that ticker's price history if the free price provider has since
dropped it. The residual bias is real but is measured to be **smaller than for
small caps**: S&P 500 removals are overwhelmingly acquisitions and index
reshuffles rather than bankruptcies, and an acquired firm's ticker typically keeps
its price history on yfinance right up to the acquisition date (it stops
*listing*, it does not retroactively vanish) — but it is not zero, and this module
does not measure it. `docs/adr/0064-point-in-time-sp500-membership.md` records a
real measurement of how much of a PIT universe actually comes back with usable
price data on `--source yfinance`, for one date range; treat that as one data
point; do not assume some other window behaves the same.

Also out of scope, explicitly (matches the card's rescoping, not an oversight):
Russell 2000, S&P 1500, or any broader index — no free vendor publishes
point-in-time membership for those, and this module makes no attempt to
approximate it.

Fixture, source, and measured coverage
---------------------------------------
The committed fixture (``tests/fixtures/sp500_membership/sp500_changes.csv``,
694 rows, ~17 KB) is a **compressed changes table**: one row per calendar date on
which S&P 500 membership actually differed from the prior row, holding only the
tickers added and removed that date. It is derived, by
``scripts/refresh_sp500_membership.py`` (manual, network, run once and committed
— never at import or test time, per ADR-0040), from
`fja05680/sp500 <https://github.com/fja05680/sp500>`_ (MIT licensed), a
community-maintained dataset built from the Wikipedia "List of S&P 500
companies" changes section plus manual research filling gaps Wikipedia's own
table leaves (its README is explicit that the Wikipedia table alone is
insufficient and describes the manual cross-checking involved). The pulled file
(dated through 2026-06-30 as fetched 2026-08-17) spans **1996-01-02..2026-06-30**.

The card that created this module cited a *different*, thinner secondhand
dataset whose change-rows broke down 1970s=2, 1980s=0, 1990s=8, 2000s=43,
2010s=218 — badly under-covering the 2000s (~80% missing against a real ~20-25
changes/year) — and recommended treating PIT membership as usable only from
about 2010. **That is not what this fixture measures.** Counted directly against
the committed file: 15-42 change-*dates* per year in *every* decade from 1996
onward (not concentrated post-2010), and three independent spot checks against
well-known corporate history all land on the exact right date — TSLA added
2020-12-21, the FB->META ticker change 2022-06-09, and GM re-added 2013-06-07
(its post-bankruptcy re-IPO, correctly distinct from the pre-2009 GM that was
removed). On that basis this module documents its usable floor as **1996-01-02**,
not 2010 — a stronger claim than the card anticipated, made because the actual
source measured differently, per the card's own instruction to trust what is
measured over secondhand summary. The one caveat inherited from the upstream
README rather than smoothed over: the first ~5 years (1996-2000) may be missing a
handful of names the maintainer could not independently verify (487 constituents
in the first row vs. today's ~503-507; the count rises to 494+ by 2001-01-16 and
never falls below that again) — so treat 1996-2000 as *slightly* less complete
than everything after, not as unusable.

A query past the fixture's last change date (2026-06-30 as committed) returns the
membership as of that last change — the fixture's own "now", which drifts stale
exactly like ``tests/fixtures/yfinance_cache/`` does until someone re-runs the
refresh script. It does not mean the real S&P 500 stopped changing.

Usage
-----
::

    from datetime import UTC, datetime
    from trading.data.sp500_membership import PointInTimeSP500

    pit = PointInTimeSP500.from_fixture()
    symbols = pit.members_as_of(datetime(2015, 1, 1, tzinfo=UTC))

``members_as_of`` (the module-level function) is the one-shot equivalent for a
single query; prefer :class:`PointInTimeSP500` when you need more than one date
from the same fixture, since it parses the CSV once and answers each query by
bisection rather than a fresh O(changes) replay.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

# The committed fixture. Resolved relative to this file, not the working
# directory, so the default works regardless of where a script or test is
# invoked from within a checkout (mirrors the dev-only assumption already made
# by tests/integration/test_backtest_real_data.py's FIXTURE_DIR).
DEFAULT_FIXTURE_PATH: Path = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "sp500_membership"
    / "sp500_changes.csv"
)

_HEADER = ("date", "added", "removed")


@dataclass(frozen=True, slots=True)
class MembershipChange:
    """One date on which S&P 500 membership changed, and exactly what changed.

    ``added``/``removed`` are the tickers that entered/left the index *on this
    date relative to the previous change* — not a full snapshot. The very first
    change in a well-formed sequence is the exception: its ``added`` tuple is the
    entire starting membership (there being nothing before it to diff against)
    and its ``removed`` tuple is always empty.
    """

    date: datetime
    added: tuple[str, ...]
    removed: tuple[str, ...]


def load_changes(path: Path | str = DEFAULT_FIXTURE_PATH) -> list[MembershipChange]:
    """Parse the changes-table fixture into ascending :class:`MembershipChange` rows.

    Raises :class:`FileNotFoundError` (naming the refresh script) if the fixture
    is missing, and :class:`ValueError` on a malformed header or row -- the same
    "fail loudly, never fetch instead" discipline as ``csv_adapter.py`` and the
    yfinance cache (ADR-0040).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"No PIT S&P 500 membership fixture at {p}. This is a committed fixture, "
            "not something fetched at runtime (ADR-0040) -- if it is genuinely missing, "
            "run `uv run python scripts/refresh_sp500_membership.py` once (network, "
            "manual) and commit the result."
        )

    changes: list[MembershipChange] = []
    with p.open(newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{p} is empty; expected header {','.join(_HEADER)}") from None
        if tuple(h.strip().lower() for h in header) != _HEADER:
            raise ValueError(f"{p} has unexpected header {header!r}; expected {list(_HEADER)}.")

        for line_no, row in enumerate(reader, start=2):
            if not row:
                continue  # tolerate blank trailing lines
            if len(row) != len(_HEADER):
                raise ValueError(
                    f"{p}:{line_no}: expected {len(_HEADER)} columns "
                    f"({','.join(_HEADER)}), got {len(row)}: {row!r}"
                )
            date_raw, added_raw, removed_raw = row
            try:
                date = datetime.strptime(date_raw.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError as exc:
                raise ValueError(f"{p}:{line_no}: malformed date {date_raw!r} ({exc})") from exc
            added = tuple(t for t in added_raw.split(";") if t)
            removed = tuple(t for t in removed_raw.split(";") if t)
            changes.append(MembershipChange(date=date, added=added, removed=removed))

    for previous, current in pairwise(changes):
        if current.date <= previous.date:
            raise ValueError(
                f"{p}: changes must be strictly ascending by date; "
                f"{current.date.date()} does not follow {previous.date.date()}"
            )
    return changes


class PointInTimeSP500:
    """S&P 500 membership reconstructed from a loaded changes table.

    Precomputes the cumulative membership snapshot *after* each change (once, at
    construction) so a query is a bisection over the change dates rather than an
    O(changes) replay every call -- worth doing here even at 694 rows, since the
    natural caller is a backtest that may ask for a rebalance-frequency sequence
    of dates, not just one.
    """

    def __init__(self, changes: Sequence[MembershipChange]) -> None:
        if not changes:
            raise ValueError("no membership changes given; the fixture must have at least one row")
        for previous, current in pairwise(changes):
            if current.date <= previous.date:
                raise ValueError(
                    "changes must be strictly ascending by date; "
                    f"{current.date.date()} does not follow {previous.date.date()}"
                )

        self._changes: tuple[MembershipChange, ...] = tuple(changes)
        self._dates: tuple[datetime, ...] = tuple(c.date for c in self._changes)

        members: set[str] = set()
        snapshots: list[frozenset[str]] = []
        for change in self._changes:
            members -= set(change.removed)
            members |= set(change.added)
            snapshots.append(frozenset(members))
        self._snapshots: tuple[frozenset[str], ...] = tuple(snapshots)

    @classmethod
    def from_fixture(cls, path: Path | str = DEFAULT_FIXTURE_PATH) -> PointInTimeSP500:
        """Build from the committed fixture (or another path in the same format)."""
        return cls(load_changes(path))

    @property
    def coverage_start(self) -> datetime:
        """The earliest date this fixture can answer for."""
        return self._dates[0]

    @property
    def coverage_end(self) -> datetime:
        """The latest change date in the fixture -- its own "now", which goes
        stale the moment the real index changes again until the fixture is
        refreshed (see the module docstring)."""
        return self._dates[-1]

    def members_as_of(self, date: datetime) -> list[str]:
        """S&P 500 constituents in force on ``date``, sorted.

        Raises :class:`ValueError` if ``date`` predates :attr:`coverage_start` --
        this fixture cannot answer for it, and returning an empty or partial
        answer would be a silent survivorship gap of exactly the kind this module
        exists to close. A ``date`` after :attr:`coverage_end` is answered with
        the membership as of :attr:`coverage_end` (the fixture's last known
        state), not an error -- the caller decides whether that is stale for
        their purpose.
        """
        if date < self._dates[0]:
            raise ValueError(
                f"{date.date()} is before this fixture's earliest coverage "
                f"({self._dates[0].date()}); point-in-time S&P 500 membership before "
                "that date is not available from this source (see ADR-0064 and the "
                "module docstring for what is and is not fixable on free data)."
            )
        idx = bisect_right(self._dates, date) - 1
        return sorted(self._snapshots[idx])


def members_as_of(date: datetime, *, path: Path | str = DEFAULT_FIXTURE_PATH) -> list[str]:
    """One-shot convenience: S&P 500 constituents in force on ``date``.

    Parses the fixture fresh on every call. Prefer
    :meth:`PointInTimeSP500.from_fixture` directly when querying more than one
    date, so the CSV is parsed once.
    """
    return PointInTimeSP500.from_fixture(path).members_as_of(date)
