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
from trading.data.alpaca_client import FakeAlpacaClient
from trading.interfaces import Broker
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
