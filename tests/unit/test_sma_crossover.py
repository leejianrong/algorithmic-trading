"""V2 e2e: SMA crossover produces exactly the expected entry/exit fills.

Uses a crafted single-symbol series and the engine's fill blotter to assert the
strategy enters on the up-cross and exits on the down-cross, and nowhere else.
Costs are zeroed so funding is never in doubt.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.broker import SimulatedBroker
from trading.config import CostConfig
from trading.data.fake import FakeAdapter
from trading.engine import Engine
from trading.strategies.sma_crossover import SmaCrossover
from trading.types import Bar, Portfolio, Side


def _bar(day: int, close: float) -> Bar:
    ts = datetime(2024, 1, day, tzinfo=UTC)
    return Bar("AAA", ts, open=close, high=close, low=close, close=close, volume=1_000)


def test_sma_crossover_enters_on_up_cross_and_exits_on_down_cross() -> None:
    # closes: fast(2)/slow(3) SMA is below on day 3, crosses above at day 4
    # (signal), and back below at day 6 (signal). Orders fill the next bar.
    closes = {1: 12, 2: 11, 3: 10, 4: 13, 5: 14, 6: 8, 7: 8}
    adapter = FakeAdapter([_bar(day, c) for day, c in closes.items()])
    broker = SimulatedBroker(
        Portfolio(cash=1_000.0), CostConfig(commission_per_share=0, slippage_bps=0)
    )

    result = Engine(adapter, broker).run(
        SmaCrossover(fast=2, slow=3, weight=0.9),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 7, tzinfo=UTC),
    )

    trades = [(ts.day, fill.side) for ts, fill in result.fills]
    # Entry signal day 4 → fills day 5; exit signal day 6 → fills day 7.
    assert trades == [(5, Side.BUY), (7, Side.SELL)]
    # Ends flat after the exit.
    assert result.final_portfolio.positions == {}
