"""The look-ahead guard (ADR-0001): a strategy can never see a future bar.

A "peeking" strategy asks the context for the widest history window it can and
checks whether any bar lies in the future or whether more than the bars-so-far
are visible. The context exposes no such thing, so the violations stay empty —
this is the structural guarantee that makes backtest results trustworthy.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.broker import SimulatedBroker
from trading.data.fake import FakeAdapter
from trading.engine import Engine
from trading.interfaces import StrategyContext
from trading.types import Bar, Order, Portfolio, TargetWeight


class _Peeker:
    """Records any future bar it can see, and how many bars are visible per call."""

    def __init__(self) -> None:
        self.future_violations: list[datetime] = []
        self.visible_counts: list[int] = []

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        history = context.history("AAA", 10_000)
        self.visible_counts.append(len(history))
        if any(bar.ts > ts for bar in history):
            self.future_violations.append(ts)
        # The most recent visible bar must be the current one, never beyond it.
        assert not history or history[-1].ts == ts
        return []


def test_context_never_exposes_future_bars() -> None:
    bars = [Bar("AAA", datetime(2024, 1, d, tzinfo=UTC), 10, 10, 10, 10, 100) for d in range(1, 6)]
    peeker = _Peeker()
    Engine(FakeAdapter(bars), SimulatedBroker(Portfolio(cash=1_000.0))).run(
        peeker, ["AAA"], datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 5, tzinfo=UTC)
    )

    assert peeker.future_violations == []
    # On bar k only k bars are visible (past + present), never the whole series.
    assert peeker.visible_counts == [1, 2, 3, 4, 5]
