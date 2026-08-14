"""Live order-path checks against Alpaca's crypto paper venue (ADR-0058).

**These tests place real (paper) orders and they cost money to place**, in the
sense that the venue enforces a **$10 minimum notional** — measured, not guessed:
``0.000155 BTC`` (~$9.73) is refused and ``0.00016`` (~$10.05) is accepted. So
unlike the equity live tests, which could work in $3 fractional shares, the
smallest honest order here is about $11. Every test that opens a position closes
it in a fixture, and :class:`TestAccountIsLeftFlat` asserts the account really is
flat at the end.

Unlike the equity venue there is **no market-closed branch to wait for**: crypto
trades 24/7, so these run at any hour and orders fill immediately. That is the
whole reason this file can assert things ``test_alpaca_live.py`` could not — a
*filled* order and a real reconciliation, not a parked one.

Two defects these tests were written to catch first, both measured on
2026-08-14 and both fatal to a crypto session:

* ``TimeInForce.DAY`` is refused ``422``/``42210000`` *"invalid crypto
  time_in_force"*. ADR-0041's classifier turns that into a tidy
  :class:`OrderRejectedError`, so the failure mode was a session that rejected
  **every** order while looking perfectly well behaved.
* A ``BTC/USD`` fill creates a position the venue calls ``BTCUSD``. Reconciled
  under that key the holding is invisible to sizing and the guardrails, so a
  target-weight run buys the same coin every bar until the cash is gone.

Layering per ADR-0040: ``integration`` **and** ``network``, so the required job
never runs it. PAPER KEYS ONLY.
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from trading.brokers.alpaca import AlpacaBroker
from trading.calendar import CRYPTO_24_7
from trading.clock import WallClock
from trading.data.alpaca_client import (
    ASSET_CLASS_CRYPTO,
    OrderRejectedError,
    RealAlpacaClient,
    time_in_force_for,
)
from trading.types import Bar, Order, Side

_HAVE_CREDS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_HAVE_SDK = importlib.util.find_spec("alpaca") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.network,
    pytest.mark.skipif(
        not (_HAVE_CREDS and _HAVE_SDK),
        reason=(
            "needs ALPACA_API_KEY / ALPACA_SECRET_KEY (paper only) and the alpaca-py "
            "SDK (uv sync --extra alpaca)"
        ),
    ),
]

SYMBOL = "BTC/USD"
FLAT_SYMBOL = "BTCUSD"

# Comfortably over the venue's $10 notional floor at any plausible BTC price, and
# small enough that a stranded position is a rounding error on a paper account.
TEST_QTY = 0.0002

# Under the floor at any plausible BTC price: ~$1.26 at $63k, ~$2 at $100k.
BELOW_FLOOR_QTY = 0.00002


@pytest.fixture
def client() -> RealAlpacaClient:
    return RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)


@pytest.fixture(autouse=True)
def flat_account(client: RealAlpacaClient) -> Iterator[None]:
    """Leave the account exactly as flat as it was found (ADR-0041's discipline)."""
    yield
    _flatten(client)


def _flatten(client: RealAlpacaClient) -> None:
    """Cancel anything working in the test symbol, then sell any position back."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    for order in client._trading.get_orders():
        if _field(order, "symbol") in {SYMBOL, FLAT_SYMBOL}:
            client.cancel_order(_field(order, "id"))
    for _ in range(10):
        held = next((p for p in client.list_positions() if p.symbol == SYMBOL), None)
        if held is None:
            return
        client._trading.submit_order(
            MarketOrderRequest(
                symbol=SYMBOL,
                qty=held.qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce(time_in_force_for(ASSET_CLASS_CRYPTO)),
            )
        )
        time.sleep(2)


def _field(sdk_object: object, name: str) -> str:
    """Read one field off an SDK model as a string.

    alpaca-py types its list responses as ``list[Model | str]`` (the raw-data mode
    this bench never enables), so a plain attribute access does not type-check.
    These tests read the SDK **directly and deliberately** — they are asserting
    what the venue reports, next to what the seam turns it into — so they pay the
    same defensive-read tax ``_to_asset`` and ``_to_order`` do inside the seam.
    """
    return str(getattr(sdk_object, name, ""))


def _bar(price: float = 60_000.0) -> Bar:
    return Bar(
        symbol=SYMBOL,
        ts=datetime.now(UTC).replace(microsecond=0),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
    )


class TestTimeInForce:
    """The refusal that made every crypto order fail, and the duration that works."""

    def test_the_day_duration_the_equity_path_uses_is_refused(
        self, client: RealAlpacaClient
    ) -> None:
        """Watch the guard fail: this is the bug, reproduced against the venue.

        Reproduced through the raw SDK rather than the seam, because the seam no
        longer *can* send ``DAY`` on crypto — which is the fix. If this ever stops
        raising, Alpaca has started accepting ``DAY`` and
        :data:`~trading.data.alpaca_client._TIME_IN_FORCE` could be simplified;
        until then it is the reason the table exists.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        with pytest.raises(Exception) as excinfo:
            client._trading.submit_order(
                MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=TEST_QTY,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
        assert "time_in_force" in str(excinfo.value)
        assert getattr(excinfo.value, "status_code", None) == 422

    def test_the_seam_sends_gtc_and_the_order_is_accepted(self, client: RealAlpacaClient) -> None:
        placed = client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        assert placed.id
        assert placed.symbol == SYMBOL, "the order echoes the canonical slash form"
        assert placed.side is Side.BUY

    def test_the_refusal_would_have_been_classified_not_crashed(self) -> None:
        """Why the bug was *quiet*: 422 + an Alpaca code is a textbook ADR-0041 refusal.

        The failure mode was never a traceback. Every order would have been
        recorded as a legible ``(Order, reason)`` rejection carrying the venue's
        own words, reached ``result.json``, and printed in the summary — while the
        session traded nothing at all.
        """
        from trading.data.alpaca_client import _classify_order_error

        fake = type(
            "APIErrorLike",
            (Exception,),
            {"status_code": 422, "code": 42210000},
        )('{"code":42210000,"message":"invalid crypto time_in_force"}')
        classified = _classify_order_error(fake, SYMBOL, TEST_QTY, Side.BUY)
        assert isinstance(classified, OrderRejectedError)
        assert "invalid crypto time_in_force" in str(classified)


class TestPositionSymbolCanonicalization:
    """The venue disagrees with itself, and the seam papers over it deliberately."""

    def test_a_fill_creates_a_position_under_the_concatenated_symbol(
        self, client: RealAlpacaClient
    ) -> None:
        """The raw venue behaviour, asserted so the workaround has a stated cause."""
        client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        time.sleep(3)
        raw = [_field(p, "symbol") for p in client._trading.get_all_positions()]
        assert FLAT_SYMBOL in raw, f"expected the venue's concatenated form, got {raw}"
        assert SYMBOL not in raw, "the venue reported the slash form; ADR-0058's premise moved"

    def test_the_seam_reports_it_under_the_symbol_the_bars_use(
        self, client: RealAlpacaClient
    ) -> None:
        """Which is the whole point: one key for bars, orders, sizing and guardrails."""
        client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        time.sleep(3)
        symbols = [p.symbol for p in client.list_positions()]
        assert SYMBOL in symbols, f"list_positions did not canonicalize: {symbols}"
        assert FLAT_SYMBOL not in symbols


class TestBrokerRoundTrip:
    """A real fill through the Broker seam, reconciled — what equities could not run."""

    def test_a_crypto_order_fills_and_reconciles_into_the_portfolio(
        self, client: RealAlpacaClient
    ) -> None:
        broker = AlpacaBroker(client, clock=WallClock(), calendar=CRYPTO_24_7)
        opening_cash = broker.portfolio.cash

        broker.submit(Order(symbol=SYMBOL, qty=TEST_QTY, side=Side.BUY))
        fills = broker.on_bar({SYMBOL: _bar()})

        assert broker.rejections == [], broker.rejections
        assert len(fills) == 1, f"expected one fill, got {fills}"
        fill = fills[0]
        assert fill.symbol == SYMBOL
        assert fill.side is Side.BUY
        assert fill.qty == pytest.approx(TEST_QTY, rel=1e-6)
        assert fill.price > 0

        held = broker.portfolio.positions.get(SYMBOL)
        assert held is not None, (
            "the portfolio has no position under the traded symbol — this is the "
            f"reconciliation defect ADR-0058 fixes; keys are {list(broker.portfolio.positions)}"
        )
        assert broker.portfolio.cash < opening_cash

    def test_the_position_the_account_credits_is_smaller_than_the_reported_fill(
        self, client: RealAlpacaClient
    ) -> None:
        """Alpaca's paper crypto venue charges ~25 bps, taken in the received asset.

        ``filled_qty`` is reported **gross**, so the ``Fill`` this bench records
        overstates what the account actually received and its ``commission`` is
        ``0.0``. Recorded, not corrected (ADR-0058): nothing in the order carries
        the fee, and inventing a constant from one measurement is exactly the
        re-tuning ADR-0052 refused. It matters most to KAN-710 — a 5 bps
        slippage-only cost model against a venue charging ~25 bps plus slippage.
        """
        before = next((p.qty for p in client.list_positions() if p.symbol == SYMBOL), 0.0)
        placed = client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        time.sleep(3)
        settled = client.get_order(placed.id)
        after = next((p.qty for p in client.list_positions() if p.symbol == SYMBOL), 0.0)

        received = after - before
        assert settled.filled_qty == pytest.approx(TEST_QTY, rel=1e-6)
        assert received < settled.filled_qty, (
            "the venue no longer takes a fee in the received asset; ADR-0058's "
            f"25 bps note is stale (gross={settled.filled_qty} received={received})"
        )
        fee_bps = (1 - received / settled.filled_qty) * 10_000
        assert 10 < fee_bps < 60, f"fee moved materially from the measured ~25 bps: {fee_bps:.1f}"


class TestNotionalFloor:
    """Why no client-side minimum-size gate was built (ADR-0058)."""

    def test_an_order_below_the_ten_dollar_floor_is_refused_legibly(
        self, client: RealAlpacaClient
    ) -> None:
        """Refused with the venue's own words, through ADR-0041's classifier.

        Never silent: this reaches :attr:`AlpacaBroker.rejections` as an
        ``(Order, reason)`` pair, and from there ``result.json`` and the summary.
        """
        with pytest.raises(OrderRejectedError, match="cost basis must be"):
            client.submit_order(SYMBOL, BELOW_FLOOR_QTY, Side.BUY)

    def test_the_published_minimum_is_far_below_the_binding_floor(
        self, client: RealAlpacaClient
    ) -> None:
        """The measurement that made a metadata-based gate the wrong design.

        ``min_order_size`` for BTC/USD is ~1.57e-05 (~$1 of notional), an order of
        magnitude under the $10 the venue actually enforces. A gate on the
        published number would wave through orders the venue refuses — a false
        negative dressed as a safety check — so the venue's refusal is left to be
        the answer.
        """
        asset = client.get_asset(SYMBOL)
        assert asset.min_order_size is not None
        assert asset.min_order_size < BELOW_FLOOR_QTY, (
            "the published minimum now exceeds an order the venue refuses, i.e. it "
            f"has become the binding constraint after all: {asset.min_order_size}"
        )

    def test_a_refused_order_reaches_the_brokers_rejection_list(
        self, client: RealAlpacaClient
    ) -> None:
        broker = AlpacaBroker(client, clock=WallClock(), calendar=CRYPTO_24_7)
        broker.submit(Order(symbol=SYMBOL, qty=BELOW_FLOOR_QTY, side=Side.BUY))

        assert broker.pending_order_ids == (), "a refused order must not be pending"
        assert len(broker.rejections) == 1
        order, reason = broker.rejections[0]
        assert order.symbol == SYMBOL
        assert "cost basis" in reason


class TestQuantityPrecision:
    """The venue truncates rather than refusing — so we must not round either."""

    def test_a_sizer_shaped_quantity_is_accepted_and_truncated_to_nine_decimals(
        self, client: RealAlpacaClient
    ) -> None:
        """The target-weight sizer emits full float precision; the venue copes.

        ``min_trade_increment`` is 1e-09 and the venue rounds to it silently. Were
        it ever to refuse instead, **every** order this bench sends would fail, so
        this is pinned rather than trusted.
        """
        qty = 0.00021739130434782607
        placed = client.submit_order(SYMBOL, qty, Side.BUY)
        assert placed.qty == pytest.approx(round(qty, 9), rel=1e-9)
        assert placed.qty != qty, "expected the venue to truncate to nine decimals"


class TestDuplicateGuardOnACryptoPair:
    """ADR-0036's guard was written with no market in mind; check it holds here."""

    def test_a_second_working_buy_is_refused_by_our_guard(self, client: RealAlpacaClient) -> None:
        """On a venue that fills instantly this rarely triggers — assert it can.

        The first order usually settles before the second is submitted, in which
        case there is nothing to duplicate and both go through legitimately. Either
        outcome is correct; what must never happen is a *silent* drop.
        """
        broker = AlpacaBroker(client, clock=WallClock(), calendar=CRYPTO_24_7)
        broker.submit(Order(symbol=SYMBOL, qty=TEST_QTY, side=Side.BUY))
        broker.submit(Order(symbol=SYMBOL, qty=TEST_QTY, side=Side.BUY))

        submitted = len(broker.pending_order_ids)
        refused = len(broker.rejections)
        assert submitted + refused == 2, "an order went missing entirely"
        if refused:
            assert "still working at the venue" in broker.rejections[0][1]


class TestAccountIsLeftFlat:
    """Runs last by file order; the fixture has already flattened each test."""

    def test_no_positions_and_no_working_orders_remain(self, client: RealAlpacaClient) -> None:
        _flatten(client)
        assert [p.symbol for p in client.list_positions()] == []
        working = [_field(o, "symbol") for o in client._trading.get_orders()]
        assert working == [], f"working orders left behind: {working}"

    def test_the_account_still_has_its_cash(self, client: RealAlpacaClient) -> None:
        account = client.get_account()
        assert account.cash > 50_000, f"paper account unexpectedly drained: {account.cash}"
        assert account.equity > 50_000


class TestSessionEndingOrdersNeverExpire:
    """A consequence of GTC that has no equity analogue, asserted not assumed."""

    def test_a_crypto_order_carries_no_expiry(self, client: RealAlpacaClient) -> None:
        """On equities an unfilled DAY order expires at the close (ADR-0036).

        Crypto orders are GTC because the venue refuses DAY, so an unfilled one
        stays working indefinitely — a session that ends with a working crypto
        order leaves it working. Flattening is manual, as the runbook already says
        for positions (ADR-0052); here it is also true of orders.
        """
        placed = client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        settled = client.get_order(placed.id)
        assert settled.status not in {"expired"}
        # Cleanup is the autouse fixture's job; assert the seam can take it back.
        client.cancel_order(placed.id)


class TestCancelIsIdempotentOnAFilledOrder:
    """ADR-0036's contract, broken by a case it never got to execute (ADR-0058)."""

    def test_the_venue_refuses_to_cancel_a_filled_order(self, client: RealAlpacaClient) -> None:
        """The raw behaviour: ``422``/``42210000`` *order is already in "filled" state*.

        ADR-0036 established "cancelling an already-terminal order succeeds
        silently" against the equity venue **with the market shut**, so the only
        terminal state it could reach was ``canceled``. A filled order was never
        cancelled until crypto made an on-demand fill possible. Asserted here
        against the SDK directly, because the seam now absorbs it.
        """
        placed = client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        time.sleep(3)
        assert client.get_order(placed.id).status == "filled"

        with pytest.raises(Exception) as excinfo:
            client._trading.cancel_order_by_id(placed.id)
        assert "already in" in str(excinfo.value)
        assert getattr(excinfo.value, "status_code", None) == 422

    def test_the_seam_absorbs_it_and_stays_idempotent(self, client: RealAlpacaClient) -> None:
        """The documented contract, restored — and it matters to a real operator.

        The runbook tells an operator to flatten a session's book by hand
        (ADR-0052). Doing that over a list of order ids hits a filled one
        immediately, and before this the flatten died on the first fill.
        """
        placed = client.submit_order(SYMBOL, TEST_QTY, Side.BUY)
        time.sleep(3)
        assert client.get_order(placed.id).status == "filled"

        client.cancel_order(placed.id)  # must not raise
        client.cancel_order(placed.id)  # ...twice

    def test_an_unknown_order_id_still_raises_lookup_error(self, client: RealAlpacaClient) -> None:
        """The absorption must not swallow "we never heard of it" (ADR-0028)."""
        with pytest.raises(LookupError):
            client.cancel_order("00000000-0000-0000-0000-000000000000")
