"""Fast, offline tests for `scripts/crypto_daily_tape_density.py` (KAN-1078).

`scripts/` is not an installed package (see `pyproject.toml`'s
`packages = ["src/trading"]`), so the module under test is loaded by file path
rather than a normal import -- the same reason none of the other `scripts/*.py`
files have a test module of their own yet. Everything here runs on hand-built
`Bar`s through a `FakeAdapter`; no network, no Alpaca credentials.

The point of this file is the late-listing-vs-genuine-hole split
(`SymbolTapeDensity.coverage_since_first_bar` / `.listed_late`), which is what let
the orchestrating session's real 2026-09-09 measurement tell AAVE/AVAX's ~85-91%
"full window" numbers (pure listing-date artifacts, 100.1% once re-anchored on
their own first bar) apart from SOL/USD's ~80% number (a genuine hole: SOL has
been listed since the window start, same as BTC/ETH, and still misses ~20% of its
daily bars). `test_late_listing_is_not_penalized_once_rescored` reproduces that
exact distinction on synthetic data so it is a tested behavior, not just something
asserted in prose in `docs/crypto-daily-tape-density-2026-09-09.md`.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading.calendar import CRYPTO_24_7
from trading.data.fake import FakeAdapter
from trading.frequency import Frequency
from trading.types import Bar

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "crypto_daily_tape_density.py"
_spec = importlib.util.spec_from_file_location("crypto_daily_tape_density", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
tape_density_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tape_density_script
_spec.loader.exec_module(tape_density_script)

FREQ = Frequency.parse("1d", calendar=CRYPTO_24_7)
EPOCH = datetime(2021, 1, 1, tzinfo=UTC)


def _bar(symbol: str, day_offset: int, price: float = 100.0) -> Bar:
    return Bar(symbol, EPOCH + timedelta(days=day_offset), price, price, price, price, 1_000)


class TestSummarizeCoverage:
    """Pure `summarize_coverage` -- no adapter, no fetch, just the arithmetic."""

    def test_fully_present_series_scores_near_full_coverage(self) -> None:
        bars = [_bar("BTC/USD", day) for day in range(100)]  # day 0..99, no gaps
        window_start, window_end = EPOCH, EPOCH + timedelta(days=99)

        result = tape_density_script.summarize_coverage(
            "BTC/USD", bars, window_start, window_end, FREQ
        )

        assert result.bar_count == 100
        assert not result.unverified
        assert not result.listed_late
        assert result.coverage_full_window == pytest.approx(1.0, rel=0.05)
        # Scoring from its own first bar changes nothing for a symbol that was
        # already present at the window start.
        assert result.coverage_since_first_bar == pytest.approx(1.0, rel=0.05)

    def test_missing_days_score_visibly_lower_coverage(self) -> None:
        # 100-day window, but only 80 of the days actually printed a bar --
        # a genuine hole, the SOL/USD case (measured ~79.9% over 2076 days).
        present_days = [day for day in range(100) if day % 5 != 0]  # drops 20 of 100
        bars = [_bar("SOL/USD", day) for day in present_days]
        window_start, window_end = EPOCH, EPOCH + timedelta(days=99)

        result = tape_density_script.summarize_coverage(
            "SOL/USD", bars, window_start, window_end, FREQ
        )

        assert result.bar_count == 80
        assert not result.listed_late  # present from day 0, so this is a real gap
        assert result.coverage_full_window is not None
        assert result.coverage_full_window < 0.90
        assert result.coverage_full_window > 0.70

    def test_late_listing_is_not_penalized_once_rescored(self) -> None:
        # Listed 50 days into the window (the AAVE/AVAX shape) but complete since
        # then -- scored against the fixed window this looks like a ~50% hole;
        # re-anchored on its own first bar it is complete.
        bars = [_bar("AAVE/USD", day) for day in range(50, 100)]  # day 50..99, no gaps
        window_start, window_end = EPOCH, EPOCH + timedelta(days=99)

        result = tape_density_script.summarize_coverage(
            "AAVE/USD", bars, window_start, window_end, FREQ
        )

        assert result.bar_count == 50
        assert result.listed_late
        # The naive, fixed-window number reads as a large hole...
        assert result.coverage_full_window is not None
        assert result.coverage_full_window < 0.60
        # ...but re-anchored on its own first bar it is (near) complete, exactly
        # like AAVE/AVAX's measured 100.1% since-listing coverage.
        assert result.coverage_since_first_bar == pytest.approx(1.0, rel=0.05)

    def test_symbol_with_no_bars_is_unverified_not_a_hole(self) -> None:
        result = tape_density_script.summarize_coverage(
            "GHOST/USD", [], EPOCH, EPOCH + timedelta(days=9), FREQ
        )

        assert result.unverified
        assert result.coverage_full_window is None
        assert result.coverage_since_first_bar is None
        assert not result.listed_late


class TestFetchAndSummarize:
    """The fetch + score path, through a `FakeAdapter` -- still fully offline."""

    def test_fetches_raw_and_scores_the_result(self) -> None:
        window_start, window_end = EPOCH, EPOCH + timedelta(days=49)
        bars = [_bar("LINK/USD", day) for day in range(50)]
        adapter = FakeAdapter(bars)

        result = tape_density_script.fetch_and_summarize(
            adapter, "LINK/USD", window_start, window_end, FREQ
        )

        assert result.symbol == "LINK/USD"
        assert result.bar_count == 50
        assert result.coverage_full_window == pytest.approx(1.0, rel=0.05)

    def test_missing_symbol_comes_back_unverified(self) -> None:
        adapter = FakeAdapter([_bar("LINK/USD", day) for day in range(10)])

        result = tape_density_script.fetch_and_summarize(
            adapter, "NOPE/USD", EPOCH, EPOCH + timedelta(days=9), FREQ
        )

        assert result.unverified
