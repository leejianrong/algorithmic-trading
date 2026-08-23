"""Time-series (absolute) momentum, managed-futures style: per-asset, long-or-cash.

Where ``momentum.py`` and ``cross_sectional.py`` already exist, this strategy is
neither. It is not `cross_sectional`: it never ranks one symbol against another,
so it can hold every asset in the universe at once (if every one is trending) or
none (if none is). It differs from `momentum` in three ways that matter for a
managed-futures-style read: a 12-month lookback with the most recent month
skipped (classic 12-1 momentum, avoiding the short-term reversal effect that
sits in the excluded month), a rebalance cadence rather than per-transition
sizing (controls turnover the same way `cross_sectional` does), and weight
normalized across whichever subset of the universe is *currently* trending
(so two trending assets each get half the target, not `1/len(universe)`).

On each rebalance, for **each symbol independently**: score it by trailing
total return from ``lookback`` bars ago to ``skip_recent`` bars ago (skipping
the most recent month by default), and call it "in trend" if that return is
positive. Every in-trend symbol gets `weight / (count in trend)`; everything
else gets 0.0 (cash) — including a symbol still in warmup. When nothing is
trending the whole book goes to cash, which is the honest failure mode this
strategy is expected to have (see docs/adr/0070, and KAN-641 for the
portfolio-level correlation/drawdown measurement this is really for).
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.types import Bar, Order, TargetWeight


class TrendFollowing:
    """Per-asset time-series momentum, long-or-cash, rebalanced on a cadence."""

    def __init__(
        self,
        lookback: int = 252,
        skip_recent: int = 21,
        weight: float = 0.9,
        rebalance_days: int = 21,
    ) -> None:
        if lookback <= 0:
            raise ValueError(f"lookback must be positive, got {lookback}")
        if skip_recent < 0:
            raise ValueError(f"skip_recent must be non-negative, got {skip_recent}")
        if skip_recent >= lookback:
            raise ValueError(
                f"skip_recent must be less than lookback, got "
                f"skip_recent={skip_recent} lookback={lookback}"
            )
        if not 0.0 < weight <= 1.0:
            raise ValueError(f"weight must be in (0, 1], got {weight}")
        if rebalance_days <= 0:
            raise ValueError(f"rebalance_days must be positive, got {rebalance_days}")
        self.lookback = lookback
        self.skip_recent = skip_recent
        self.weight = weight
        self.rebalance_days = rebalance_days
        # Counts bars seen; None until the first rebalance has fired.
        self._bar_index = 0
        self._last_rebalance: int | None = None

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        self._bar_index += 1

        # Score only symbols with a full window of history (past+present, never
        # the future). The signal spans `lookback` bars, ending `skip_recent`
        # bars before now, so it needs lookback + skip_recent + 1 closes.
        window = self.lookback + self.skip_recent + 1
        signals: dict[str, float] = {}
        for symbol in bars:
            history = context.history(symbol, window)
            if len(history) < window:
                continue  # still warming up for this name
            start_close = history[0].close
            end_close = history[-1 - self.skip_recent].close
            if start_close <= 0:
                continue  # degenerate series; skip rather than divide by zero
            signals[symbol] = end_close / start_close - 1.0

        if not signals:
            return []  # whole universe still warming up -> stay flat

        # Rebalance on cadence only: the first eligible bar, then every
        # `rebalance_days` bars thereafter. Between rebalances, hold untouched.
        due = (
            self._last_rebalance is None
            or self._bar_index - self._last_rebalance >= self.rebalance_days
        )
        if not due:
            return []
        self._last_rebalance = self._bar_index

        # Absolute signal, per asset: positive trailing return -> in trend.
        # No ranking against other symbols, so the trending set can be any size
        # from zero (all cash) to the whole universe.
        trending = sorted(symbol for symbol, signal in signals.items() if signal > 0)
        per_name = self.weight / len(trending) if trending else 0.0

        intents: list[Order | TargetWeight] = []
        for symbol in sorted(bars):
            target = per_name if symbol in trending else 0.0
            intents.append(TargetWeight(symbol, target))
        return intents
