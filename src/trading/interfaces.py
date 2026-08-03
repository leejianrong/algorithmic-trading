"""Dependency-injection seams (dev-playbook principle 2).

The engine depends only on these protocols, never on a concrete implementation,
which is what lets backtest and paper share one execution path (ADR-0002) and
lets a real broker or a new data source drop in later (ADR-0003, ADR-0004). Each
seam has at least one real implementation and one in-memory fake so the fast test
layer needs zero infrastructure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from trading.types import Bar, Fill, Order, Portfolio


@runtime_checkable
class DataAdapter(Protocol):
    """A source of normalized, adjusted daily bars (ADR-0003, ADR-0008).

    Implementations: ``YFinanceAdapter`` (network, cached) and ``CSVAdapter``
    for real data; ``FakeAdapter`` (in :mod:`trading.data.fake`) for fast tests.
    """

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        """Return ``symbol``'s daily bars in ``[start, end]``, ascending by time."""
        ...


@runtime_checkable
class Broker(Protocol):
    """Order execution and position/cash state (ADR-0004).

    Implementations: ``SimulatedBroker`` (MVP) and, next milestone, an Alpaca
    broker behind the same seam.
    """

    def submit(self, order: Order) -> None:
        """Accept an order for execution on a subsequent bar."""
        ...

    def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
        """Advance to a new timestamp's bars and return any resulting fills."""
        ...

    def cash(self) -> float:
        """Available cash."""
        ...


@runtime_checkable
class Strategy(Protocol):
    """User trading logic, called once per timestamp (ADR-0001, ADR-0006).

    ``context`` exposes positions, cash, equity, and a rolling per-symbol history
    window — never future bars, which is what makes look-ahead structurally
    impossible (ADR-0001).
    """

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | object]:
        """Return orders or target weights in response to a timestamp's bars."""
        ...


@runtime_checkable
class StrategyContext(Protocol):
    """Read-only view a strategy is given each bar. Never exposes the future."""

    @property
    def portfolio(self) -> Portfolio:
        """The current portfolio (cash and positions)."""
        ...

    def history(self, symbol: str, lookback: int) -> list[Bar]:
        """The last ``lookback`` bars for ``symbol`` up to and including now."""
        ...


@runtime_checkable
class RiskGuardrails(Protocol):
    """Enforced pre-trade and portfolio-level risk limits (ADR-0009)."""

    def check(
        self,
        order: Order,
        portfolio: Portfolio,
        prices: dict[str, float],
    ) -> Order | None:
        """Return an accepted (possibly clamped) order, or ``None`` to reject."""
        ...

    def halted(self, portfolio: Portfolio, prices: dict[str, float]) -> bool:
        """Whether the drawdown / daily-loss kill switch has tripped."""
        ...
