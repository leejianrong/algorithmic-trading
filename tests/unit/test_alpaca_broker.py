"""Fast, offline tests for the Alpaca paper broker (ADR-0020).

Everything here runs against :class:`FakeAlpacaClient` + :class:`FakeClock`: no
network, no key, no real wall clock. The real SDK wrapper never runs in the fast
layer, so this module deliberately does NOT import ``alpaca`` or construct
``RealAlpacaClient``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.brokers.alpaca import AlpacaBroker
from trading.clock import FakeClock
from trading.data.alpaca_client import AlpacaOrder, FakeAlpacaClient
from trading.data.fake import FakeAdapter
from trading.engine import BacktestResult, Engine
from trading.interfaces import Broker
from trading.strategies.equal_weight import EqualWeight
from trading.types import Bar, Order, Side


def _bar(symbol: str, close: float) -> Bar:
    ts = datetime(2026, 1, 2, tzinfo=UTC)
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close, close=close, volume=1_000)


def _series(symbol: str, closes: list[float]) -> list[Bar]:
    out: list[Bar] = []
    for i, c in enumerate(closes):
        ts = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(days=i)
        out.append(Bar(symbol=symbol, ts=ts, open=c, high=c, low=c, close=c, volume=1_000))
    return out


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 2, 21, 0, tzinfo=UTC))


def test_satisfies_broker_protocol() -> None:
    client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, cash=10_000.0)
    broker = AlpacaBroker(client, clock=_clock())
    assert isinstance(broker, Broker)


def test_reconciles_on_construction_before_any_bar() -> None:
    client = FakeAlpacaClient(cash=25_000.0)
    broker = AlpacaBroker(client, clock=_clock())
    # Portfolio is valid and reflects the account before the first on_bar.
    assert broker.portfolio.cash == pytest.approx(25_000.0)
    assert broker.portfolio.positions == {}


def test_submit_then_on_bar_fills_and_reconciles_from_account() -> None:
    client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, cash=10_000.0)
    client.set_price("AAA", 100.0)
    broker = AlpacaBroker(client, clock=_clock())

    broker.submit(Order("AAA", Side.BUY, qty=10))
    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})

    assert len(fills) == 1
    assert fills[0].symbol == "AAA"
    assert fills[0].side is Side.BUY
    assert fills[0].qty == pytest.approx(10.0)
    assert fills[0].price == pytest.approx(100.0)
    assert fills[0].commission == pytest.approx(0.0)  # no simulated cost (ADR-0020)

    # Portfolio reflects the *account*: cash spent and the new position.
    assert broker.portfolio.cash == pytest.approx(10_000.0 - 1_000.0)
    assert broker.portfolio.position("AAA").qty == pytest.approx(10.0)
    assert broker.portfolio.position("AAA").avg_price == pytest.approx(100.0)


def test_submit_does_not_fill_until_on_bar() -> None:
    client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, cash=10_000.0)
    broker = AlpacaBroker(client, clock=_clock())
    broker.submit(Order("AAA", Side.BUY, qty=10))
    # submit only places + tracks; account is unchanged until on_bar reconciles.
    # (auto_fill applied it at the venue, but the broker's portfolio is stale
    # until it reconciles on the next bar -- proving reconcile, not local sim.)
    assert broker.portfolio.cash == pytest.approx(10_000.0)
    assert broker.portfolio.positions == {}


def test_scripted_pending_fill_within_timeout() -> None:
    # auto_fill=False: submit leaves the order "new"; a fill_order before on_bar
    # settles it, so the first poll inside on_bar sees it filled (within timeout).
    client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
    clock = _clock()
    broker = AlpacaBroker(client, clock=clock)

    broker.submit(Order("AAA", Side.BUY, qty=5))
    order_id = "1"  # FakeAlpacaClient ids are a monotonic counter from 1.
    client.fill_order(order_id, price=101.0)

    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(101.0)
    assert fills[0].qty == pytest.approx(5.0)
    assert broker.portfolio.position("AAA").qty == pytest.approx(5.0)
    # No real sleeping was needed: the order was already filled on the first poll.
    assert clock.sleep_calls == []


def test_timeout_leaves_order_pending_then_later_bar_fills() -> None:
    # auto_fill=False and never filled: on_bar polls until the timeout and returns
    # no fill, leaving the order pending. A later on_bar (after fill_order) picks
    # it up.
    bars = {"AAA": _series("AAA", [100.0, 100.0])}
    client = FakeAlpacaClient(bars, cash=10_000.0, auto_fill=False)
    clock = _clock()
    broker = AlpacaBroker(
        client,
        clock=clock,
        poll_timeout=timedelta(seconds=6),
        poll_interval=timedelta(seconds=2),
    )

    broker.submit(Order("AAA", Side.BUY, qty=4))

    # First bar: order still "new" -> polls to timeout, no fill this bar.
    first = broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert first == []
    assert broker.portfolio.positions == {}  # account still flat
    # It actually waited (advanced the clock) up to the timeout budget.
    assert clock.sleep_calls  # at least one poll-interval wait happened

    # The venue fills between bars.
    client.fill_order("1", price=100.0)

    # Next bar: the still-pending order is retried and now settles.
    second = broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert len(second) == 1
    assert second[0].qty == pytest.approx(4.0)
    assert broker.portfolio.position("AAA").qty == pytest.approx(4.0)
    assert broker.portfolio.cash == pytest.approx(10_000.0 - 400.0)


def test_buy_then_sell_reconciles_to_flat() -> None:
    client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, cash=10_000.0)
    client.set_price("AAA", 100.0)
    broker = AlpacaBroker(client, clock=_clock())

    broker.submit(Order("AAA", Side.BUY, qty=8))
    broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert broker.portfolio.position("AAA").qty == pytest.approx(8.0)

    broker.submit(Order("AAA", Side.SELL, qty=8))
    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert len(fills) == 1
    assert fills[0].side is Side.SELL
    # Reconciled flat straight from the account: position gone, cash restored.
    assert broker.portfolio.positions == {}
    assert broker.portfolio.cash == pytest.approx(10_000.0)


def test_reconcile_reads_account_not_local_simulation() -> None:
    # Prove the portfolio comes from the account: seed a position directly at the
    # venue (via its own submit_order, bypassing the broker) and confirm a later
    # on_bar reconcile surfaces it even though the broker never "saw" that order.
    client = FakeAlpacaClient({"AAA": _series("AAA", [50.0])}, cash=10_000.0)
    client.set_price("AAA", 50.0)
    broker = AlpacaBroker(client, clock=_clock())
    assert broker.portfolio.positions == {}

    # Out-of-band venue activity the broker did not submit.
    client.submit_order("AAA", 3.0, Side.BUY)

    # Any on_bar reconciles the account, revealing the externally-created position.
    broker.on_bar({"AAA": _bar("AAA", 50.0)})
    assert broker.portfolio.position("AAA").qty == pytest.approx(3.0)
    assert broker.portfolio.cash == pytest.approx(10_000.0 - 150.0)


class TestTerminalStatuses:
    """Statuses that end an order's life without filling it (ADR-0033).

    Verified against the installed SDK: alpaca-py's ``OrderStatus`` has 18 members,
    of which 5 are terminal (``filled``, ``rejected``, ``canceled``, ``expired``,
    ``replaced``). The poll loop originally settled on only the first two, so the
    other three left the order id in ``_pending`` forever -- re-polled to the full
    timeout on every subsequent bar, for the rest of the session.
    """

    def _pending_broker(self) -> tuple[FakeAlpacaClient, AlpacaBroker, FakeClock]:
        client = FakeAlpacaClient(
            {"AAA": _series("AAA", [100.0, 100.0])}, cash=10_000.0, auto_fill=False
        )
        clock = _clock()
        broker = AlpacaBroker(
            client,
            clock=clock,
            poll_timeout=timedelta(seconds=6),
            poll_interval=timedelta(seconds=2),
        )
        return client, broker, clock

    @pytest.mark.parametrize("status", ["canceled", "expired", "replaced"])
    def test_terminal_unfilled_order_is_dropped_not_retried_forever(self, status: str) -> None:
        client, broker, clock = self._pending_broker()
        broker.submit(Order("AAA", Side.BUY, qty=4))
        client.set_order_status("1", status)

        fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})

        assert fills == []
        # The order is gone from the pending set, with the reason recorded...
        assert broker.pending_order_ids == ()
        assert len(broker.rejections) == 1
        assert status in broker.rejections[0][1]
        # ...and it settled immediately rather than burning the poll timeout.
        assert clock.sleep_calls == []

        # A second bar does no further work on it.
        assert broker.on_bar({"AAA": _bar("AAA", 100.0)}) == []
        assert len(broker.rejections) == 1

    def test_partial_fill_then_canceled_still_emits_the_partial_fill(self) -> None:
        client, broker, _ = self._pending_broker()
        broker.submit(Order("AAA", Side.BUY, qty=4))
        # The venue filled 1.5 of 4 shares, then canceled the rest.
        client.set_order_status("1", "canceled", filled_qty=1.5, filled_avg_price=99.5)

        fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})

        assert len(fills) == 1
        assert fills[0].qty == pytest.approx(1.5)
        assert fills[0].price == pytest.approx(99.5)
        assert broker.pending_order_ids == ()
        # Still reported: the order did not do what was asked of it.
        assert len(broker.rejections) == 1

    def test_working_statuses_keep_polling(self) -> None:
        # Not terminal: the order may still fill, so the poll must wait it out and
        # leave it pending rather than dropping it.
        client, broker, clock = self._pending_broker()
        broker.submit(Order("AAA", Side.BUY, qty=4))
        client.set_order_status("1", "partially_filled", filled_qty=1.0, filled_avg_price=100.0)

        fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})

        assert fills == []
        assert broker.pending_order_ids == ("1",)
        assert broker.rejections == []
        assert clock.sleep_calls  # it waited out the timeout


class TestRejectionShape:
    """A rejection is ``(Order, reason)`` -- the shape the result document reads.

    Found by driving the market-closed branch against the live venue (ADR-0036).
    ADR-0033 made a ``canceled`` / ``expired`` / ``replaced`` order settle and be
    *reported*, but it recorded the order **id** as the tuple's first element while
    :class:`~trading.broker.SimulatedBroker`, ``BacktestResult.rejections``, and
    :func:`trading.report.result_to_dict` all use the :class:`~trading.types.Order`.
    ``Engine._finalize`` merges ``broker.rejections`` in through a ``getattr``, so
    ``mypy --strict`` never saw the mismatch, and the fast layer only ever read
    ``rejections[0][1]`` -- the reason -- so nothing caught it either. The first
    order to end unfilled in a live session therefore crashed ``result.json`` with
    ``AttributeError: 'str' object has no attribute 'symbol'`` -- and an unfilled
    DAY order *expires* at the close, so that is the routine end of the very
    market-closed branch ADR-0033 left unverified.
    """

    def _canceled(self, qty: float = 4.0) -> AlpacaBroker:
        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        broker = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(seconds=0))
        broker.submit(Order("AAA", Side.BUY, qty=qty))
        client.set_order_status("1", "canceled")
        broker.on_bar({"AAA": _bar("AAA", 100.0)})
        return broker

    def test_rejection_carries_the_submitted_order(self) -> None:
        broker = self._canceled(qty=4.0)

        (order, reason) = broker.rejections[0]

        assert isinstance(order, Order)
        assert (order.symbol, order.side, order.qty) == ("AAA", Side.BUY, 4.0)
        # ADR-0033's honesty requirement is unchanged: the reason still names the
        # venue's order id and the status the order ended in.
        assert "canceled" in reason
        assert "1" in reason

    def test_rejections_survive_the_result_document(self) -> None:
        # The end-to-end shape check: this is the call that crashed, on the
        # canonical artifact the dashboard reads (ADR-0023). Serializing the whole
        # document -- not just reading the one key -- is deliberate: that is what
        # `write_result_json` actually does, so a later additive key (ADR-0032's
        # `absent`, say) stays covered by this test rather than around it.
        import json

        from trading.engine import BacktestResult
        from trading.report import result_to_dict
        from trading.types import Portfolio

        broker = self._canceled(qty=2.5)
        result = BacktestResult(
            symbols=["AAA"],
            starting_cash=10_000.0,
            equity_curve=[],
            final_portfolio=Portfolio(cash=10_000.0),
            rejections=list(broker.rejections),
        )

        document = result_to_dict(result, mode="paper")

        assert document["rejections"] == [
            {"symbol": "AAA", "qty": 2.5, "side": "buy", "reason": broker.rejections[0][1]}
        ]
        assert json.loads(json.dumps(document))["rejections"] == document["rejections"]

    def test_rejection_shape_matches_the_simulated_broker(self) -> None:
        # One execution path (ADR-0002): both brokers feed the same field, so the
        # tuple each appends must have the same shape.
        from trading.broker import SimulatedBroker
        from trading.types import Portfolio

        broker = self._canceled()
        simulated = SimulatedBroker(Portfolio(cash=1.0))
        simulated.submit(Order("AAA", Side.BUY, qty=1_000_000))  # cannot be funded
        simulated.on_bar({"AAA": _bar("AAA", 100.0)})

        assert simulated.rejections, "expected the underfunded order to be rejected"
        assert type(broker.rejections[0][0]) is type(simulated.rejections[0][0])


class _ParkingClient(FakeAlpacaClient):
    """A venue that accepts every order and then parks it, never filling.

    This is the market-closed branch ADR-0036 drove live: a fractional DAY market
    order comes back ``accepted`` -- not filled, not rejected -- and queues for the
    next open. ``auto_fill=False`` keeps the order working and stamping it
    ``accepted`` matches what the real venue reported, so the account stays flat
    for as long as the order sits there. That flat account is exactly what made the
    broker resubmit: the portfolio reconciles from it (ADR-0020), so a target-weight
    strategy sees its target unmet on every bar.

    Every submission is recorded, so a test can count what actually *reached* the
    venue rather than inferring it from the broker's own bookkeeping.
    """

    def __init__(self, bars: dict[str, list[Bar]] | None = None, *, cash: float = 10_000.0) -> None:
        super().__init__(bars, cash=cash, auto_fill=False)
        self.submitted: list[AlpacaOrder] = []

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        placed = super().submit_order(symbol, qty, side)
        parked = self.set_order_status(placed.id, "accepted")
        self.submitted.append(parked)
        return parked


class TestDuplicateOrderGuard:
    """A working order suppresses a same-direction duplicate (ADR-0036, KAN-669).

    The bug, confirmed live on 2026-08-08: while an order sits ``accepted`` at the
    venue the portfolio reconciles from the account and therefore still reads
    **flat**, so a target-weight strategy re-emits the same order every bar and the
    broker submitted it again. Orders compounded for as long as the venue held them
    and then all filled at once.

    The guardrails are no backstop here. :class:`~trading.risk.Guardrails` nets
    same-bar committed exposure, but that tally is reset at the top of *every* bar
    while ``current_gross`` comes from a book that reads flat, so each bar
    re-authorises a fresh 100% of gross exposure. ``max_position_pct`` caps one
    order, not the running total.
    """

    def _broker(self, client: _ParkingClient) -> AlpacaBroker:
        # A zero poll timeout: the first poll sees a working status, the deadline has
        # already passed, and on_bar returns with the order still pending -- the
        # timeout branch, with no clock advance to script.
        return AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(seconds=0))

    def test_parked_order_is_not_resubmitted_on_later_bars(self) -> None:
        client = _ParkingClient({"AAA": _series("AAA", [100.0, 100.0, 100.0])})
        broker = self._broker(client)

        for _ in range(3):
            broker.submit(Order("AAA", Side.BUY, qty=10))
            broker.on_bar({"AAA": _bar("AAA", 100.0)})

        # THE assertion: three bars of an unmet target, one order at the venue.
        assert len(client.submitted) == 1
        assert broker.pending_order_ids == ("1",)

    def test_refusal_is_recorded_with_the_working_order_id(self) -> None:
        client = _ParkingClient({"AAA": _series("AAA", [100.0, 100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        broker.on_bar({"AAA": _bar("AAA", 100.0)})
        duplicate = Order("AAA", Side.BUY, qty=10)
        broker.submit(duplicate)

        assert len(broker.rejections) == 1
        (order, reason) = broker.rejections[0]
        # The tuple shape the result document reads (ADR-0036), carrying the order
        # that was *refused*, not the one still working.
        assert order is duplicate
        assert "1" in reason  # names the working order at the venue
        assert "working" in reason
        assert "AAA" in reason

    def test_refusal_survives_the_result_document(self) -> None:
        # A refusal must be visible, not silent: it has to reach result.json and the
        # summary the same way a venue rejection does.
        import json

        from trading.engine import BacktestResult
        from trading.report import result_to_dict
        from trading.types import Portfolio

        client = _ParkingClient({"AAA": _series("AAA", [100.0])})
        broker = self._broker(client)
        broker.submit(Order("AAA", Side.BUY, qty=10))
        broker.submit(Order("AAA", Side.BUY, qty=2.5))

        result = BacktestResult(
            symbols=["AAA"],
            starting_cash=10_000.0,
            equity_curve=[],
            final_portfolio=Portfolio(cash=10_000.0),
            rejections=list(broker.rejections),
        )
        document = result_to_dict(result, mode="paper")

        assert document["rejections"] == [
            {"symbol": "AAA", "qty": 2.5, "side": "buy", "reason": broker.rejections[0][1]}
        ]
        assert json.loads(json.dumps(document))["rejections"] == document["rejections"]

    def test_a_working_buy_never_blocks_a_sell(self) -> None:
        # The exit invariant. Long-or-flat (ADR-0011) means a SELL is the only way
        # out, and an unsellable position is far worse than a duplicate buy -- the
        # same reasoning the halt path already uses ("exits are allowed while
        # halted, always", ADR-0013/0031). The refusal is keyed on symbol *and*
        # side, so a working BUY is never even compared against a SELL.
        client = _ParkingClient({"AAA": _series("AAA", [100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        broker.submit(Order("AAA", Side.SELL, qty=10))

        assert [o.side for o in client.submitted] == [Side.BUY, Side.SELL]
        assert broker.rejections == []

    def test_a_working_sell_never_blocks_a_buy(self) -> None:
        # The mirror of the above, for completeness: the key is direction-scoped in
        # both directions, so the guard can only ever suppress a *repeat* of an
        # intent the venue is already working on.
        client = _ParkingClient({"AAA": _series("AAA", [100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.SELL, qty=10))
        broker.submit(Order("AAA", Side.BUY, qty=10))

        assert [o.side for o in client.submitted] == [Side.SELL, Side.BUY]
        assert broker.rejections == []

    def test_a_working_sell_suppresses_a_duplicate_sell(self) -> None:
        # Refusing a *second* exit does not block the exit: the first SELL is
        # already working at the venue and will still fill. Duplicating it would
        # oversell the position, which this bench forbids outright (ADR-0011).
        client = _ParkingClient({"AAA": _series("AAA", [100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.SELL, qty=10))
        broker.submit(Order("AAA", Side.SELL, qty=10))

        assert len(client.submitted) == 1
        assert len(broker.rejections) == 1

    def test_other_symbols_are_unaffected(self) -> None:
        client = _ParkingClient({"AAA": _series("AAA", [100.0]), "BBB": _series("BBB", [50.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        broker.submit(Order("BBB", Side.BUY, qty=10))
        broker.submit(Order("AAA", Side.BUY, qty=10))

        assert [o.symbol for o in client.submitted] == ["AAA", "BBB"]
        assert len(broker.rejections) == 1

    def test_a_settled_order_frees_the_symbol_again(self) -> None:
        # The guard tracks *working* orders, so it cannot latch: once the venue
        # settles the order the next order in the same direction goes through. This
        # is what keeps a rebalance that legitimately tops up a position working.
        client = _ParkingClient({"AAA": _series("AAA", [100.0, 100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        client.fill_order("1", price=100.0)
        broker.on_bar({"AAA": _bar("AAA", 100.0)})
        assert broker.pending_order_ids == ()

        broker.submit(Order("AAA", Side.BUY, qty=4))

        assert len(client.submitted) == 2
        assert broker.rejections == []

    def test_partial_fill_that_ended_allows_a_follow_up_for_the_remainder(self) -> None:
        # A partial fill is legitimate (ADR-0033). Once the venue *ends* the order
        # -- canceled/expired, the routine end of a parked DAY order -- the rest of
        # the intent is unfilled and nothing is working, so a follow-up order for the
        # remainder is a new intent, not a duplicate.
        client = _ParkingClient({"AAA": _series("AAA", [100.0, 100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        client.set_order_status("1", "canceled", filled_qty=4.0, filled_avg_price=100.0)
        fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})
        assert len(fills) == 1  # the partial still flows (ADR-0033)
        assert broker.pending_order_ids == ()

        broker.submit(Order("AAA", Side.BUY, qty=6))  # the remainder

        assert len(client.submitted) == 2
        # Only the venue's own "ended canceled" entry; no duplicate refusal.
        assert len(broker.rejections) == 1
        assert "canceled" in broker.rejections[0][1]

    def test_partial_fill_still_working_suppresses_the_remainder(self) -> None:
        # ``partially_filled`` is a *working* status: the rest of that same order is
        # still live at the venue, so topping up the remainder would double it. The
        # order is suppressed until the venue settles it -- at which point the
        # previous test's path applies.
        client = _ParkingClient({"AAA": _series("AAA", [100.0, 100.0])})
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        client.set_order_status("1", "partially_filled", filled_qty=4.0, filled_avg_price=100.0)
        broker.on_bar({"AAA": _bar("AAA", 100.0)})
        assert broker.pending_order_ids == ("1",)

        broker.submit(Order("AAA", Side.BUY, qty=6))

        assert len(client.submitted) == 1
        assert len(broker.rejections) == 1


class TestParkedOrdersDoNotCompoundExposure:
    """N bars of a parked order must not authorise N x the intended exposure.

    The end-to-end statement of the same bug, driven through the real
    :class:`~trading.engine.Engine` with the default enforced guardrails, because
    the guardrails are where an operator would *expect* to be protected and are
    not: ``Guardrails`` resets its committed-exposure tally at the top of every bar
    and reads ``current_gross`` off a portfolio that a parked order leaves flat, so
    every bar re-authorises a fresh full allowance. Without the broker-level guard,
    five bars of an unmet 20% target queue 100% of equity at the venue -- all of it
    to fill at the next open.
    """

    TARGET = 0.20
    BARS = 5
    CASH = 10_000.0
    PRICE = 100.0

    def _run(self) -> tuple[_ParkingClient, BacktestResult]:
        series = _series("AAA", [self.PRICE] * self.BARS)
        client = _ParkingClient({"AAA": series}, cash=self.CASH)
        broker = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(seconds=0))
        engine = Engine(FakeAdapter(series), broker)
        result = engine.run(
            EqualWeight(invested=self.TARGET),
            ["AAA"],
            series[0].ts,
            series[-1].ts,
        )
        return client, result

    def test_exposure_queued_at_the_venue_stays_within_the_target(self) -> None:
        client, _ = self._run()

        queued = sum(o.qty * self.PRICE for o in client.submitted)

        # One target, one order: never BARS x the intent.
        assert len(client.submitted) == 1
        assert queued <= self.TARGET * self.CASH * 1.001
        # And it is unambiguously below what the unguarded broker queued.
        assert queued < self.BARS * self.TARGET * self.CASH

    def test_every_suppressed_duplicate_is_reported(self) -> None:
        _, result = self._run()

        assert len(result.rejections) == self.BARS - 1
        assert all("working" in reason for (_order, reason) in result.rejections)


class TestVenueRefusalAtSubmit:
    """A venue refusal at *submit* time is recorded, not raised (ADR-0041).

    Found by executing the duplicate-guard live test against the paper account on
    2026-08-08 with the venue shut. Alpaca answered the exit order with::

        HTTP 403 {"code":40310000,"existing_order_id":"a182da86-...",
                  "message":"potential wash trade detected. use complex orders",
                  "reject_reason":"opposite side market/stop order exists"}

    which the SDK raises as an ``APIError``. Nothing caught it, so it travelled
    straight out of ``AlpacaBroker.submit`` -- through ``Engine._step`` and out of
    ``PaperSession.run`` -- killing the session and taking the equity CSV,
    ``result.json`` and the summary with it. That is the same class of loss
    ADR-0033 fixed for Ctrl-C, arriving through a different door, and it is the
    *routine* case: the refusal only happens while an order is parked, which is
    the normal state of every overnight and weekend session.
    """

    def _broker(self, client: FakeAlpacaClient) -> AlpacaBroker:
        return AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(seconds=0))

    def test_a_refused_submit_is_recorded_instead_of_raising(self) -> None:
        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, auto_fill=False)
        client.set_submit_refusal("AAA", "potential wash trade detected. use complex orders")
        broker = self._broker(client)
        refused = Order("AAA", Side.SELL, qty=10)

        broker.submit(refused)  # must not raise

        assert len(broker.rejections) == 1
        order, reason = broker.rejections[0]
        assert order is refused
        assert "wash trade" in reason  # the venue's words, not a summary

    def test_a_refused_order_never_becomes_pending(self) -> None:
        # No id came back, so there is nothing to poll -- and nothing that could
        # make the duplicate guard refuse the *next* attempt for a ghost order.
        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, auto_fill=False)
        client.set_submit_refusal("AAA", "insufficient buying power")
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))

        assert broker.pending_order_ids == ()
        assert broker.on_bar({"AAA": _bar("AAA", 100.0)}) == []

    def test_a_later_bar_can_still_try_again(self) -> None:
        # A refusal is per-order, not a latch: the strategy re-emits next bar and
        # the broker submits it, because nothing is working at the venue.
        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, auto_fill=False)
        client.set_submit_refusal("AAA", "potential wash trade detected")
        broker = self._broker(client)

        broker.submit(Order("AAA", Side.BUY, qty=10))
        client.clear_submit_refusals()
        broker.submit(Order("AAA", Side.BUY, qty=10))

        assert len(broker.rejections) == 1
        assert len(broker.pending_order_ids) == 1

    def test_we_could_not_ask_still_propagates(self) -> None:
        # The other half of the classification (ADR-0028's stance, again): a
        # credential or transport failure is NOT the venue refusing an order, and
        # swallowing it would turn a broken session into a quiet stream of
        # rejections that never trades.
        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, auto_fill=False)
        client.set_submit_failure("AAA", ConnectionError("connection reset by peer"))
        broker = self._broker(client)

        with pytest.raises(ConnectionError):
            broker.submit(Order("AAA", Side.BUY, qty=10))

        assert broker.rejections == []

    def test_the_venue_can_refuse_the_exit_our_guard_deliberately_allows(self) -> None:
        """The whole live finding, offline: our guard lets the SELL through, the venue does not.

        ADR-0036's amendment says a working BUY "can never block a SELL", and that
        remains true of *this bench* -- the guard is keyed on side and never even
        compares the two. What the live run showed is that the claim does not
        extend to the system: Alpaca refuses an opposite-side market order while
        one is working, so an exit attempted against a parked entry is refused by
        the **venue**. The bench's job is to report that, not to crash on it.
        """
        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, auto_fill=False)
        broker = self._broker(client)
        broker.submit(Order("AAA", Side.BUY, qty=10))
        assert len(broker.pending_order_ids) == 1
        # ...and now the venue starts refusing the opposite side, as it really does.
        client.set_submit_refusal("AAA", "opposite side market/stop order exists", side=Side.SELL)

        broker.submit(Order("AAA", Side.SELL, qty=10))

        # Our guard did not refuse it: the reason is the venue's, not ours.
        assert len(broker.rejections) == 1
        _order, reason = broker.rejections[0]
        assert "still working at the venue" not in reason  # not the duplicate guard
        assert "opposite side" in reason
        # And the parked BUY is untouched: one order at the venue, still pending.
        assert len(broker.pending_order_ids) == 1

    def test_a_venue_refusal_survives_the_result_document(self) -> None:
        # Same visibility requirement as every other rejection (ADR-0036): it must
        # reach result.json, which reads order.symbol / .qty / .side off the tuple.
        import json

        from trading.report import result_to_dict
        from trading.types import Portfolio

        client = FakeAlpacaClient({"AAA": _series("AAA", [100.0])}, auto_fill=False)
        client.set_submit_refusal("AAA", "potential wash trade detected")
        broker = self._broker(client)
        broker.submit(Order("AAA", Side.SELL, qty=2.5))

        document = result_to_dict(
            BacktestResult(
                symbols=["AAA"],
                starting_cash=10_000.0,
                equity_curve=[],
                final_portfolio=Portfolio(cash=10_000.0),
                rejections=list(broker.rejections),
            ),
            mode="paper",
        )

        assert document["rejections"] == [
            {"symbol": "AAA", "qty": 2.5, "side": "sell", "reason": broker.rejections[0][1]}
        ]
        assert json.loads(json.dumps(document))["rejections"] == document["rejections"]

    def test_a_refused_session_still_finishes_its_bars(self) -> None:
        # The consequence that matters: a refusing venue costs the run its trades,
        # not its artifacts. Driven through the real Engine so the exception path
        # is the production one.
        series = _series("AAA", [100.0] * 4)
        client = FakeAlpacaClient({"AAA": series}, cash=10_000.0, auto_fill=False)
        client.set_submit_refusal("AAA", "potential wash trade detected")
        broker = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(seconds=0))

        result = Engine(FakeAdapter(series), broker).run(
            EqualWeight(invested=0.2), ["AAA"], series[0].ts, series[-1].ts
        )

        assert len(result.equity_curve) == len(series)
        assert result.fills == []
        assert result.rejections
        assert all("wash trade" in reason for (_order, reason) in result.rejections)
