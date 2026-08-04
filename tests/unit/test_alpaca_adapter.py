"""Fast, offline tests for the Alpaca data adapter.

Everything here runs against :class:`FakeAlpacaClient` with no network, no key,
and no SDK, so this module deliberately does NOT import ``alpaca``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading.data.alpaca_adapter import AlpacaAdapter
from trading.data.alpaca_client import FakeAlpacaClient
from trading.interfaces import DataAdapter
from trading.types import Bar


def _bar(symbol: str, day: int, close: float) -> Bar:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close, close=close, volume=1_000)


def _series(symbol: str, closes: list[float]) -> list[Bar]:
    return [_bar(symbol, i, c) for i, c in enumerate(closes)]


_WIDE_START = datetime(2026, 1, 1, tzinfo=UTC)
_WIDE_END = datetime(2026, 12, 31, tzinfo=UTC)


class _RecordingClient(FakeAlpacaClient):
    """A FakeAlpacaClient that remembers the ``adjusted`` flag it last received."""

    last_adjusted: bool | None = None

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        self.last_adjusted = adjusted
        return super().get_daily_bars(symbol, start, end, adjusted=adjusted)


def test_adapter_satisfies_the_data_adapter_protocol() -> None:
    adapter = AlpacaAdapter(FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}))
    assert isinstance(adapter, DataAdapter)


def test_get_bars_returns_the_seams_bars_ascending() -> None:
    bars = _series("AAPL", [100.0, 101.0, 102.0])
    client = FakeAlpacaClient({"AAPL": list(reversed(bars))})
    adapter = AlpacaAdapter(client)
    assert adapter.get_bars("AAPL", _WIDE_START, _WIDE_END) == bars


def test_get_bars_filters_to_range_inclusive() -> None:
    client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0, 101.0, 102.0, 103.0])})
    adapter = AlpacaAdapter(client)
    start = datetime(2026, 1, 2, tzinfo=UTC)
    end = datetime(2026, 1, 3, tzinfo=UTC)
    got = adapter.get_bars("AAPL", start, end)
    assert [b.close for b in got] == [101.0, 102.0]


def test_unknown_symbol_returns_empty() -> None:
    adapter = AlpacaAdapter(FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}))
    assert adapter.get_bars("TSLA", _WIDE_START, _WIDE_END) == []


def test_adjusted_flag_defaults_true_and_is_threaded_to_the_client() -> None:
    client = _RecordingClient({"AAPL": _series("AAPL", [100.0])})
    AlpacaAdapter(client).get_bars("AAPL", _WIDE_START, _WIDE_END)
    assert client.last_adjusted is True


def test_adjusted_false_is_threaded_to_the_client() -> None:
    client = _RecordingClient({"AAPL": _series("AAPL", [100.0])})
    AlpacaAdapter(client, adjusted=False).get_bars("AAPL", _WIDE_START, _WIDE_END)
    assert client.last_adjusted is False
