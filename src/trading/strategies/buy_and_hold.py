"""Buy-and-hold: the V1 correctness baseline.

On the first bar it splits available cash equally across the day's symbols and
buys; thereafter it holds. Fractional shares (ADR-0011) let it deploy essentially
all the cash even on high-priced symbols, which is the whole point of the small
default account.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.types import Bar, Order, Side, TargetWeight

# Shares are sized at the decision bar's close, but fill at the next bar's open
# plus slippage — so a naive 100%-of-cash allocation overshoots available cash and
# the broker rejects it. Leaving a small headroom (0.2%) comfortably covers the
# default 5 bps slippage and modest commission. Proper cost-aware sizing arrives
# with the sizing layer in V2.
CASH_BUFFER = 0.998


class BuyAndHold:
    """Allocate once, equally weighted, then hold."""

    def __init__(self) -> None:
        self._invested = False

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        # Act only on the first bar, while still flat.
        if self._invested or context.portfolio.positions or not bars:
            return []

        allocation = context.portfolio.cash * CASH_BUFFER / len(bars)
        orders: list[Order | TargetWeight] = []
        for symbol, bar in sorted(bars.items()):
            qty = allocation / bar.close
            if qty > 0:
                orders.append(Order(symbol, Side.BUY, qty))
        self._invested = True
        return orders
