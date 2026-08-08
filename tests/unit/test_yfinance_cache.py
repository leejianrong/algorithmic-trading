"""Fast unit tests for YFinanceAdapter's cache — via a stub fetcher, no network.

The fetcher is injected, so these tests exercise the read-through cache and
determinism (dev-playbook seam) without touching yfinance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yfinance
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from trading.data.yfinance_adapter import (
    ProviderRefusedError,
    YFinanceAdapter,
    _default_fetch,
)
from trading.engine import REASON_FETCH_FAILED, REASON_NO_BARS, load_series


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


def _empty_download(*args: Any, **kwargs: Any) -> pd.DataFrame:
    """What ``yf.download`` returns for BOTH a rate limit and a real absence.

    ``multi.py``'s ``_download_one`` catches every per-ticker exception and
    substitutes an empty frame, so the caller cannot tell the two apart from the
    return value alone — the bug ADR-0040 fixes.
    """
    return pd.DataFrame()


class _RateLimitedTicker:
    """``Ticker.history`` re-raises ``YFRateLimitError`` unconditionally (1.5.2)."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def history(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        raise YFRateLimitError()


class _AbsentTicker:
    """A delisted / not-yet-listed symbol: empty frame, no exception."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def history(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()


class _SoftFailTicker:
    """The provider named a *missing-prices* condition rather than a refusal."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def history(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        raise YFPricesMissingError(self.symbol, "(1d 2003-01-01 -> 2003-02-01)")


class TestARefusalIsNotAnAbsence:
    """A rate limit must not read as missing history, nor vice versa (ADR-0040).

    This is the failure that motivated the whole slice: CI's *required* integration
    job died on ``YFRateLimitError('Too Many Requests...')`` and the bench rendered
    it as ``EmptyUniverseError: no bars for AAPL ... not listed in this window, or
    the source has no history``. Both readings of that sentence are wrong, and the
    dangerous one is the second: a genuine provider break gets re-run away.
    """

    def test_a_rate_limited_fetch_raises_instead_of_reporting_no_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(yfinance, "download", _empty_download)
        monkeypatch.setattr(yfinance, "Ticker", _RateLimitedTicker)

        with pytest.raises(ProviderRefusedError) as excinfo:
            _default_fetch("AAPL", *_range())

        message = str(excinfo.value)
        assert "AAPL" in message
        assert "NOT missing history" in message
        assert "Rate limited" in message  # the provider's own words, quoted

    def test_a_genuinely_absent_symbol_still_returns_an_empty_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ADR-0032 behaviour must survive: absence is data, not an error."""
        monkeypatch.setattr(yfinance, "download", _empty_download)
        monkeypatch.setattr(yfinance, "Ticker", _AbsentTicker)

        frame = _default_fetch("META", *_range())

        assert frame.empty
        assert list(frame.columns) == ["open", "high", "low", "close", "volume"]

    def test_a_named_missing_prices_failure_is_read_as_absence_not_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Conservative on purpose: only a refusal we can *name* becomes a failure.

        Misclassifying a late listing as a fetch failure would re-break the
        multi-decade walk-forward ADR-0032 fixed, so everything that is not a
        recognised refusal keeps the empty-means-absent reading.
        """
        monkeypatch.setattr(yfinance, "download", _empty_download)
        monkeypatch.setattr(yfinance, "Ticker", _SoftFailTicker)

        assert _default_fetch("META", *_range()).empty

    def test_the_probe_only_runs_when_the_response_was_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful fetch must cost exactly one request, as before."""
        probes: list[str] = []

        def good_download(*args: Any, **kwargs: Any) -> pd.DataFrame:
            idx = pd.to_datetime(["2024-01-02"])
            return pd.DataFrame(
                {
                    "Open": [10.0],
                    "High": [10.5],
                    "Low": [9.5],
                    "Close": [10.2],
                    "Volume": [1000],
                },
                index=pd.Index(idx, name="Date"),
            )

        class _SpyTicker:
            def __init__(self, symbol: str) -> None:
                probes.append(symbol)

            def history(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
                return pd.DataFrame()

        monkeypatch.setattr(yfinance, "download", good_download)
        monkeypatch.setattr(yfinance, "Ticker", _SpyTicker)

        frame = _default_fetch("AAPL", *_range())

        assert not frame.empty
        assert probes == []

    def test_the_engine_reports_a_refusal_as_fetch_failed_not_no_bars(self, tmp_path: Path) -> None:
        """The two reason codes, end to end through the adapter seam (ADR-0032)."""

        def refusing_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
            raise ProviderRefusedError("yfinance refused the request for AAPL: rate limited")

        def absent_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
            frame = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            frame.index.name = "ts"
            return frame

        start, end = _range()

        refusing = YFinanceAdapter(tmp_path / "a", refusing_fetch)
        absenting = YFinanceAdapter(tmp_path / "b", absent_fetch)
        _, refused = load_series(refusing, ["AAPL"], start, end)
        _, absent = load_series(absenting, ["AAPL"], start, end)

        assert [a.reason for a in refused] == [REASON_FETCH_FAILED]
        assert "not listed in this window" not in refused[0].detail
        assert [a.reason for a in absent] == [REASON_NO_BARS]
        assert "not listed in this window" in absent[0].detail
