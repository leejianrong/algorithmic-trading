"""Fast, offline tests for the Alpaca client seam (ADR-0017, ADR-0018).

Everything here runs against :class:`FakeAlpacaClient` with no network, no key,
and no wall clock. The real SDK wrapper never runs in the fast layer, so this
module deliberately does NOT import ``alpaca``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.data.alpaca_client import (
    STATUS_FILLED,
    STATUS_NEW,
    AccountSnapshot,
    AlpacaClient,
    AlpacaOrder,
    AssetInfo,
    FakeAlpacaClient,
    PositionSnapshot,
)
from trading.types import Bar, Side


def _bar(symbol: str, day: int, close: float) -> Bar:
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close, close=close, volume=1_000)


def _series(symbol: str, closes: list[float]) -> list[Bar]:
    return [_bar(symbol, i, c) for i, c in enumerate(closes)]


_WIDE_START = datetime(2026, 1, 1, tzinfo=UTC)
_WIDE_END = datetime(2026, 12, 31, tzinfo=UTC)


class TestRuntimeCheckable:
    def test_fake_satisfies_protocol(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])})
        assert isinstance(client, AlpacaClient)


class TestBars:
    def test_bars_round_trip_ascending(self) -> None:
        bars = _series("AAPL", [100.0, 101.0, 102.0])
        client = FakeAlpacaClient({"AAPL": list(reversed(bars))})
        got = client.get_daily_bars("AAPL", _WIDE_START, _WIDE_END, adjusted=True)
        assert got == bars  # stored sorted ascending regardless of input order

    def test_bars_date_filtered_inclusive(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0, 101.0, 102.0, 103.0])})
        start = datetime(2026, 1, 2, tzinfo=UTC)
        end = datetime(2026, 1, 3, tzinfo=UTC)
        got = client.get_daily_bars("AAPL", start, end, adjusted=True)
        assert [b.close for b in got] == [101.0, 102.0]

    def test_unknown_symbol_returns_empty(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])})
        got = client.get_daily_bars("MSFT", _WIDE_START, _WIDE_END, adjusted=True)
        assert got == []


class TestImmediateFill:
    def test_buy_updates_cash_and_position(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0)
        order = client.submit_order("AAPL", 4.0, Side.BUY)

        assert order.status == STATUS_FILLED
        assert order.filled_qty == 4.0
        assert order.filled_avg_price == 50.0

        account = client.get_account()
        assert account.cash == pytest.approx(800.0)  # 1000 - 4*50
        assert account.equity == pytest.approx(1_000.0)  # cash 800 + 4 shares * 50

        positions = client.list_positions()
        assert positions == [PositionSnapshot("AAPL", 4.0, 50.0)]

    def test_get_order_reflects_fill(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])})
        order = client.submit_order("AAPL", 1.0, Side.BUY)
        assert client.get_order(order.id) == order

    def test_set_price_overrides_last_close(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0)
        client.set_price("AAPL", 60.0)
        order = client.submit_order("AAPL", 2.0, Side.BUY)
        assert order.filled_avg_price == 60.0
        assert client.get_account().cash == pytest.approx(880.0)

    def test_sell_closes_position_and_returns_cash(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0)
        client.submit_order("AAPL", 4.0, Side.BUY)
        client.set_price("AAPL", 55.0)
        client.submit_order("AAPL", 4.0, Side.SELL)

        assert client.list_positions() == []  # flat
        # 1000 - 4*50 (buy) + 4*55 (sell) = 1020
        assert client.get_account().cash == pytest.approx(1_020.0)

    def test_fractional_qty_allowed(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [40.0])}, cash=100.0)
        client.submit_order("AAPL", 1.5, Side.BUY)
        assert client.list_positions()[0].qty == pytest.approx(1.5)

    def test_partial_sell_keeps_avg_price(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0)
        client.submit_order("AAPL", 4.0, Side.BUY)
        client.set_price("AAPL", 70.0)
        client.submit_order("AAPL", 1.0, Side.SELL)
        pos = client.list_positions()[0]
        assert pos.qty == pytest.approx(3.0)
        assert pos.avg_price == pytest.approx(50.0)  # entry basis unchanged by a sell


class TestNoShorting:
    def test_sell_more_than_held_rejected(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0)
        client.submit_order("AAPL", 2.0, Side.BUY)
        with pytest.raises(ValueError, match="shorting"):
            client.submit_order("AAPL", 3.0, Side.SELL)

    def test_sell_with_no_position_rejected(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])})
        with pytest.raises(ValueError, match="shorting"):
            client.submit_order("AAPL", 1.0, Side.SELL)

    def test_non_positive_qty_rejected(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])})
        with pytest.raises(ValueError, match="positive"):
            client.submit_order("AAPL", 0.0, Side.BUY)


class TestPendingMode:
    def test_pending_order_unfilled_until_advanced(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0, auto_fill=False)
        order = client.submit_order("AAPL", 2.0, Side.BUY)

        assert order.status == STATUS_NEW
        assert order.filled_qty == 0.0
        assert order.filled_avg_price is None
        # state untouched while pending -> a poll-then-timeout can be tested
        assert client.get_account().cash == pytest.approx(1_000.0)
        assert client.list_positions() == []
        assert client.get_order(order.id).status == STATUS_NEW

    def test_fill_order_advances_and_settles(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0, auto_fill=False)
        order = client.submit_order("AAPL", 2.0, Side.BUY)
        filled = client.fill_order(order.id, price=50.0)

        assert filled.status == STATUS_FILLED
        assert filled.filled_qty == 2.0
        assert client.get_order(order.id).status == STATUS_FILLED
        assert client.get_account().cash == pytest.approx(900.0)
        assert client.list_positions() == [PositionSnapshot("AAPL", 2.0, 50.0)]

    def test_fill_order_is_idempotent(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [50.0])}, cash=1_000.0, auto_fill=False)
        order = client.submit_order("AAPL", 2.0, Side.BUY)
        client.fill_order(order.id, price=50.0)
        again = client.fill_order(order.id, price=50.0)  # no double-spend
        assert again.status == STATUS_FILLED
        assert client.get_account().cash == pytest.approx(900.0)


class TestDtos:
    def test_dto_fields(self) -> None:
        order = AlpacaOrder(
            id="1",
            symbol="AAPL",
            qty=1.0,
            side=Side.BUY,
            status=STATUS_FILLED,
            filled_qty=1.0,
            filled_avg_price=50.0,
        )
        assert (order.id, order.symbol, order.side) == ("1", "AAPL", Side.BUY)
        assert AccountSnapshot(cash=1.0, equity=2.0).equity == 2.0
        assert PositionSnapshot("AAPL", 3.0, 4.0).avg_price == 4.0


class TestAssetInfo:
    """The asset-metadata DTO (ADR-0028) — our own type, never an SDK model."""

    def test_fields_and_descriptive_defaults(self) -> None:
        asset = AssetInfo(symbol="AAPL", tradable=True, fractionable=True)
        assert (asset.symbol, asset.tradable, asset.fractionable) == ("AAPL", True, True)
        # Descriptive fields are optional; absent means empty/False, not unknown-truthy.
        assert (asset.exchange, asset.name, asset.shortable) == ("", "", False)

    def test_reports_broker_flags_verbatim(self) -> None:
        # An untradable-but-fractionable answer is preserved, not "fixed up" into
        # a tidier combination — the broker's answer is the fact.
        asset = AssetInfo(symbol="XYZ", tradable=False, fractionable=True)
        assert asset.tradable is False
        assert asset.fractionable is True

    def test_empty_symbol_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            AssetInfo(symbol="", tradable=True, fractionable=True)

    def test_whitespace_symbol_rejected(self) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            AssetInfo(symbol="AA PL", tradable=True, fractionable=True)
        with pytest.raises(ValueError, match="whitespace"):
            AssetInfo(symbol=" AAPL", tradable=True, fractionable=True)

    def test_frozen(self) -> None:
        asset = AssetInfo(symbol="AAPL", tradable=True, fractionable=True)
        with pytest.raises(AttributeError):
            asset.tradable = False  # type: ignore[misc]


class TestGetAsset:
    """``get_asset`` is part of the seam and the fake scripts every answer."""

    def test_protocol_includes_get_asset(self) -> None:
        client = FakeAlpacaClient()
        assert isinstance(client, AlpacaClient)
        assert hasattr(AlpacaClient, "get_asset")

    def test_default_asset_is_tradable_and_fractionable(self) -> None:
        asset = FakeAlpacaClient().get_asset("AAPL")
        assert (asset.symbol, asset.tradable, asset.fractionable) == ("AAPL", True, True)

    def test_set_asset_scripts_flags(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("BRK.A", fractionable=False)
        client.set_asset("HALT", tradable=False)

        assert client.get_asset("BRK.A").fractionable is False
        assert client.get_asset("BRK.A").tradable is True  # only the named flag moves
        assert client.get_asset("HALT").tradable is False

    def test_assets_can_be_supplied_at_construction(self) -> None:
        supplied = AssetInfo(symbol="AAPL", tradable=True, fractionable=False, name="Apple Inc.")
        client = FakeAlpacaClient(assets={"AAPL": supplied})
        assert client.get_asset("AAPL") == supplied

    def test_set_asset_failure_raises_lookup_error(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset_failure("BOGUS", "asset not found")
        with pytest.raises(LookupError, match="asset not found"):
            client.get_asset("BOGUS")

    def test_set_asset_clears_a_previous_failure(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset_failure("AAPL")
        client.set_asset("AAPL")
        assert client.get_asset("AAPL").tradable is True
