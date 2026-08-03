"""Buy-and-hold: the correctness baseline.

On the first bar it targets an equal weight in each of the day's symbols; the
sizing layer (ADR-0007) converts those weights to fractional-share orders and it
holds thereafter. Fractional shares (ADR-0011) let it deploy essentially all the
cash even on high-priced symbols.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.types import Bar, Order, TargetWeight

# Target just under 100% so the initial buys still fit after the next-open fill
# picks up slippage (a full 100% target would overshoot cash and be rejected).
INVESTED_WEIGHT = 0.998


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

        weight = INVESTED_WEIGHT / len(bars)
        self._invested = True
        return [TargetWeight(symbol, weight) for symbol in sorted(bars)]
