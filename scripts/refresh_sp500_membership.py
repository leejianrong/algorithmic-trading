#!/usr/bin/env python3
"""Refresh the committed point-in-time S&P 500 membership fixture (KAN-631).

Manual, occasional, and network-only — never imported by ``src/trading`` and
never run by any test. Per ADR-0040, the required CI job may not leave the
machine, so this script exists precisely so that fetch never has to happen at
test time or import time: run it once, by hand, and commit the CSV it writes.

Source
------
`fja05680/sp500 <https://github.com/fja05680/sp500>`_ (MIT licensed), which
derives point-in-time S&P 500 constituent snapshots from the Wikipedia "List of
S&P 500 companies" changes section plus a manually-researched supplement (its
README explains why: Wikipedia's changes table is not itself complete, so the
maintainer cross-checks and fills gaps by hand). We fetch
``S&P 500 Historical Components & Changes (Updated).csv`` — one row per
*calendar date on which the file's snapshot changed*, each row holding the full
comma-separated ticker list in force as of that date, from 1996-01-02 forward.

That upstream file is ~5.5 MB of near-daily *full snapshots* (mostly duplicate
rows, since membership does not change every day). This script compresses it
into a **changes table**: one row per date the membership actually differs from
the previous row, storing only the added/removed tickers, matching the
`Basket`/fixture spirit of "commit the minimum that reconstructs the rest"
already used for ``tests/fixtures/yfinance_cache/``. Measured on the
2026-06-30-dated pull: 2,718 raw daily rows compress to 694 change rows, ~17 KB.

Verify before you trust it (do not skip this on a refresh)
------------------------------------------------------------
Measured against this exact pull, 2026-08-17 (see ``docs/adr/0064-*.md`` for the
full derivation): 694 change-dates spanning 1996-01-02..2026-06-30, roughly
15-40 change-dates/year in every decade from the 1990s on (not concentrated in
the 2010s the way a thinner dataset can be), and three independent spot checks
against known corporate history all landed on the exact right date --
TSLA added 2020-12-21, FB->META ticker change 2022-06-09, GM re-added
2013-06-07 (post-bankruptcy re-IPO). Cross-check any refresh the same way
before trusting it; a silently thinner or shifted refresh would be worse than
the stale fixture it replaced.

Usage
-----
::

    uv run python scripts/refresh_sp500_membership.py

Writes ``tests/fixtures/sp500_membership/sp500_changes.csv``, overwriting the
committed fixture. Review the diff (a source correction upstream, e.g. a
backfilled 1990s gap, should show as a small diff; a wholesale reshuffle is a
signal something is wrong) before committing.
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "sp500_membership"
    / "sp500_changes.csv"
)

OUTPUT_HEADER = ("date", "added", "removed")


def _fetch_raw_snapshots(url: str) -> list[tuple[str, frozenset[str]]]:
    """Download the upstream daily-snapshot CSV and parse it to (date, tickers)."""
    with urllib.request.urlopen(url) as resp:  # explicit manual refresh, not test/import-time
        text = resp.read().decode("utf-8")

    rows: list[tuple[str, frozenset[str]]] = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    if tuple(h.strip().lower() for h in header) != ("date", "tickers"):
        raise ValueError(f"unexpected upstream header {header!r}; the source format changed")
    for row in reader:
        if not row:
            continue
        date, tickers = row[0].strip(), row[1]
        members = frozenset(t.strip() for t in tickers.split(",") if t.strip())
        if not members:
            raise ValueError(f"empty membership on {date!r}; refusing to compress a bad row")
        rows.append((date, members))
    rows.sort(key=lambda r: r[0])
    return rows


def _compress_to_changes(
    rows: list[tuple[str, frozenset[str]]],
) -> list[tuple[str, list[str], list[str]]]:
    """Collapse consecutive identical snapshots into one row per actual change.

    The first row's "added" list is the full initial snapshot (nothing to diff
    against), which doubles as the base membership a reader replays forward from.
    """
    changes: list[tuple[str, list[str], list[str]]] = []
    previous: frozenset[str] | None = None
    for date, members in rows:
        if members == previous:
            continue
        added = sorted(members - previous) if previous is not None else sorted(members)
        removed = sorted(previous - members) if previous is not None else []
        changes.append((date, added, removed))
        previous = members
    return changes


def main() -> int:
    print(f"Fetching {SOURCE_URL} ...", file=sys.stderr)
    rows = _fetch_raw_snapshots(SOURCE_URL)
    print(f"  {len(rows)} raw daily snapshot rows, {rows[0][0]}..{rows[-1][0]}", file=sys.stderr)

    changes = _compress_to_changes(rows)
    print(f"  compressed to {len(changes)} change rows", file=sys.stderr)

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE_PATH.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(OUTPUT_HEADER)
        for date, added, removed in changes:
            writer.writerow([date, ";".join(added), ";".join(removed)])

    print(f"Wrote {FIXTURE_PATH} ({FIXTURE_PATH.stat().st_size} bytes)", file=sys.stderr)
    print(
        "Review the diff before committing -- verify a sample of known changes "
        "(see this script's docstring) still land on the right date.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
