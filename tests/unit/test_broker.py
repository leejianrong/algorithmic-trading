"""Fast unit tests for the simulated broker's fills, costs, and timing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.config import CostConfig
from trading.types import Bar, Order, Portfolio, Position, Side


def _bar(symbol: str, o: float) -> Bar:
    return Bar(symbol, datetime(2024, 1, 2, tzinfo=UTC), o, o, o, o, 1_000)


def test_buy_fills_at_next_open_plus_slippage_with_commission() -> None:
    broker = SimulatedBroker(
        Portfolio(cash=10_000.0),
        CostConfig(commission_per_share=0.01, slippage_bps=100),  # 1% slippage
    )
    broker.submit(Order("AAA", Side.BUY, qty=10))
    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})

    assert len(fills) == 1
    assert fills[0].price == pytest.approx(101.0)  # 100 * (1 + 0.01)
    assert fills[0].commission == pytest.approx(0.10)  # 10 * 0.01
    # cash = 10_000 - 10*101 - 0.10
    assert broker.portfolio.cash == pytest.approx(10_000.0 - 1_010.0 - 0.10)


def test_sell_fills_at_next_open_minus_slippage() -> None:
    pf = Portfolio(cash=0.0, positions={"AAA": Position("AAA", qty=5, avg_price=100.0)})
    broker = SimulatedBroker(pf, CostConfig(slippage_bps=100))

    broker.submit(Order("AAA", Side.SELL, qty=5))
    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert fills[0].price == pytest.approx(99.0)  # 100 * (1 - 0.01)
    assert broker.portfolio.cash == pytest.approx(5 * 99.0)  # proceeds received


def test_submit_does_not_fill_until_on_bar() -> None:
    broker = SimulatedBroker(Portfolio(cash=1_000.0))
    broker.submit(Order("AAA", Side.BUY, qty=1))
    # No on_bar yet → nothing executed, cash untouched.
    assert broker.portfolio.cash == pytest.approx(1_000.0)
    assert broker.portfolio.positions == {}


def test_underfunded_buy_is_rejected_not_raised() -> None:
    broker = SimulatedBroker(Portfolio(cash=50.0), CostConfig(slippage_bps=0))
    broker.submit(Order("AAA", Side.BUY, qty=1))
    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})

    assert fills == []
    assert broker.portfolio.cash == pytest.approx(50.0)  # unchanged
    assert len(broker.rejections) == 1
    assert "insufficient cash" in broker.rejections[0][1]


def test_order_without_a_bar_this_timestamp_stays_pending() -> None:
    broker = SimulatedBroker(Portfolio(cash=10_000.0), CostConfig(slippage_bps=0))
    broker.submit(Order("AAA", Side.BUY, qty=1))

    # A timestamp where AAA has no bar: the order can't be priced, so it waits.
    assert broker.on_bar({"BBB": _bar("BBB", 5.0)}) == []
    assert broker.portfolio.positions == {}

    # Next timestamp AAA prints: now it fills.
    fills = broker.on_bar({"AAA": _bar("AAA", 100.0)})
    assert len(fills) == 1
    assert broker.portfolio.position("AAA").qty == pytest.approx(1.0)
