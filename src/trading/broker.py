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

    def fill_price(self, side: Side, reference: float, symbol: str | None = None) -> float:
        """Apply slippage adversely: buys pay up, sells receive less.

        **The venue fee is deliberately not folded in here** (ADR-0060). Rolling a
        proportional fee into the executed price would make it visible to
        ADR-0038's divergence statistic — which is a ratio of fill price to
        reference price — but only by *fabricating* a divergence: the venue's
        realized fee is taken out of the received asset and genuinely is **not** in
        the price it reports, so a model that priced it in would show a 25 bps gap
        against every real fill and invite someone to "correct" a cost model that
        was right. The fee is a separate term because it is a separate thing.

        ``symbol`` is optional and additive (KAN-861, ADR-0063): a caller that
        does not pass one, or a config with no ``symbol_slippage_bps`` at all, gets
        exactly the flat ``slippage_bps`` behavior this method has always had. When
        both are given and ``symbol`` is a key in the map, that rate is used
        instead — the per-symbol override :meth:`CostConfig.symbol_slippage_bps`
        documents, classified once before the run from pre-run ADV, never per-bar.
        """
        tiers = self.config.symbol_slippage_bps
        rate = self.config.slippage_bps
        if symbol is not None and tiers is not None and symbol in tiers:
            rate = tiers[symbol]
        slip = rate / 10_000.0
        factor = 1.0 + slip if side is Side.BUY else 1.0 - slip
        return reference * factor

    def commission(self, qty: float, price: float) -> float:
        """Total cash cost of trading ``qty`` units at ``price``, beyond the price.

        Two terms that are not interchangeable (ADR-0060): a per-unit commission
        (dollars per share, independent of price) plus a proportional venue fee
        (a fraction of notional, independent of quantity). ``price`` is the
        *executed* price from :meth:`fill_price`, because the fee is charged on the
        notional actually transacted.

        **The fee is charged the same on both sides, and that is exact rather than
        an approximation.** Alpaca takes it "on the credited crypto asset/fiat (what
        you receive) per trade": a SELL credits fiat and is docked ``qty*price*f``
        in cash, while a BUY credits coin and is docked ``qty*f`` in *coin*, worth
        ``qty*price*f`` at that same fill price. Both sides are ``qty*price*f``.

        Charging the BUY side in **cash** rather than in kind is the one modelling
        approximation here, and it is deliberate. Paying ``qty*price*(1+f)`` cash
        for ``qty`` coin is not identical to paying ``qty*price`` for ``qty*(1-f)``
        coin — the two differ by ``f²`` of notional, which at 25 bps is 0.000625 bps,
        five orders of magnitude below the slippage term. What it buys in exchange is
        that :meth:`~trading.types.Portfolio.apply_fill` stays the single accounting
        path for both markets (ADR-0002) and ``Fill.qty`` keeps meaning "quantity
        ordered *is* quantity received" — the assumption every downstream consumer
        makes, and whose violation at the venue is exactly the ADR-0058 §7
        ``SHARE_PRECISION`` oversell. The real cost of the choice is not precision,
        it is **funding**: a cash fee needs cash the sizer never reserved, so a
        fully-invested buy can now be rejected. That is measured in ADR-0060, not
        assumed.
        """
        return qty * self.config.commission_per_share + qty * price * (
            self.config.taker_fee_bps / 10_000.0
        )


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
        price = self._costs.fill_price(order.side, bar.open, order.symbol)
        commission = self._costs.commission(order.qty, price)

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
