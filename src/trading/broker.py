"""The simulated broker: the MVP implementation of the Broker seam (ADR-0004).

It models fills conservatively (ADR-0004, Q14): a queued order fills at the *next*
bar's open, moved against you by a configurable slippage, plus commission. This is
the only accounting authority — both backtest and (later) paper trading route
through it, so the two can't drift (ADR-0002).
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.config import CostConfig
from trading.types import Bar, Fill, Order, Portfolio, Side


@dataclass(frozen=True, slots=True)
class CostModel:
    """Turns a reference price into an executed price and a commission."""

    config: CostConfig

    def fill_price(self, side: Side, reference: float) -> float:
        """Apply slippage adversely: buys pay up, sells receive less."""
        slip = self.config.slippage_bps / 10_000.0
        factor = 1.0 + slip if side is Side.BUY else 1.0 - slip
        return reference * factor

    def commission(self, qty: float) -> float:
        """Commission for trading ``qty`` shares."""
        return qty * self.config.commission_per_share


class SimulatedBroker:
    """Queues orders and fills them at the next bar's open (ADR-0001, ADR-0004).

    Rejections (an underfunded buy, an oversell) are recorded rather than raised
    so one bad order doesn't abort a run; inspect :attr:`rejections` after.
    """

    def __init__(self, portfolio: Portfolio, costs: CostConfig | None = None) -> None:
        self._portfolio = portfolio
        self._costs = CostModel(costs or CostConfig())
        self._pending: list[Order] = []
        self.rejections: list[tuple[Order, str]] = []

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    def submit(self, order: Order) -> None:
        """Queue ``order`` for execution on the next :meth:`on_bar`."""
        self._pending.append(order)

    def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
        """Execute every queued order against ``bars`` at their opens.

        Orders on symbols without a bar this timestamp stay queued (they can't be
        priced yet). Everything else is executed or rejected, and the queue is
        left holding only the un-priceable remainder.
        """
        fills: list[Fill] = []
        still_pending: list[Order] = []

        for order in self._pending:
            bar = bars.get(order.symbol)
            if bar is None:
                still_pending.append(order)
                continue
            fill = self._execute(order, bar)
            if fill is not None:
                fills.append(fill)

        self._pending = still_pending
        return fills

    def _execute(self, order: Order, bar: Bar) -> Fill | None:
        price = self._costs.fill_price(order.side, bar.open)
        commission = self._costs.commission(order.qty)

        if order.side is Side.BUY:
            cost = order.qty * price + commission
            if cost > self._portfolio.cash + 1e-9:
                self.rejections.append(
                    (order, f"insufficient cash: need {cost:.2f}, have {self._portfolio.cash:.2f}")
                )
                return None

        fill = Fill(order.symbol, order.side, order.qty, price, commission)
        try:
            self._portfolio.apply_fill(fill)
        except ValueError as exc:  # e.g. an oversell (implicit shorting)
            self.rejections.append((order, str(exc)))
            return None
        return fill
