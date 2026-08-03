"""RSI mean-reversion: buy oversold dips, exit as they recover.

For each symbol, when RSI falls below ``oversold`` it targets ``weight`` of
equity (a bet the dip reverts); when RSI climbs back above ``exit_level`` it
targets zero. Between those thresholds it holds its current stance. Long-only,
and it acts only on a threshold *crossing*, so the sizing layer produces one
entry and one exit per swing rather than rebalancing every bar.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.strategies.indicators import rsi
from trading.types import Bar, Order, TargetWeight


class MeanReversion:
    """RSI oversold/recovery mean-reversion, long-or-flat, per symbol."""

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        exit_level: float = 50.0,
        weight: float = 0.9,
    ) -> None:
        if period <= 0:
            raise ValueError(f"period must be positive, got {period}")
        if not 0.0 <= oversold < exit_level <= 100.0:
            raise ValueError(
                "require 0 <= oversold < exit_level <= 100, "
                f"got oversold={oversold}, exit_level={exit_level}"
            )
        if not 0.0 < weight <= 1.0:
            raise ValueError(f"weight must be in (0, 1], got {weight}")
        self.period = period
        self.oversold = oversold
        self.exit_level = exit_level
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
            history = context.history(symbol, self.period + 1)
            value = rsi(history, self.period)
            if value is None:
                continue  # not enough history yet

            was_long = self._long.get(symbol, False)
            if not was_long and value < self.oversold:
                want_long = True
            elif was_long and value > self.exit_level:
                want_long = False
            else:
                want_long = was_long  # hold between the thresholds

            if want_long == was_long:
                continue  # no threshold crossing this bar -> nothing to do

            self._long[symbol] = want_long
            intents.append(TargetWeight(symbol, long_weight if want_long else 0.0))
        return intents
