"""Fast unit tests for the FakeAdapter seam and its DataAdapter conformance."""

from __future__ import annotations

from datetime import UTC, datetime

from trading.data.fake import FakeAdapter
from trading.interfaces import DataAdapter
from trading.types import Bar


def _bar(symbol: str, day: int, close: float) -> Bar:
    ts = datetime(2024, 1, day, tzinfo=UTC)
    return Bar(symbol, ts, open=close, high=close, low=close, close=close, volume=1_000)


def test_fake_adapter_satisfies_the_data_adapter_protocol() -> None:
    adapter = FakeAdapter([])
    assert isinstance(adapter, DataAdapter)


def test_returns_bars_sorted_and_filtered_to_range() -> None:
    adapter = FakeAdapter([_bar("AAPL", 3, 30.0), _bar("AAPL", 1, 10.0), _bar("AAPL", 2, 20.0)])
    bars = adapter.get_bars(
        "AAPL", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC)
    )
    assert [b.close for b in bars] == [10.0, 20.0]


def test_isolates_symbols() -> None:
    adapter = FakeAdapter([_bar("AAPL", 1, 10.0), _bar("MSFT", 1, 99.0)])
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)
    assert [b.symbol for b in adapter.get_bars("MSFT", start, end)] == ["MSFT"]
    assert adapter.get_bars("TSLA", start, end) == []
