"""Fast unit tests for YFinanceAdapter's cache — via a stub fetcher, no network.

The fetcher is injected, so these tests exercise the read-through cache and
determinism (dev-playbook seam) without touching yfinance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from trading.data.yfinance_adapter import YFinanceAdapter


class _StubFetcher:
    """Returns a fixed frame and counts how often it's called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.calls += 1
        idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
        return pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [10.5, 11.5],
                "low": [9.5, 10.5],
                "close": [10.2, 11.2],
                "volume": [1000, 1100],
            },
            index=pd.Index(idx, name="ts"),
        )


def _range() -> tuple[datetime, datetime]:
    return datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)


def test_first_call_fetches_and_writes_cache(tmp_path: Path) -> None:
    fetcher = _StubFetcher()
    adapter = YFinanceAdapter(tmp_path, fetcher)
    start, end = _range()

    bars = adapter.get_bars("AAA", start, end)

    assert fetcher.calls == 1
    assert list(tmp_path.glob("AAA_*_adj.csv"))  # cache file written
    assert [b.close for b in bars] == [10.2, 11.2]
    # Timestamps are made tz-aware UTC.
    assert all(b.ts.tzinfo is not None for b in bars)
    assert bars[0].volume == 1000


def test_second_call_hits_cache_and_is_deterministic(tmp_path: Path) -> None:
    fetcher = _StubFetcher()
    adapter = YFinanceAdapter(tmp_path, fetcher)
    start, end = _range()

    first = adapter.get_bars("AAA", start, end)
    second = adapter.get_bars("AAA", start, end)

    assert fetcher.calls == 1  # network not hit again
    assert first == second  # identical, bit-for-bit


def test_unadjusted_request_is_rejected(tmp_path: Path) -> None:
    adapter = YFinanceAdapter(tmp_path, _StubFetcher())
    start, end = _range()
    with pytest.raises(ValueError, match="adjusted"):
        adapter.get_bars("AAA", start, end, adjusted=False)


def test_unadjusted_request_steers_to_alpaca_or_synthetic(tmp_path: Path) -> None:
    # ADR-0021: yfinance is a backtest-only (adjusted) source; the raw rejection
    # must point the user at a raw live source and an offline demo source.
    adapter = YFinanceAdapter(tmp_path, _StubFetcher())
    start, end = _range()
    with pytest.raises(ValueError, match="--source alpaca"):
        adapter.get_bars("AAA", start, end, adjusted=False)
    with pytest.raises(ValueError, match="--source synthetic"):
        adapter.get_bars("AAA", start, end, adjusted=False)


class TestAbsenceIsDataNotFailure:
    """An empty provider response is cached and returned as [] (ADR-0032)."""

    def test_empty_response_returns_no_bars_instead_of_raising(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def empty_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
            calls.append(symbol)
            frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            frame.index.name = "ts"
            return frame

        adapter = YFinanceAdapter(tmp_path, fetcher=empty_fetch)
        assert adapter.get_bars("NOTLISTED", *_range()) == []
        assert calls == ["NOTLISTED"]

    def test_absence_is_cached_so_later_folds_skip_the_network(self, tmp_path: Path) -> None:
        """The wart this fixes: the raise happened before the cache write, so every
        walk-forward fold re-hit the network to fail again."""
        calls: list[str] = []

        def empty_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
            calls.append(symbol)
            frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            frame.index.name = "ts"
            return frame

        adapter = YFinanceAdapter(tmp_path, fetcher=empty_fetch)
        assert adapter.get_bars("NOTLISTED", *_range()) == []
        assert adapter.get_bars("NOTLISTED", *_range()) == []
        assert calls == ["NOTLISTED"]  # exactly one fetch, not two

    def test_a_raising_fetcher_still_propagates(self, tmp_path: Path) -> None:
        """Absence is tolerated; a genuine lookup failure is still an exception."""

        def broken_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
            raise ConnectionError("upstream reset the connection")

        adapter = YFinanceAdapter(tmp_path, fetcher=broken_fetch)
        with pytest.raises(ConnectionError):
            adapter.get_bars("AAPL", *_range())
