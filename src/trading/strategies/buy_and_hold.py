"""Buy-and-hold: the correctness baseline.

On the first bar it fixes an equal weight in each of that day's symbols; the
sizing layer (ADR-0007) converts those weights to fractional-share orders and it
holds thereafter. Fractional shares (ADR-0011) let it deploy essentially all the
cash even on high-priced symbols.

**The allocation is one-shot in intent, not in attempt** (ADR-0037 as amended).
An order sized from bar *t*'s close fills at bar *t+1*'s open plus slippage
(ADR-0001/0004), so an overnight gap up of more than the ~20 bps of headroom in
:data:`INVESTED_WEIGHT` overshoots the cash and the broker rejects it. This
strategy used to latch a private ``_invested`` flag *before* returning its
intents, so that single rejection left the book 100% in cash for the rest of the
run — silently, because a rejection is recorded, not raised. It now keeps the
entry intent alive until the position actually exists, and only then stops
trading forever. The universe and the weights are still frozen on the first bar,
so this is a retry of the *same* allocation, never a rebalance.
"""

from __future__ import annotations

from datetime import datetime

from trading.interfaces import StrategyContext
from trading.sizing import SHARE_PRECISION
from trading.types import Bar, Order, Side, TargetWeight

# Target just under 100% so the initial buys still fit after the next-open fill
# picks up slippage (a full 100% target would overshoot cash and be rejected).
# The headroom is a courtesy, not a guarantee — a large enough overnight gap
# still overshoots, which is exactly why the entry is retried rather than latched.
INVESTED_WEIGHT = 0.998


class BuyAndHold:
    """Allocate once, equally weighted, then hold."""

    def __init__(self) -> None:
        # The frozen first-bar allocation: symbol -> target weight. ``None`` until
        # the first bar with data decides it.
        self._allocation: dict[str, float] | None = None
        # Latches once every allocated symbol is actually held. From then on the
        # strategy is inert for the rest of the run.
        self._established = False

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._established or not bars:
            return []

        if self._allocation is None:
            # A book that already holds something is not ours to allocate — a
            # resumed paper session, say. Stand down permanently, exactly as the
            # previous ``context.portfolio.positions`` guard did.
            if context.portfolio.positions:
                self._established = True
                return []
            self._allocation = {symbol: INVESTED_WEIGHT / len(bars) for symbol in sorted(bars)}

        # Retry only the legs that never got established. A leg that *is* held is
        # left strictly alone, whatever it is now worth: re-targeting a held
        # symbol would make this a constant-mix rebalancer, not buy-and-hold.
        held = context.portfolio.positions
        missing = [symbol for symbol in self._allocation if symbol not in held]
        if not missing:
            self._established = True
            return []

        # A missing leg with no bar this timestamp cannot be priced, so it cannot
        # be sized (ADR-0006); it stays missing and is retried when it next trades.
        tradable = [symbol for symbol in missing if symbol in bars]
        if not tradable:
            return []

        if not held:
            # Nothing is held, so the sizing layer's equity *is* the cash and the
            # allocation can be stated as the weights it was defined as. This is
            # the first-bar path, unchanged, so an entry that clears on the first
            # attempt produces exactly the orders it always did.
            return [TargetWeight(symbol, self._allocation[symbol]) for symbol in tradable]

        # Partly established: some legs filled, one or more did not. Re-asserting
        # a weight of *equity* here would demand cash the filled legs have already
        # spent, and would be rejected on every remaining bar of the run. The
        # allocation's intent — split the money equally across the universe — is
        # honoured by funding the stragglers from the cash that actually remains,
        # with the same headroom the weight carries. When there is no cash left the
        # quantity rounds to zero and nothing is submitted, so an unfundable leg
        # costs one rejection, not one per bar.
        budget = INVESTED_WEIGHT * max(context.portfolio.cash, 0.0) / len(tradable)
        orders: list[Order | TargetWeight] = []
        for symbol in tradable:
            qty = round(budget / bars[symbol].close, SHARE_PRECISION)
            if qty > 0:
                orders.append(Order(symbol, Side.BUY, qty))
        return orders
