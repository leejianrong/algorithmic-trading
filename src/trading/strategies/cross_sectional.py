"""Cross-sectional rank-and-hold-top-K: relative-strength selection.

Where momentum and mean-reversion score each symbol *in isolation* (an absolute
signal per name), this strategy scores every symbol *against the others* and
holds only the strongest. On each rebalance it ranks the universe by trailing
total return over ``lookback`` bars, holds the top ``top_k`` at equal weight
(``weight / top_k`` each), and targets zero for everything else — so a name that
drops out of the top-K is exited. Long-or-flat only (no shorting, ADR-0011).

Ranks would thrash if recomputed daily, so it only rebalances every
``rebalance_days`` bars (≈ monthly by default); between rebalances it holds its
current book untouched. It fits the existing ``Strategy`` seam with no engine or
interface change — it reads only ``context.history`` (past+present, never the
future, ADR-0001) and emits ``TargetWeight``\\ s the V2 sizing layer resolves.

Note the K↔position-cap interaction: ``weight / top_k`` must stay under the
per-symbol position cap (ADR-0009) or the guardrails clamp each entry. The
defaults (``weight=0.9``, ``top_k=8`` → ~0.1125 each) sit safely under the 0.25
default cap; a small ``top_k`` with a large ``weight`` would be clamped.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.types import Bar, Order, TargetWeight


class CrossSectional:
    """Rank the universe by trailing return; hold the top-K at equal weight."""

    def __init__(
        self,
        lookback: int = 120,
        top_k: int = 8,
        weight: float = 0.9,
        rebalance_days: int = 21,
    ) -> None:
        if lookback <= 0:
            raise ValueError(f"lookback must be positive, got {lookback}")
        if top_k <= 0:
            raise ValueError(f"top_k must be positive, got {top_k}")
        if not 0.0 < weight <= 1.0:
            raise ValueError(f"weight must be in (0, 1], got {weight}")
        if rebalance_days <= 0:
            raise ValueError(f"rebalance_days must be positive, got {rebalance_days}")
        self.lookback = lookback
        self.top_k = top_k
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

        # Score only symbols with a full lookback window of history (past+present,
        # never the future). Trailing total return spans `lookback` bars, so we
        # need lookback + 1 closes: return = close[-1] / close[0] - 1.
        scores: dict[str, float] = {}
        for symbol in bars:
            history = context.history(symbol, self.lookback + 1)
            if len(history) < self.lookback + 1:
                continue  # still warming up for this name
            first = history[0].close
            if first <= 0:
                continue  # degenerate series; skip rather than divide by zero
            scores[symbol] = history[-1].close / first - 1.0

        if not scores:
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

        # Rank by trailing return descending; break ties by symbol for
        # determinism. Hold the top-K at equal weight, exit everything else.
        ranked = sorted(scores, key=lambda s: (-scores[s], s))
        winners = set(ranked[: self.top_k])
        per_name = self.weight / self.top_k

        intents: list[Order | TargetWeight] = []
        for symbol in sorted(bars):
            target = per_name if symbol in winners else 0.0
            intents.append(TargetWeight(symbol, target))
        return intents
