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
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from trading.types import Side

if TYPE_CHECKING:  # the wrapper's *name*, without importing it at collection time
    from trading.data.alpaca_client import RealAlpacaClient

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
# A symbol whose split Alpaca *does* back out of its adjusted series, used by
# the ADR-0021 raw-vs-adjusted tests. TSLA split 5:1 on 2020-08-31 -- the same
# session AAPL split 4:1, which Alpaca gets wrong (ADR-0045).
_SPLIT_SYMBOL = "TSLA"
_SPLIT_RATIO = 5.0
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

    def test_api_error_code_raises_when_the_body_carries_no_code(self) -> None:
        # What `_order_error_code` is defensive about, and why it cannot be a
        # `getattr(exc, "code", None)`: alpaca-py implements `.code` as
        # `json.loads(self._error)["code"]`, so a body without one *raises*
        # rather than returning None. That distinction is the whole classifier:
        # a refused order carries a code, an unauthorized request does not
        # (both bodies below are verbatim from the paper API, 2026-08-08).
        from alpaca.common.exceptions import APIError

        from trading.data.alpaca_client import _order_error_code

        # The SDK's constructor is untyped, and mypy runs twice -- once without the
        # extra (where `APIError` is Any) and once with it. Going through an
        # explicitly-Any alias types the same in both, so neither run needs an
        # ignore the other would call unused (ADR-0018's double-typecheck).
        make_error: Any = APIError
        refusal: Any = make_error('{"code":40310000,"message":"insufficient buying power"}')
        unauthorized: Any = make_error('{"message": "unauthorized."}')

        assert _order_error_code(refusal) == 40310000
        with pytest.raises(KeyError):
            _ = unauthorized.code
        assert _order_error_code(unauthorized) is None

    def test_the_queued_statuses_are_not_terminal(self) -> None:
        # The market-closed branch rests on this: a DAY order placed while the
        # venue is shut is parked as `accepted` (observed) or `new`, and both must
        # stay *working* so the poll waits and retries rather than dropping the
        # order (ADR-0020, ADR-0033).
        from alpaca.trading.enums import OrderStatus

        from trading.data.alpaca_client import TERMINAL_STATUSES

        for member in (OrderStatus.ACCEPTED, OrderStatus.NEW, OrderStatus.PENDING_NEW):
            assert member.value not in TERMINAL_STATUSES

    def test_cancel_order_by_id_is_the_sdk_call_the_wrapper_makes(self) -> None:
        from alpaca.trading.client import TradingClient

        assert callable(TradingClient.cancel_order_by_id)


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
    """Bars, and the raw-vs-adjusted split (ADR-0021).

    **History, because these two tests were red for a reason.** Both split tests
    below asserted against **AAPL**, and both went red on 2026-08-08 when Alpaca
    stopped applying AAPL's 2020-08-31 4:1 split to its adjusted series (499.30
    raw vs 484.31 adjusted, a ratio of 1.031 = dividends only). They were left
    failing on purpose, because weakening an assertion to match a broken provider
    hides an honesty regression.

    That finding now has its own slice. The defect is **one symbol's data, not
    the provider's pipeline** — measured 2026-08-09, TSLA 5:1, NVDA 10:1, GOOGL
    20:1, AMZN 20:1 and CMG 50:1 are all correctly adjusted, and only AAPL is
    not — so these two tests are retargeted at TSLA, which splits 5:1 on the very
    same 2020-08-31 date. They now test what they were always *for*: that the two
    price notions really do diverge across a corporate action, which is the only
    thing that makes ADR-0021 meaningful.

    The provider's own state is not swept under the carpet, it is stated in two
    louder places: ``tests/integration/test_alpaca_contract.py`` (nightly,
    ``network``-marked) carries a **strict xfail** that turns the nightly RED the
    day Alpaca fixes AAPL, and ``test_adjusted_aapl_is_refused_by_the_guard``
    below asserts our own refusal (ADR-0045). Nothing here is weaker than it was;
    the honesty moved to where it runs every night instead of only when someone
    happens to have credentials loaded.
    """

    def test_raw_and_adjusted_differ_across_a_split(self) -> None:
        # ADR-0021 is only meaningful if the two price notions actually diverge.
        # TSLA split 5:1 on 2020-08-31, so pre-split raw closes are ~5x adjusted.
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        start, end = datetime(2020, 8, 25, tzinfo=UTC), datetime(2020, 8, 28, tzinfo=UTC)

        adjusted = client.get_daily_bars(_SPLIT_SYMBOL, start, end, adjusted=True)
        raw = client.get_daily_bars(_SPLIT_SYMBOL, start, end, adjusted=False)

        assert adjusted and raw
        assert [b.ts for b in adjusted] == [b.ts for b in raw]
        assert raw[0].close > adjusted[0].close * 4.5

    def test_adapter_honors_the_per_call_adjusted_flag(self) -> None:
        from trading.data.alpaca_adapter import AlpacaAdapter

        adapter = AlpacaAdapter()
        start, end = datetime(2020, 8, 25, tzinfo=UTC), datetime(2020, 8, 28, tzinfo=UTC)

        assert adapter.get_bars(_SPLIT_SYMBOL, start, end, adjusted=False)[0].close > (
            adapter.get_bars(_SPLIT_SYMBOL, start, end, adjusted=True)[0].close * 4.5
        )

    def test_adjusted_aapl_is_refused_by_the_guard(self) -> None:
        """The provider defect, asserted as *our* behaviour rather than theirs.

        Stated as an if/else on the measured provider state so it is correct in
        both worlds: while Alpaca leaves AAPL's split in, the guard must refuse
        the window (ADR-0045); once Alpaca fixes it, the guard must fall silent.
        Neither branch is a skip, so this can never quietly stop meaning anything.
        """
        from trading.data.alpaca_adapter import AlpacaAdapter, UnadjustedSplitError
        from trading.data.alpaca_client import RealAlpacaClient

        start, end = datetime(2020, 8, 25, tzinfo=UTC), datetime(2020, 9, 4, tzinfo=UTC)
        client = RealAlpacaClient()
        adjusted = {b.ts: b for b in client.get_daily_bars(_SYMBOL, start, end, adjusted=True)}
        raw = {b.ts: b for b in client.get_daily_bars(_SYMBOL, start, end, adjusted=False)}
        shared = sorted(set(adjusted) & set(raw))
        pre = [ts for ts in shared if ts.date() < date(2020, 8, 31)][-1]
        post = next(ts for ts in shared if ts.date() >= date(2020, 8, 31))
        applied = (raw[pre].close / adjusted[pre].close) / (raw[post].close / adjusted[post].close)

        adapter = AlpacaAdapter()
        if abs(applied - 1.0) < 0.02:  # the split is still not backed out
            with pytest.raises(UnadjustedSplitError) as excinfo:
                adapter.get_bars(_SYMBOL, start, end, adjusted=True)
            assert "2020-08-31" in str(excinfo.value)
            assert "4:1" in str(excinfo.value)
        else:
            assert abs(applied - 4.0) < 0.08, f"unexpected adjustment factor {applied}"
            assert adapter.get_bars(_SYMBOL, start, end, adjusted=True)

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


# --- Live: the branch that only runs when the venue is shut -------------------


@_needs_live
class TestMarketClosedOrder:
    """The pending/timeout branch, driven against the real venue (ADR-0036).

    ADR-0033 shipped the terminal-status classification but recorded, honestly,
    that "the market-closed pending/timeout branch of the real broker" had never
    run: the first live session happened during market hours and took the fill
    path. Everything below needs the venue *shut*, which is a few hours a day and
    all weekend, so each test skips when the market is open rather than asserting
    a branch that cannot happen.

    What the venue actually does, observed 2026-08-08 (Saturday, next open Mon
    2026-08-10 09:30 ET): a fractional ``TimeInForce.DAY`` market order is
    accepted and parked at status ``accepted`` -- not rejected, not filled -- and
    stays there. ``accepted`` is *not* terminal, so the poll waits it out and
    leaves the order pending, which is exactly what ADR-0020 designed. Cancelling
    moved it to ``canceled`` in under a second.
    """

    def _require_closed(self) -> None:
        if _market_is_open():
            pytest.skip("this branch only exists while the venue is closed")

    def test_a_parked_order_stays_pending_past_a_clean_poll_timeout(self) -> None:
        """Submit -> parked non-terminal -> timeout returns cleanly -> still pending.

        The poll timeout is shortened from the 30s default purely to keep the test
        quick; the branch it drives is identical, and the default is asserted
        below so a change to it is still caught.
        """
        import time

        from trading.brokers.alpaca import DEFAULT_POLL_TIMEOUT, AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import TERMINAL_STATUSES, RealAlpacaClient
        from trading.types import Bar, Order

        assert timedelta(seconds=30) == DEFAULT_POLL_TIMEOUT  # what live runs use

        self._require_closed()
        client = RealAlpacaClient()
        poll_timeout = timedelta(seconds=6)
        broker = AlpacaBroker(
            client,
            clock=WallClock(),
            poll_timeout=poll_timeout,
            poll_interval=timedelta(seconds=2),
        )
        opening_qty = broker.portfolio.position(_SYMBOL).qty
        bar = Bar(
            symbol=_SYMBOL, ts=datetime.now(UTC), open=1.0, high=1.0, low=1.0, close=1.0, volume=1
        )

        try:
            broker.submit(Order(_SYMBOL, Side.BUY, qty=_TINY_QTY))
            assert len(broker.pending_order_ids) == 1
            order_id = broker.pending_order_ids[0]

            # The venue parked it rather than filling or refusing it.
            parked = client.get_order(order_id)
            assert parked.status not in TERMINAL_STATUSES, (
                f"expected a working status with the market closed, got {parked.status!r}"
            )
            assert parked.filled_qty == 0.0
            assert parked.filled_avg_price is None

            # The timeout fires cleanly: no hang, no raise, no fill, no eviction.
            started = time.monotonic()
            fills = broker.on_bar({_SYMBOL: bar})
            elapsed = time.monotonic() - started

            assert fills == []
            assert broker.rejections == []
            assert broker.pending_order_ids == (order_id,)
            assert elapsed >= poll_timeout.total_seconds()
            assert elapsed < poll_timeout.total_seconds() + 15.0, "polling overran its timeout"
            # Still parked afterwards -- polling is a read, it does not disturb it.
            assert client.get_order(order_id).status not in TERMINAL_STATUSES
        finally:
            _flatten(client, _SYMBOL, opening_qty)

    def test_cancelling_a_parked_order_settles_it_and_evicts_the_id(self) -> None:
        """The other half: ``canceled`` IS terminal, so the next bar drops the id.

        This is the whole point of ADR-0033's classification, run for real: before
        it, a canceled order stayed in the pending set and burned the full poll
        timeout on every subsequent bar for the rest of the session.
        """
        import time

        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import (
            STATUS_CANCELED,
            TERMINAL_STATUSES,
            RealAlpacaClient,
        )
        from trading.types import Bar, Order

        self._require_closed()
        client = RealAlpacaClient()
        broker = AlpacaBroker(
            client,
            clock=WallClock(),
            poll_timeout=timedelta(seconds=4),
            poll_interval=timedelta(seconds=2),
        )
        opening_qty = broker.portfolio.position(_SYMBOL).qty
        bar = Bar(
            symbol=_SYMBOL, ts=datetime.now(UTC), open=1.0, high=1.0, low=1.0, close=1.0, volume=1
        )
        submitted = Order(_SYMBOL, Side.BUY, qty=_TINY_QTY)

        try:
            broker.submit(submitted)
            order_id = broker.pending_order_ids[0]

            client.cancel_order(order_id)
            for _ in range(10):  # the venue cancels asynchronously
                if client.get_order(order_id).status == STATUS_CANCELED:
                    break
                time.sleep(1)
            assert client.get_order(order_id).status == STATUS_CANCELED
            assert STATUS_CANCELED in TERMINAL_STATUSES

            # The next bar settles it on the *first* poll: no timeout is burned.
            started = time.monotonic()
            fills = broker.on_bar({_SYMBOL: bar})
            elapsed = time.monotonic() - started

            assert fills == []  # nothing filled, so nothing to blotter
            assert broker.pending_order_ids == ()  # the id is evicted
            assert elapsed < 4.0, "a terminal order must settle without waiting out the timeout"

            # Reported, not silently dropped -- and shaped for result.json (ADR-0036).
            assert len(broker.rejections) == 1
            order, reason = broker.rejections[0]
            assert order == submitted
            assert STATUS_CANCELED in reason
            assert order_id in reason

            # A later bar does no further work on it.
            assert broker.on_bar({_SYMBOL: bar}) == []
            assert len(broker.rejections) == 1
        finally:
            _flatten(client, _SYMBOL, opening_qty)

    def test_cancelling_an_already_terminal_order_is_accepted(self) -> None:
        # Pins what the fake mimics: the venue answers a repeat cancel with 200,
        # so `cancel_order` is idempotent and cleanup can call it unconditionally.
        import time

        from trading.data.alpaca_client import STATUS_CANCELED, RealAlpacaClient

        self._require_closed()
        client = RealAlpacaClient()
        placed = client.submit_order(_SYMBOL, _TINY_QTY, Side.BUY)
        try:
            client.cancel_order(placed.id)
            for _ in range(10):
                if client.get_order(placed.id).status == STATUS_CANCELED:
                    break
                time.sleep(1)
            assert client.get_order(placed.id).status == STATUS_CANCELED

            client.cancel_order(placed.id)  # must not raise

            assert client.get_order(placed.id).status == STATUS_CANCELED
        finally:
            _flatten(client, _SYMBOL, 0.0)

    def test_cancelling_an_unknown_order_raises_lookup_error(self) -> None:
        # The 404 -> LookupError mapping, the same distinction get_asset draws:
        # "we never heard of it" is not "we could not ask".
        import uuid

        from trading.data.alpaca_client import RealAlpacaClient

        with pytest.raises(LookupError):
            RealAlpacaClient().cancel_order(str(uuid.uuid4()))

    def test_the_session_leaves_no_working_orders_behind(self) -> None:
        # The account-hygiene guard: a parked order that outlives a test fills at
        # the next open, hours later, and looks like a phantom trade.
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        _cancel_open_orders(client, _SYMBOL)

        assert _open_order_ids(client, _SYMBOL) == []


@_needs_live
class TestDuplicateOrderGuardLive:
    """The duplicate-order guard against the real venue (ADR-0036 amended, KAN-669).

    Needs the venue *shut*, because that is what parks an order long enough to be
    duplicated: while it sits ``accepted``, the account -- and therefore the
    portfolio the broker reconciles from -- reads flat, so a target-weight strategy
    asks again on the next bar. The fast layer proves the guard deterministically;
    this proves the *condition* it guards against is the venue's real behaviour,
    and that only one order actually reaches the account.

    Executed for the first time on 2026-08-08 (Saturday, next open Mon 09:30 ET),
    and it did not pass as written. Two things the offline fake had wrong:

    * The venue happily accepts a **duplicate** same-side order -- so the guard is
      the only thing standing between a parked order and a compounding stack, as
      ADR-0036 assumed but had never checked (:meth:`test_the_venue_itself_is_no_backstop`).
    * The venue **refuses the opposite side** while an order is working::

          HTTP 403 {"code":40310000,"existing_order_id":"a182da86-...",
                    "message":"potential wash trade detected. use complex orders",
                    "reject_reason":"opposite side market/stop order exists"}

      so "a working BUY can never block a SELL" holds for *this bench's* guard and
      not for the system. Worse, the raw ``APIError`` used to escape
      ``AlpacaBroker.submit`` and kill the session; ADR-0041 classifies it.
    """

    def _require_closed(self) -> None:
        if _market_is_open():
            pytest.skip("an order only parks long enough to be duplicated when closed")

    def test_a_parked_order_is_not_duplicated_at_the_venue(self) -> None:
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.types import Bar, Order

        self._require_closed()
        client = RealAlpacaClient()
        broker = AlpacaBroker(
            client,
            clock=WallClock(),
            # Zero timeout: one poll sees the parked order, on_bar returns at once.
            poll_timeout=timedelta(seconds=0),
            poll_interval=timedelta(seconds=1),
        )
        opening_qty = broker.portfolio.position(_SYMBOL).qty
        before = set(_open_order_ids(client, _SYMBOL))
        bar = Bar(
            symbol=_SYMBOL, ts=datetime.now(UTC), open=1.0, high=1.0, low=1.0, close=1.0, volume=1
        )

        try:
            # Three bars of the same unmet intent, which is what a target-weight
            # strategy emits while the account reads flat.
            for _ in range(3):
                broker.submit(Order(_SYMBOL, Side.BUY, qty=_TINY_QTY))
                broker.on_bar({_SYMBOL: bar})

            assert len(broker.pending_order_ids) == 1
            order_id = broker.pending_order_ids[0]
            # The venue's own view: one new working order in the symbol, not three.
            assert set(_open_order_ids(client, _SYMBOL)) - before == {order_id}
            # Both refusals name the order that is still working.
            assert len(broker.rejections) == 2
            assert all(order_id in reason for (_order, reason) in broker.rejections)
            assert all("still working at the venue" in r for (_o, r) in broker.rejections)
        finally:
            _flatten(client, _SYMBOL, opening_qty)

    def test_the_venue_itself_is_no_backstop_against_duplicates(self) -> None:
        """Two identical orders, straight at the client: Alpaca accepts both.

        ADR-0036's premise, checked instead of assumed. The guard is bypassed here
        deliberately -- this is a statement about the *venue*, and it is the reason
        the guard has to exist at all. If Alpaca ever started rejecting the
        duplicate itself this would go red, which is the notification we want.
        """
        from trading.data.alpaca_client import RealAlpacaClient

        self._require_closed()
        client = RealAlpacaClient()
        placed = []
        try:
            for _ in range(2):
                placed.append(client.submit_order(_SYMBOL, _TINY_QTY, Side.BUY))

            assert len({o.id for o in placed}) == 2, "the venue issued one id for two orders"
            for order in placed:
                assert order.status not in ("rejected",)
                assert client.get_order(order.id).status not in ("rejected",)
            # Both are genuinely working at the account, not deduplicated for us.
            assert {o.id for o in placed} <= set(_open_order_ids(client, _SYMBOL))
        finally:
            _flatten(client, _SYMBOL, 0.0)

    def test_the_venue_refuses_the_exit_our_guard_deliberately_allows(self) -> None:
        """The finding: our guard lets the SELL through, and Alpaca refuses it.

        ADR-0036's amendment promised "a working BUY can never block a SELL". That
        is still true of the guard -- the refusal is keyed on side, so an exit is
        never even compared against a working entry -- and this test asserts it by
        checking the rejection is *not* ours. But the venue has a wash-trade rule
        of its own, so the exit does not reach the book while an entry is parked.

        The bench's obligation is to report that honestly rather than die on it,
        which is exactly what ADR-0041 makes it do: the refusal is recorded on
        ``rejections`` (and so reaches ``result.json``), the parked BUY is
        untouched, and the session carries on.
        """
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.types import Order

        self._require_closed()
        client = RealAlpacaClient()
        broker = AlpacaBroker(
            client,
            clock=WallClock(),
            poll_timeout=timedelta(seconds=0),
            poll_interval=timedelta(seconds=1),
        )
        opening_qty = broker.portfolio.position(_SYMBOL).qty

        try:
            broker.submit(Order(_SYMBOL, Side.BUY, qty=_TINY_QTY))
            assert len(broker.pending_order_ids) == 1
            buy_id = broker.pending_order_ids[0]

            # Must not raise: before ADR-0041 this line ended the session.
            broker.submit(Order(_SYMBOL, Side.SELL, qty=_TINY_QTY))

            assert len(broker.rejections) == 1
            _order, reason = broker.rejections[0]
            # Not our duplicate guard -- the guard is side-keyed and stayed out of it.
            assert "still working at the venue" not in reason
            # The venue's own words, carried through verbatim.
            assert "Alpaca refused sell" in reason
            assert "wash trade" in reason or "opposite side" in reason
            # The parked BUY is untouched: still exactly one order at the venue.
            assert broker.pending_order_ids == (buy_id,)
            assert _open_order_ids(client, _SYMBOL) == [buy_id]
        finally:
            _flatten(client, _SYMBOL, opening_qty)

    def test_a_refused_submit_leaves_nothing_pending(self) -> None:
        """A refusal is not a phantom order: the broker must not start polling one.

        Driven with a refusal that needs no parked order to provoke -- selling from
        a flat account is a short, which this bench forbids (ADR-0011) and the
        venue refuses with ``422 {"code":42210000,"message":"fractional orders
        cannot be sold short"}``. It is the same submit-time path.
        """
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.types import Bar, Order

        self._require_closed()
        client = RealAlpacaClient()
        if any(p.symbol == _SYMBOL for p in client.list_positions()):
            pytest.skip(f"needs a flat book in {_SYMBOL} for the short to be refused")
        broker = AlpacaBroker(client, clock=WallClock(), poll_timeout=timedelta(seconds=0))
        bar = Bar(
            symbol=_SYMBOL, ts=datetime.now(UTC), open=1.0, high=1.0, low=1.0, close=1.0, volume=1
        )

        broker.submit(Order(_SYMBOL, Side.SELL, qty=_TINY_QTY))

        assert len(broker.rejections) == 1
        assert broker.pending_order_ids == ()
        assert broker.on_bar({_SYMBOL: bar}) == []  # nothing to poll, nothing filled

    def test_a_credential_failure_is_not_recorded_as_a_venue_refusal(self) -> None:
        """The other half of ADR-0041's classification, against the real API.

        A bad key answers ``401 {"message": "unauthorized."}`` with no error code,
        so it must reach the caller rather than be logged as the venue's verdict on
        an order. Runs against the live endpoint precisely because the classifier
        turns on a detail of the response body -- the absence of ``code`` -- that
        only the real API can confirm.
        """
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import OrderRejectedError, RealAlpacaClient
        from trading.types import Order

        good = RealAlpacaClient()
        bad = RealAlpacaClient(api_key="NOTAREALKEY", secret_key="NOTAREALSECRET")
        # Construct the broker on a working client, then swap in the broken one, so
        # the up-front reconcile succeeds and only `submit` faces the bad key.
        broker = AlpacaBroker(good, clock=WallClock(), poll_timeout=timedelta(seconds=0))
        broker._client = bad

        with pytest.raises(Exception) as caught:
            broker.submit(Order(_SYMBOL, Side.BUY, qty=_TINY_QTY))

        assert not isinstance(caught.value, OrderRejectedError)
        assert getattr(caught.value, "status_code", None) == 401
        assert broker.rejections == []
        assert broker.pending_order_ids == ()

    def test_the_account_is_left_flat(self) -> None:
        """The final read: no working orders and no position, from the venue itself.

        Declared after the test above so it runs after it. A parked order that
        outlives the run fills at the next open, hours later, and looks like a
        phantom trade; a position that outlives it silently changes what every
        later test starts from. ``_flatten`` already cancels then sells, so this
        asserts the outcome rather than performing it -- and it holds on both
        branches: if the venue opened mid-test the buy filled and was sold back.
        The paper account this suite runs against is kept flat by convention
        (ADR-0036 left it at $100,000.06 cash, checked rather than assumed).
        """
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        _cancel_open_orders(client, _SYMBOL)

        assert _open_order_ids(client, _SYMBOL) == []
        assert [p for p in client.list_positions() if p.symbol == _SYMBOL] == []


def _flatten(client: object, symbol: str, target_qty: float) -> None:
    """Leave the account as we found it: no working orders, no extra shares.

    Cancelling first is not optional. A market order placed while the venue is
    closed is *parked* rather than filled (see :class:`TestMarketClosedOrder`), so
    a test that only sells back what it can see leaves a queued buy behind that
    fills at the next open -- a stray position appearing hours after the test
    passed. Selling is best-effort after that: if the market is closed the sell
    queues too, and the test should not fail for it. Anything this opens is a
    hundredth of a share of paper money.
    """
    import time

    from trading.data.alpaca_client import TERMINAL_STATUSES, RealAlpacaClient

    assert isinstance(client, RealAlpacaClient)
    _cancel_open_orders(client, symbol)
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
    _cancel_open_orders(client, symbol)


def _cancel_open_orders(client: object, symbol: str) -> None:
    """Cancel every still-working order in ``symbol``, through the seam (ADR-0036)."""
    import contextlib

    for order_id in _open_order_ids(client, symbol):
        # Already terminal, or the venue refused: not this test's failure.
        with contextlib.suppress(Exception):
            _as_real(client).cancel_order(order_id)


def _open_order_ids(client: object, symbol: str) -> list[str]:
    """Ids of the venue's currently-open orders in ``symbol``.

    ``get_orders`` is typed ``list[Order] | list[str]`` by the SDK (the raw-data
    arm ``RealAlpacaClient`` never asks for), so the string arm is skipped rather
    than assumed away -- the same stance :func:`_require_model` takes.
    """
    return [
        str(raw.id)
        for raw in _as_real(client)._trading.get_orders()
        if not isinstance(raw, str) and str(raw.symbol) == symbol
    ]


def _as_real(client: object) -> RealAlpacaClient:
    """Narrow a client to the real wrapper (these helpers only ever get one)."""
    from trading.data.alpaca_client import RealAlpacaClient

    assert isinstance(client, RealAlpacaClient)
    return client
