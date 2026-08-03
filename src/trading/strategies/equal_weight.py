"""Equal-weight allocation: hold every symbol at the same target weight.

A simple multi-symbol example that exercises the sizing layer's rebalancing —
each bar it re-targets equal weights, so the sizing layer trims winners and tops
up laggards back toward parity.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.types import Bar, Order, TargetWeight

# Target just under 100% total so buys still fit after next-open slippage.
INVESTED_WEIGHT = 0.98


class EqualWeight:
    """Rebalance to equal weights across the day's symbols every bar."""

    def __init__(self, invested: float = INVESTED_WEIGHT) -> None:
        if not 0.0 < invested <= 1.0:
            raise ValueError(f"invested must be in (0, 1], got {invested}")
        self.invested = invested

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if not bars:
            return []
        weight = self.invested / len(bars)
        return [TargetWeight(symbol, weight) for symbol in sorted(bars)]
