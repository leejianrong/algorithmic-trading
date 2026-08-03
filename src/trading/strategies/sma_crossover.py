"""SMA crossover: go long when the fast average is above the slow one.

For each symbol, when the fast SMA crosses above the slow SMA it targets
``weight`` of equity; when it crosses back below it targets zero. It emits a
target only on a *transition*, so the sizing layer produces one entry and one
exit per cross rather than rebalancing every bar.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.strategies.indicators import sma
from trading.types import Bar, Order, TargetWeight


class SmaCrossover:
    """Fast/slow SMA crossover, long-or-flat, per symbol."""

    def __init__(self, fast: int = 10, slow: int = 20, weight: float = 0.95) -> None:
        if fast >= slow:
            raise ValueError(f"fast window ({fast}) must be shorter than slow ({slow})")
        if not 0.0 < weight <= 1.0:
            raise ValueError(f"weight must be in (0, 1], got {weight}")
        self.fast = fast
        self.slow = slow
        self.weight = weight
        self._long: dict[str, bool] = {}

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        # Split the target across the universe so several simultaneous long
        # signals don't over-leverage (which the broker would reject). The
        # exposure cap in V3 (ADR-0009) will enforce this centrally.
        long_weight = self.weight / len(bars)
        intents: list[Order | TargetWeight] = []
        for symbol in sorted(bars):
            history = context.history(symbol, self.slow)
            slow_ma = sma(history, self.slow)
            fast_ma = sma(history, self.fast)
            if slow_ma is None or fast_ma is None:
                continue  # not enough history yet

            want_long = fast_ma >= slow_ma
            if want_long == self._long.get(symbol, False):
                continue  # no crossover this bar → nothing to do

            self._long[symbol] = want_long
            intents.append(TargetWeight(symbol, long_weight if want_long else 0.0))
        return intents
