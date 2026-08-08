"""Fast, offline tests for the Alpaca data adapter.

Everything here runs against :class:`FakeAlpacaClient` with no network, no key,
and no SDK, so this module deliberately does NOT import ``alpaca``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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


class _DualClient(FakeAlpacaClient):
    """Serves DISTINGUISHABLE series for raw vs adjusted, so a per-call flag shows.

    Adjusted asks get total-return closes; raw asks get the (different) actual
    quotes — mirroring what ``Adjustment.ALL`` vs ``Adjustment.RAW`` would return.
    Subclasses the fake so it still satisfies the full ``AlpacaClient`` seam.
    """

    def __init__(self, symbol: str, adjusted: list[float], raw: list[float]) -> None:
        super().__init__({symbol: _series(symbol, adjusted)})
        self._adjusted_series = _series(symbol, adjusted)
        self._raw_series = _series(symbol, raw)

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        source = self._adjusted_series if adjusted else self._raw_series
        return [b for b in source if start <= b.ts <= end]


def test_per_call_adjusted_flag_selects_the_series() -> None:
    # ADR-0021: the per-call keyword controls the fetch, overriding the default.
    client = _DualClient("AAPL", adjusted=[10.0, 11.0], raw=[100.0, 110.0])
    adapter = AlpacaAdapter(client)  # constructor default is adjusted=True

    got_adjusted = adapter.get_bars("AAPL", _WIDE_START, _WIDE_END, adjusted=True)
    got_raw = adapter.get_bars("AAPL", _WIDE_START, _WIDE_END, adjusted=False)

    assert [b.close for b in got_adjusted] == [10.0, 11.0]
    assert [b.close for b in got_raw] == [100.0, 110.0]


def test_per_call_flag_overrides_the_constructor_default() -> None:
    # A raw-defaulted adapter still honors an explicit adjusted=True request.
    client = _DualClient("AAPL", adjusted=[10.0], raw=[100.0])
    adapter = AlpacaAdapter(client, adjusted=False)

    assert [b.close for b in adapter.get_bars("AAPL", _WIDE_START, _WIDE_END)] == [100.0]
    assert [b.close for b in adapter.get_bars("AAPL", _WIDE_START, _WIDE_END, adjusted=True)] == [
        10.0
    ]


class _IntervalRecordingClient(FakeAlpacaClient):
    """Records which seam method served the last call and the interval it saw."""

    last_method: str | None = None
    last_interval: timedelta | None = None

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        self.last_method = "get_daily_bars"
        self.last_interval = None
        return super().get_daily_bars(symbol, start, end, adjusted=adjusted)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
        interval: timedelta = timedelta(days=1),
    ) -> list[Bar]:
        self.last_method = "get_bars"
        self.last_interval = interval
        return super().get_bars(symbol, start, end, adjusted=adjusted, interval=interval)


def _intraday_series(symbol: str, closes: list[float]) -> list[Bar]:
    """Hourly bars on 2026-01-02, START timestamps 13:30, 14:30, ..."""
    base = datetime(2026, 1, 2, 13, 30, tzinfo=UTC)
    return [
        Bar(symbol, base + timedelta(hours=i), c, c, c, c, volume=1_000)
        for i, c in enumerate(closes)
    ]


def test_default_interval_routes_through_get_daily_bars() -> None:
    client = _IntervalRecordingClient({"AAPL": _series("AAPL", [100.0, 101.0])})
    AlpacaAdapter(client).get_bars("AAPL", _WIDE_START, _WIDE_END)
    assert client.last_method == "get_daily_bars"


def test_intraday_interval_routes_through_get_bars_with_the_interval() -> None:
    bars = _intraday_series("AAPL", [100.0, 101.0, 102.0])
    client = _IntervalRecordingClient({"AAPL": bars})
    adapter = AlpacaAdapter(client, interval=timedelta(hours=1))

    got = adapter.get_bars("AAPL", _WIDE_START, _WIDE_END)

    assert client.last_method == "get_bars"
    assert client.last_interval == timedelta(hours=1)
    assert got == bars  # the fake serves the intraday bars it was given, ascending


def test_fake_get_bars_serves_intraday_bars_in_range() -> None:
    bars = _intraday_series("AAPL", [100.0, 101.0, 102.0, 103.0])
    client = FakeAlpacaClient({"AAPL": bars})
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    end = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
    got = client.get_bars("AAPL", start, end, adjusted=True, interval=timedelta(hours=1))
    assert [b.close for b in got] == [101.0, 102.0]


class TestDataFeed:
    """The market-data tape is a construction property of the client (ADR-0034)."""

    def test_feed_with_an_injected_client_is_rejected(self) -> None:
        # An injected client already carries its own feed; accepting one here too
        # would silently ignore it, which is how a live run ends up on a tape the
        # operator did not pick.
        client = FakeAlpacaClient({"AAA": []})
        with pytest.raises(ValueError, match="only when AlpacaAdapter builds its own client"):
            AlpacaAdapter(client=client, feed="iex")

    def test_no_feed_with_an_injected_client_is_fine(self) -> None:
        client = FakeAlpacaClient({"AAA": []})
        assert AlpacaAdapter(client=client) is not None
