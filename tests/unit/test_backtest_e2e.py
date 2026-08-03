"""V1 acceptance test: a full backtest on a hand-computable synthetic series.

This is the load-bearing correctness proof for the engine + broker + portfolio
seam. It needs no infrastructure (FakeAdapter), so it runs in the fast gate on
every push. Costs are zeroed here so the expected equity curve is exact; slippage
and commission are exercised separately in test_broker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.config import CostConfig
from trading.data.fake import FakeAdapter
from trading.engine import Engine
from trading.strategies.buy_and_hold import CASH_BUFFER, BuyAndHold
from trading.types import Bar, Portfolio


def _bar(symbol: str, day: int, o: float, c: float) -> Bar:
    return Bar(symbol, datetime(2024, 1, day, tzinfo=UTC), o, max(o, c), min(o, c), c, 1_000)


def test_buy_and_hold_two_symbols_exact_equity_curve() -> None:
    # AAA and BBB, three daily bars each. Buy-and-hold splits $1,000 → $500 each
    # on day 1 (at day-1 close), fills at day-2 open, then holds.
    bars = [
        _bar("AAA", 1, o=10, c=10),  # decision price 10 → 50 shares
        _bar("AAA", 2, o=10, c=11),  # fills here at open 10; marked at close 11
        _bar("AAA", 3, o=11, c=12),
        _bar("BBB", 1, o=100, c=100),  # decision price 100 → 5 shares
        _bar("BBB", 2, o=100, c=100),  # fills here at open 100
        _bar("BBB", 3, o=100, c=110),
    ]
    adapter = FakeAdapter(bars)
    broker = SimulatedBroker(
        Portfolio(cash=1_000.0), CostConfig(commission_per_share=0, slippage_bps=0)
    )
    engine = Engine(adapter, broker)

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 3, tzinfo=UTC)
    result = engine.run(BuyAndHold(), ["AAA", "BBB"], start, end)

    # Buy-and-hold leaves a small cash buffer B for costs, so it buys 50*B AAA
    # and 5*B BBB. With zero costs the equity curve is exact:
    #   day1 = 1000 (flat); day2 = 1000 + 50*B; day3 = 1000 + 150*B.
    b = CASH_BUFFER
    equities = [p.equity for p in result.equity_curve]
    assert equities == pytest.approx([1_000.0, 1_000.0 + 50 * b, 1_000.0 + 150 * b])
    assert result.total_return == pytest.approx(0.15 * b)
    assert not result.rejections

    assert result.final_portfolio.position("AAA").qty == pytest.approx(50.0 * b)
    assert result.final_portfolio.position("BBB").qty == pytest.approx(5.0 * b)
    assert result.final_portfolio.cash == pytest.approx(1_000.0 * (1.0 - b))


def test_no_look_ahead_order_fills_next_bar_not_this_one() -> None:
    # A single symbol that gaps up the day after the buy decision. If the engine
    # cheated and filled at the decision bar's price, day-1 equity would already
    # show a position; it must not.
    bars = [_bar("AAA", 1, o=10, c=10), _bar("AAA", 2, o=20, c=20)]
    adapter = FakeAdapter(bars)
    broker = SimulatedBroker(
        Portfolio(cash=100.0), CostConfig(commission_per_share=0, slippage_bps=0)
    )
    result = Engine(adapter, broker).run(
        BuyAndHold(), ["AAA"], datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC)
    )

    # qty is sized at the decision close (10) → ~9.98 shares. The fill happens at
    # day-2 open (20), costing ~$199 > $100, so the buy is rejected for insufficient
    # funds and equity stays at cash. Had the engine cheated and filled at the
    # decision price, it would have bought and day-1 equity would differ.
    assert result.equity_curve[0].equity == pytest.approx(100.0)
    assert result.final_portfolio.cash == pytest.approx(100.0)
    assert len(result.rejections) == 1
