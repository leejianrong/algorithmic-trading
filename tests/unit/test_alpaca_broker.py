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
