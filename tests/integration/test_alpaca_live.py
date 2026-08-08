"""Integration: the live Alpaca paths, executed for real (ADR-0033, ADR-0034).

Until this module, `RealAlpacaClient` / `AlpacaAdapter` / `AlpacaBroker` had never
been *run* -- ADR-0018 said so outright ("verified by inspection and types only").
These tests are what ended that, and what keeps it ended.

Gating mirrors ``test_alpaca_intraday.py`` exactly: marked ``integration`` (so it
never gates a local push) and additionally SKIPPED unless **both** credentials
(``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``) and the optional ``alpaca-py`` SDK are
present. CI installs with ``uv sync --frozen`` (no extras) and holds no Alpaca
credentials, so every test here skips there.

Two gates, deliberately different:

* ``_HAVE_SDK`` alone guards the SDK-shape tests. They construct SDK models and
  read enums locally -- no network, no key -- and pin the assumptions the wrapper's
  string handling depends on, so a future SDK release that renames a status value
  or drops an enum prefix fails here instead of silently in production.
* ``_HAVE_SDK and _HAVE_CREDS`` guards everything that talks to the venue.

PAPER ONLY. Every client here is constructed with the default ``paper=True``
(ADR-0018); the order test trades a hundredth of a share and flattens what it
opens in a ``finally``.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta

import pytest

from trading.types import Side

_HAVE_CREDS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_HAVE_SDK = importlib.util.find_spec("alpaca") is not None

pytestmark = pytest.mark.integration

_needs_sdk = pytest.mark.skipif(not _HAVE_SDK, reason="needs the alpaca-py SDK (--extra alpaca)")
_needs_live = pytest.mark.skipif(
    not (_HAVE_CREDS and _HAVE_SDK),
    reason="needs ALPACA_API_KEY / ALPACA_SECRET_KEY and the alpaca-py SDK",
)

# A liquid, fractionable mega-cap, and a ticker that cannot exist.
_SYMBOL = "AAPL"
_UNKNOWN = "ZZZZNOTREAL"
# Tiny: a hundredth of one share, a couple of dollars of paper money.
_TINY_QTY = 0.01


def _market_is_open() -> bool:
    """Whether the venue says the market is open right now.

    Order-lifecycle expectations legitimately differ by market state (ADR-0020): a
    market order placed while closed stays *working* rather than filling, so the
    test asserts the branch that actually applies instead of flaking at 4:01pm.
    """
    from trading.data.alpaca_client import RealAlpacaClient

    client = RealAlpacaClient()
    return bool(getattr(client._trading.get_clock(), "is_open", False))


# --- SDK shape: no network, no credentials ------------------------------------


@_needs_sdk
class TestSdkShapeAssumptions:
    """Pin the SDK facts the wrapper's defensive string handling rests on.

    Each of these was an *assumption* written without ever importing the SDK.
    """

    def test_order_status_values_match_our_literals(self) -> None:
        from alpaca.trading.enums import OrderStatus

        from trading.data.alpaca_client import (
            STATUS_CANCELED,
            STATUS_EXPIRED,
            STATUS_FILLED,
            STATUS_NEW,
            STATUS_REJECTED,
            STATUS_REPLACED,
        )

        values = {member.value for member in OrderStatus}
        for literal in (
            STATUS_NEW,
            STATUS_FILLED,
            STATUS_REJECTED,
            STATUS_CANCELED,
            STATUS_EXPIRED,
            STATUS_REPLACED,
        ):
            assert literal in values, f"{literal!r} is no longer an Alpaca OrderStatus"

    def test_status_str_carries_the_enum_prefix_our_parser_strips(self) -> None:
        # `_to_order` does str(status).lower().removeprefix("orderstatus."). If the
        # SDK ever switches to a bare StrEnum this still passes (removeprefix is a
        # no-op then) -- but if the *prefix spelling* changes, the parse breaks, so
        # assert the shape rather than trusting it.
        from alpaca.trading.enums import OrderStatus

        rendered = str(OrderStatus.FILLED)
        assert rendered.lower().removeprefix("orderstatus.") == "filled"

    def test_exchange_str_carries_the_prefix_to_asset_strips(self) -> None:
        from alpaca.trading.enums import AssetExchange

        rendered = str(AssetExchange.NASDAQ)
        assert rendered.split(".")[-1] == "NASDAQ"

    def test_side_str_carries_the_prefix_to_order_strips(self) -> None:
        from alpaca.trading.enums import OrderSide

        assert str(OrderSide.BUY).lower().removeprefix("orderside.") == "buy"

    def test_asset_declares_tradable_and_fractionable_as_required_bools(self) -> None:
        # ADR-0028 defaults a missing flag to False ("absent permission is not
        # permission"). Both fields are in fact *required* in this SDK, so that
        # default is a belt-and-braces guard, not the normal path -- worth knowing.
        from alpaca.trading.models import Asset

        for field in ("tradable", "fractionable"):
            assert Asset.model_fields[field].is_required()
            assert Asset.model_fields[field].annotation is bool

    def test_order_fill_fields_are_optional_and_may_be_strings(self) -> None:
        # `_to_order` reads both with getattr and coerces via float(): they are
        # declared Optional[str | float], so "present but None" is the real shape
        # (not "absent"), and the string arm is why float() is needed at all.
        from alpaca.trading.models import Order

        for field in ("filled_qty", "filled_avg_price"):
            assert not Order.model_fields[field].is_required()
            assert "str" in str(Order.model_fields[field].annotation)

    def test_account_cash_and_equity_are_optional_strings(self) -> None:
        # Why `_require_float` exists: float(None) would crash with no context.
        from alpaca.trading.models import TradeAccount

        for field in ("cash", "equity"):
            assert not TradeAccount.model_fields[field].is_required()

    def test_api_error_exposes_status_code(self) -> None:
        # The 404 -> LookupError mapping reads `exc.status_code`.
        from alpaca.common.exceptions import APIError

        assert isinstance(APIError.status_code, property)


# --- Live: account and asset metadata ----------------------------------------


@_needs_live
class TestAccountRoundTrip:
    def test_get_account_returns_usable_numbers(self) -> None:
        from trading.data.alpaca_client import AccountSnapshot, RealAlpacaClient

        account = RealAlpacaClient().get_account()

        assert isinstance(account, AccountSnapshot)
        assert isinstance(account.cash, float)
        assert isinstance(account.equity, float)
        assert account.equity > 0.0

    def test_list_positions_returns_our_dtos(self) -> None:
        from trading.data.alpaca_client import PositionSnapshot, RealAlpacaClient

        positions = RealAlpacaClient().list_positions()

        assert isinstance(positions, list)
        for position in positions:
            assert isinstance(position, PositionSnapshot)
            assert position.symbol
            assert position.qty != 0.0

    def test_defaults_to_the_paper_endpoint(self) -> None:
        # The safety property, asserted against a live object rather than only the
        # signature default (ADR-0018).
        from alpaca.common.enums import BaseURL

        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        assert client._trading._base_url == BaseURL.TRADING_PAPER


@_needs_live
class TestGetAssetLive:
    def test_known_symbol_is_tradable_and_fractionable(self) -> None:
        from trading.data.alpaca_client import AssetInfo, RealAlpacaClient

        asset = RealAlpacaClient().get_asset(_SYMBOL)

        assert isinstance(asset, AssetInfo)
        assert asset.symbol == _SYMBOL
        assert asset.tradable
        assert asset.fractionable
        # The enum prefix really is stripped against the live response.
        assert asset.exchange == "NASDAQ"
        assert "." not in asset.exchange
        assert asset.name

    def test_unknown_symbol_raises_lookup_error(self) -> None:
        # ADR-0028's whole "unverified vs unusable" distinction depends on this
        # being a LookupError and not an opaque APIError.
        from trading.data.alpaca_client import RealAlpacaClient

        with pytest.raises(LookupError, match=_UNKNOWN):
            RealAlpacaClient().get_asset(_UNKNOWN)

    def test_the_404_branch_is_what_fires(self) -> None:
        # Pin the *mechanism*, not just the outcome: if Alpaca ever answered an
        # unknown ticker with, say, 422, the mapping would silently stop working
        # and the symbol would surface as unverified-by-exception instead.
        from alpaca.common.exceptions import APIError

        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        with pytest.raises(APIError) as caught:
            client._trading.get_asset(_UNKNOWN)
        assert caught.value.status_code == 404


@_needs_live
class TestUniverseVerification:
    """The first real broker verification of the curated baskets (ADR-0024/0028)."""

    @pytest.mark.parametrize("basket", ["blue20", "core10"])
    def test_curated_basket_is_fully_usable(self, basket: str) -> None:
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.universe import get_universe, validate_universe

        symbols = get_universe(basket)
        validation = validate_universe(symbols, RealAlpacaClient())

        assert validation.unverified == (), f"could not verify: {validation.unverified}"
        assert validation.unusable == (), f"broker refuses: {validation.unusable}"
        assert list(validation.usable) == list(symbols)
        assert validation.is_clean

    def test_an_unknown_symbol_lands_in_unverified_not_unusable(self) -> None:
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.universe import REASON_UNVERIFIED, validate_universe

        validation = validate_universe([_SYMBOL, _UNKNOWN], RealAlpacaClient())

        assert list(validation.usable) == [_SYMBOL]
        assert [d.symbol for d in validation.unverified] == [_UNKNOWN]
        assert validation.unverified[0].reason == REASON_UNVERIFIED
        assert validation.unusable == ()


# --- Live: bars, and the raw/adjusted split ----------------------------------


@_needs_live
class TestRealBars:
    def test_raw_and_adjusted_differ_across_a_split(self) -> None:
        # ADR-0021 is only meaningful if the two price notions actually diverge.
        # AAPL split 4:1 on 2020-08-31, so pre-split raw closes are ~4x adjusted.
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        start, end = datetime(2020, 8, 25, tzinfo=UTC), datetime(2020, 8, 28, tzinfo=UTC)

        adjusted = client.get_daily_bars(_SYMBOL, start, end, adjusted=True)
        raw = client.get_daily_bars(_SYMBOL, start, end, adjusted=False)

        assert adjusted and raw
        assert [b.ts for b in adjusted] == [b.ts for b in raw]
        assert raw[0].close > adjusted[0].close * 3.5

    def test_adapter_honors_the_per_call_adjusted_flag(self) -> None:
        from trading.data.alpaca_adapter import AlpacaAdapter

        adapter = AlpacaAdapter()
        start, end = datetime(2020, 8, 25, tzinfo=UTC), datetime(2020, 8, 28, tzinfo=UTC)

        assert adapter.get_bars(_SYMBOL, start, end, adjusted=False)[0].close > (
            adapter.get_bars(_SYMBOL, start, end, adjusted=True)[0].close * 3.5
        )

    def test_recent_sip_bars_are_refused_as_a_subscription_error(self) -> None:
        """A data-plan refusal is classified, not leaked as a raw SDK error.

        Regression test for the bug that made ``paper --broker alpaca --live``
        unusable: the live feed polls up to ``now``, and this account's plan
        answers HTTP 403 on the SIP tape for anything that recent. Skips rather
        than fails if the plan *does* cover recent SIP -- a paid plan is not a bug.
        """
        from trading.data.alpaca_client import DataSubscriptionError, RealAlpacaClient

        client = RealAlpacaClient()
        end = datetime.now(UTC)
        start = end - timedelta(hours=2)
        try:
            client.get_bars(_SYMBOL, start, end, adjusted=False, interval=timedelta(minutes=1))
        except DataSubscriptionError as exc:
            assert "--data-feed iex" in str(exc)
        else:
            pytest.skip("this account's data plan covers recent SIP bars")

    def test_iex_feed_serves_recent_bars(self) -> None:
        # The other half: what the live paper feed defaults to must actually work.
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient(feed="iex")
        end = datetime.now(UTC)
        bars = client.get_bars(
            _SYMBOL, end - timedelta(days=5), end, adjusted=False, interval=timedelta(minutes=1)
        )

        assert bars, "expected recent 1m IEX bars within a 5-day window"
        assert bars == sorted(bars, key=lambda b: b.ts)
        assert all(b.volume > 0 for b in bars)


# --- Live: the order lifecycle through the broker ----------------------------


@_needs_live
class TestOrderLifecycle:
    """Submit-then-poll against the real venue (ADR-0020).

    Which branch is exercised depends on the market state, and *both* are correct:
    open -> the order reaches a terminal status; closed -> it stays working past the
    poll timeout and is retried on a later bar. The test asserts whichever applies
    and says which, so a closed-market run is never mistaken for a pass of the
    fill path.
    """

    def test_tiny_market_order_settles_or_stays_pending(self) -> None:
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import STATUS_FILLED, RealAlpacaClient
        from trading.types import Bar, Order

        client = RealAlpacaClient()
        is_open = _market_is_open()
        broker = AlpacaBroker(client, clock=WallClock(), poll_timeout=timedelta(seconds=20))
        opening_qty = broker.portfolio.position(_SYMBOL).qty

        bar = Bar(
            symbol=_SYMBOL,
            ts=datetime.now(UTC),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1,
        )
        try:
            broker.submit(Order(_SYMBOL, Side.BUY, qty=_TINY_QTY))
            assert len(broker.pending_order_ids) == 1

            fills = broker.on_bar({_SYMBOL: bar})

            if is_open:
                # Market open: the venue fills a market order essentially at once.
                assert len(fills) == 1, "market is open; expected the order to fill"
                assert fills[0].symbol == _SYMBOL
                assert fills[0].side is Side.BUY
                assert fills[0].qty == pytest.approx(_TINY_QTY)
                assert fills[0].price > 0.0
                assert fills[0].commission == 0.0  # the venue's price, nothing added
                assert len(broker.pending_order_ids) == 0
                # Reconciled from the account, not simulated locally.
                assert broker.portfolio.position(_SYMBOL).qty == pytest.approx(
                    opening_qty + _TINY_QTY
                )
            else:
                # Market closed: a DAY order queues for the next open, so it is
                # still *working* at timeout and stays pending for a later bar --
                # documented, intended behaviour (ADR-0020), not a failure.
                assert fills == []
                assert len(broker.pending_order_ids) == 1
                order_id = broker.pending_order_ids[0]
                assert client.get_order(order_id).status != STATUS_FILLED
        finally:
            _flatten(client, _SYMBOL, opening_qty)

    def test_reconcile_reads_the_account_as_the_authority(self) -> None:
        # ADR-0020: the broker's portfolio is the account's, not a local simulation.
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        broker = AlpacaBroker(client, clock=WallClock())

        account = client.get_account()
        # Reconciliation happens at construction, before any bar.
        assert broker.portfolio.cash == pytest.approx(account.cash, rel=1e-6)
        held = {p.symbol: p.qty for p in client.list_positions()}
        assert {s: p.qty for s, p in broker.portfolio.positions.items()} == pytest.approx(held)


def _flatten(client: object, symbol: str, target_qty: float) -> None:
    """Sell back down to ``target_qty`` so the test leaves no position behind.

    Best-effort by design: if the market is closed the sell queues and the test
    should not fail for it, so a refusal is swallowed. Anything this opens is a
    hundredth of a share of paper money.
    """
    import time

    from trading.data.alpaca_client import TERMINAL_STATUSES, RealAlpacaClient

    assert isinstance(client, RealAlpacaClient)
    held = next((p.qty for p in client.list_positions() if p.symbol == symbol), 0.0)
    excess = held - target_qty
    if excess <= 0.0:
        return
    try:
        order = client.submit_order(symbol, excess, Side.SELL)
    except Exception:
        return
    for _ in range(10):
        if client.get_order(order.id).status in TERMINAL_STATUSES:
            return
        time.sleep(2)
