"""Fast unit tests for the target-weight sizing layer (ADR-0007)."""

from __future__ import annotations

import pytest

from trading.sizing import size
from trading.types import Order, Portfolio, Position, Side, TargetWeight


def test_target_weight_realizes_exactly_that_weight() -> None:
    pf = Portfolio(cash=1_000.0)  # flat → equity 1_000
    orders = size([TargetWeight("AAA", 0.20)], pf, {"AAA": 50.0})

    assert orders == [Order("AAA", Side.BUY, 4.0)]  # 0.20 * 1000 / 50
    # Realized position value 4 * 50 = 200 = 20% of equity.
    assert orders[0].qty * 50.0 == pytest.approx(0.20 * 1_000.0)


def test_rebalance_emits_the_delta_not_the_target() -> None:
    # Already holds 10 AAA @ 50; equity = 500 cash + 500 = 1000. Target 20% = 4
    # shares, so the order is the -6 delta, as a sell.
    pf = Portfolio(cash=500.0, positions={"AAA": Position("AAA", qty=10, avg_price=50.0)})
    orders = size([TargetWeight("AAA", 0.20)], pf, {"AAA": 50.0})
    assert orders == [Order("AAA", Side.SELL, 6.0)]


def test_zero_target_sells_the_whole_position() -> None:
    pf = Portfolio(cash=0.0, positions={"AAA": Position("AAA", qty=5, avg_price=10.0)})
    orders = size([TargetWeight("AAA", 0.0)], pf, {"AAA": 10.0})
    assert orders == [Order("AAA", Side.SELL, 5.0)]


def test_orders_pass_through_untouched() -> None:
    pf = Portfolio(cash=1_000.0)
    order = Order("AAA", Side.BUY, 3.0)
    assert size([order], pf, {"AAA": 50.0}) == [order]


def test_dust_delta_produces_no_order() -> None:
    # Position already at the target → delta ~0 → nothing.
    pf = Portfolio(cash=600.0, positions={"AAA": Position("AAA", qty=4, avg_price=50.0)})
    # equity = 600 + 200 = 800; target 25% = 200/50 = 4 shares = current.
    assert size([TargetWeight("AAA", 0.25)], pf, {"AAA": 50.0}) == []


def test_missing_price_is_an_error() -> None:
    pf = Portfolio(cash=1_000.0)
    with pytest.raises(ValueError, match="no price"):
        size([TargetWeight("AAA", 0.2)], pf, {"BBB": 10.0})


def test_multi_symbol_equal_weight_rebalance() -> None:
    pf = Portfolio(cash=1_000.0)  # flat, equity 1000
    orders = size(
        [TargetWeight("AAA", 0.5), TargetWeight("BBB", 0.5)],
        pf,
        {"AAA": 10.0, "BBB": 100.0},
    )
    assert orders == [Order("AAA", Side.BUY, 50.0), Order("BBB", Side.BUY, 5.0)]
