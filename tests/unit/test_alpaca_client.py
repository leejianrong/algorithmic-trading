"""Fast, offline tests for the Alpaca client seam (ADR-0017, ADR-0018).

Everything here runs against :class:`FakeAlpacaClient` with no network, no key,
and no wall clock. The real SDK wrapper never runs in the fast layer, so this
module deliberately does NOT import ``alpaca``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.data.alpaca_client import (
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_NEW,
    AccountSnapshot,
    AlpacaClient,
    AlpacaOrder,
    AssetInfo,
    FakeAlpacaClient,
    OrderRejectedError,
    PositionSnapshot,
    _classify_order_error,
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


class TestListAssets:
    """The seam's eighth call (KAN-863): enumerate a venue's whole listing."""

    def test_protocol_includes_list_assets(self) -> None:
        client = FakeAlpacaClient()
        assert isinstance(client, AlpacaClient)
        assert hasattr(AlpacaClient, "list_assets")

    def test_unscripted_fake_lists_nothing(self) -> None:
        """The fake has no notion of a venue-wide listing beyond what a test declares."""
        assert FakeAlpacaClient().list_assets() == []

    def test_lists_every_scripted_asset(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("BTC/USD")
        client.set_asset("ETH/USD", fractionable=False)
        symbols = {a.symbol for a in client.list_assets()}
        assert symbols == {"BTC/USD", "ETH/USD"}

    def test_assets_supplied_at_construction_are_listed(self) -> None:
        supplied = AssetInfo(symbol="LINK/USD", tradable=True, fractionable=True)
        client = FakeAlpacaClient(assets={"LINK/USD": supplied})
        assert client.list_assets() == [supplied]

    def test_get_asset_does_not_add_to_the_listing(self) -> None:
        """Looking up an unscripted symbol invents a default answer but is not registration."""
        client = FakeAlpacaClient()
        client.get_asset("AAPL")  # falls back to the generic default, unscripted
        assert client.list_assets() == []


class TestCancelOrder:
    """The seam's sixth call (ADR-0017's anticipated widening, ADR-0036).

    Every behaviour asserted here was observed against the live paper venue with
    the market closed, not guessed: a working order cancels to ``canceled``, a
    second cancel of an already-terminal order is accepted silently, and an
    unknown id is a 404 the wrapper turns into a :class:`LookupError`.
    """

    def test_protocol_includes_cancel_order(self) -> None:
        client = FakeAlpacaClient()
        assert isinstance(client, AlpacaClient)
        assert hasattr(AlpacaClient, "cancel_order")

    def test_cancelling_a_working_order_makes_it_canceled(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}, auto_fill=False)
        order = client.submit_order("AAPL", 1.0, Side.BUY)
        assert client.get_order(order.id).status == STATUS_NEW

        client.cancel_order(order.id)

        assert client.get_order(order.id).status == STATUS_CANCELED

    def test_cancel_keeps_a_partial_fill_on_the_record(self) -> None:
        # A partially-filled-then-canceled order moved real shares; cancelling
        # must not erase them (ADR-0033 emits that partial as a Fill).
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}, auto_fill=False)
        order = client.submit_order("AAPL", 4.0, Side.BUY)
        client.set_order_status(order.id, "partially_filled", filled_qty=1.5, filled_avg_price=99.5)

        client.cancel_order(order.id)

        after = client.get_order(order.id)
        assert after.status == STATUS_CANCELED
        assert after.filled_qty == 1.5
        assert after.filled_avg_price == 99.5

    def test_cancelling_a_terminal_order_is_a_silent_no_op(self) -> None:
        # Observed live 2026-08-08: DELETE /orders/{id} on an already-canceled
        # order returned 200 with no error, so the fake must not invent one.
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])})
        order = client.submit_order("AAPL", 1.0, Side.BUY)  # auto_fill -> filled
        assert order.status == STATUS_FILLED

        client.cancel_order(order.id)

        assert client.get_order(order.id).status == STATUS_FILLED

    def test_cancelling_an_unknown_order_raises_lookup_error(self) -> None:
        # The venue answers 404; "we never heard of it" must stay distinct from
        # "we cancelled it", exactly as get_asset keeps unknown apart from refused.
        with pytest.raises(LookupError, match="no-such-order"):
            FakeAlpacaClient().cancel_order("no-such-order")


class _StubApiError(Exception):
    """A stand-in for ``alpaca.common.exceptions.APIError``, shaped from life.

    Two details are copied deliberately, because the classifier depends on both
    and they were observed on the wire (2026-08-08) rather than assumed:

    * ``status_code`` is a plain int attribute.
    * ``code`` is a *property that raises* when the body carries no numeric code.
      alpaca-py implements it as ``json.loads(self._error)["code"]``, so an auth
      failure -- whose body is just ``{"message": "unauthorized."}`` -- makes
      ``exc.code`` throw ``KeyError`` rather than return ``None``.
    """

    def __init__(self, body: str, *, status_code: int, code: int | None = None) -> None:
        super().__init__(body)
        self.status_code = status_code
        self._code = code

    @property
    def code(self) -> int:
        if self._code is None:
            raise KeyError("code")
        return self._code


class TestOrderRefusalClassification:
    """ "The venue refused this order" vs "we could not ask" (ADR-0041).

    Every case below is a real response body recorded against the paper venue on
    2026-08-08. The discriminator is Alpaca's own error taxonomy: a refusal of a
    specific order carries a numeric ``code`` in the body, and a credential or
    transport failure does not.
    """

    def test_wash_trade_refusal_becomes_an_order_rejected_error(self) -> None:
        # The one that started this: submitting a SELL while a BUY is parked.
        exc = _StubApiError(
            '{"code":40310000,"existing_order_id":"a182da86","message":"potential wash '
            'trade detected. use complex orders","reject_reason":"opposite side '
            'market/stop order exists"}',
            status_code=403,
            code=40310000,
        )

        classified = _classify_order_error(exc, "AAPL", 0.01, Side.SELL)

        assert isinstance(classified, OrderRejectedError)
        assert "AAPL" in str(classified)
        assert "sell" in str(classified)
        # The venue's own words survive verbatim; nothing is summarised away.
        assert "potential wash trade detected" in str(classified)

    @pytest.mark.parametrize(
        ("status", "code", "body"),
        [
            (403, 40310000, '{"code":40310000,"message":"insufficient buying power"}'),
            (422, 42210000, '{"code":42210000,"message":"asset \\"ZZZZ\\" not found"}'),
            (422, 42210000, '{"code":42210000,"message":"fractional orders cannot be sold short"}'),
        ],
    )
    def test_every_observed_refusal_is_classified(self, status: int, code: int, body: str) -> None:
        exc = _StubApiError(body, status_code=status, code=code)

        assert isinstance(_classify_order_error(exc, "AAPL", 1.0, Side.BUY), OrderRejectedError)

    def test_bad_credentials_pass_through_untouched(self) -> None:
        # 401 with NO numeric code: we could not ask, so the run must not carry on
        # quietly recording rejections. Observed body, verbatim.
        exc = _StubApiError('{"message": "unauthorized."}', status_code=401)

        assert _classify_order_error(exc, "AAPL", 1.0, Side.BUY) is exc

    def test_a_rate_limit_passes_through(self) -> None:
        # Carries a code *and* is a 4xx, so only the explicit 429 exclusion keeps
        # it out -- deliberately, or this passes for the wrong reason.
        exc = _StubApiError(
            '{"code":42910000,"message":"too many requests"}', status_code=429, code=42910000
        )

        assert _classify_order_error(exc, "AAPL", 1.0, Side.BUY) is exc

    def test_a_server_error_passes_through(self) -> None:
        # Likewise: a code is present, so it is the 4xx range check doing the work.
        exc = _StubApiError(
            '{"code":50010000,"message":"internal"}', status_code=500, code=50010000
        )

        assert _classify_order_error(exc, "AAPL", 1.0, Side.BUY) is exc

    def test_a_transport_error_with_no_status_passes_through(self) -> None:
        exc = ConnectionError("connection reset by peer")

        assert _classify_order_error(exc, "AAPL", 1.0, Side.BUY) is exc

    def test_a_4xx_without_a_numeric_code_passes_through(self) -> None:
        # Belt and braces: absent taxonomy is not a refusal we can name.
        exc = _StubApiError('{"message": "forbidden."}', status_code=403)

        assert _classify_order_error(exc, "AAPL", 1.0, Side.BUY) is exc


class TestFakeSubmitRefusal:
    """The fake can refuse a submit, because the real venue does (ADR-0041).

    Before this the fake accepted every order, which is precisely why the live
    duplicate-guard test asserted an exit the venue actually refuses.
    """

    def test_a_scripted_refusal_raises_order_rejected_error(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])})
        client.set_submit_refusal("AAPL", "potential wash trade detected")

        with pytest.raises(OrderRejectedError, match="wash trade"):
            client.submit_order("AAPL", 1.0, Side.BUY)

    def test_a_refusal_is_side_scoped_so_the_other_side_still_works(self) -> None:
        # The live shape: a BUY parks at the venue and the SELL is what gets
        # refused, so the fake must be able to refuse one direction only.
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}, auto_fill=False)
        client.set_submit_refusal("AAPL", "opposite side market/stop order exists", side=Side.SELL)

        assert client.submit_order("AAPL", 1.0, Side.BUY).status == STATUS_NEW
        with pytest.raises(OrderRejectedError):
            client.submit_order("AAPL", 1.0, Side.SELL)

    def test_a_refusal_leaves_the_account_untouched(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}, cash=10_000.0)
        client.set_submit_refusal("AAPL", "insufficient buying power")

        with pytest.raises(OrderRejectedError):
            client.submit_order("AAPL", 1.0, Side.BUY)

        assert client.get_account().cash == 10_000.0
        assert client.list_positions() == []

    def test_other_symbols_are_unaffected(self) -> None:
        client = FakeAlpacaClient({"AAPL": _series("AAPL", [100.0])}, auto_fill=False)
        client.set_submit_refusal("AAPL", "nope")

        assert client.submit_order("MSFT", 1.0, Side.BUY).status == STATUS_NEW
