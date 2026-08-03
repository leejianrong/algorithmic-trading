"""Time-series momentum: go long when trailing returns are positive.

For each symbol, when the current close sits above the close ``lookback`` bars
ago (a positive trailing return) it targets ``weight`` of equity; when the
trailing return turns non-positive it targets zero. Like the SMA crossover it
emits a target only on a *transition*, so the sizing layer produces one entry
and one exit per regime change rather than rebalancing every bar.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.types import Bar, Order, TargetWeight


class Momentum:
    """Trailing-return momentum, long-or-flat, per symbol."""

    def __init__(self, lookback: int = 60, weight: float = 0.9) -> None:
        if lookback <= 0:
            raise ValueError(f"lookback must be positive, got {lookback}")
        if not 0.0 < weight <= 1.0:
            raise ValueError(f"weight must be in (0, 1], got {weight}")
        self.lookback = lookback
        self.weight = weight
        self._long: dict[str, bool] = {}

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        # Split the target across the universe so several simultaneous long
        # signals don't over-leverage (the exposure cap enforces this centrally).
        long_weight = self.weight / len(bars)
        intents: list[Order | TargetWeight] = []
        for symbol in sorted(bars):
            # lookback + 1 closes span exactly `lookback` bars of return.
            history = context.history(symbol, self.lookback + 1)
            if len(history) < self.lookback + 1:
                continue  # not enough history yet

            want_long = history[-1].close > history[0].close
            if want_long == self._long.get(symbol, False):
                continue  # no regime change this bar -> nothing to do

            self._long[symbol] = want_long
            intents.append(TargetWeight(symbol, long_weight if want_long else 0.0))
        return intents
