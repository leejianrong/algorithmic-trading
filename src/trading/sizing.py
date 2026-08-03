"""Target-weight sizing: turn strategy intent into concrete orders (ADR-0007).

A strategy may emit a :class:`~trading.types.TargetWeight` ("hold 20% of equity in
AAPL") instead of a raw order. The sizing layer resolves that against the current
portfolio: desired shares = ``weight * equity / price``, and the order is the
*delta* from what's already held (a rebalance). Shares are fractional (ADR-0011),
rounded to a fixed precision, and dust deltas are dropped.

Sizing is pure target-weight — realized position value equals ``weight * equity``,
so weights mean what they say. It does not reserve for costs or cap exposure; a
strategy that targets ~100% should leave a little headroom (buys fill at the next
open plus slippage), and the exposure cap that enforces this arrives with the
guardrails in V3 (ADR-0009).
"""

from __future__ import annotations

from trading.types import Order, Portfolio, Side, TargetWeight

# Shares are rounded to this many decimals; deltas smaller than one unit at this
# precision are dropped as dust rather than emitting a no-op order.
SHARE_PRECISION = 6
_DUST = 10.0**-SHARE_PRECISION


def size(
    intents: list[Order | TargetWeight],
    portfolio: Portfolio,
    prices: dict[str, float],
) -> list[Order]:
    """Resolve a bar's intents into orders against ``portfolio`` at ``prices``.

    All target weights size against the *same* pre-trade equity snapshot, so a
    multi-symbol rebalance is computed consistently. ``prices`` are the
    decision-bar closes (never the future); the resulting orders fill on the next
    bar (ADR-0001).
    """
    equity = portfolio.equity(prices)
    orders: list[Order] = []

    for intent in intents:
        if isinstance(intent, Order):
            orders.append(intent)
            continue

        order = _size_one(intent, portfolio, prices, equity)
        if order is not None:
            orders.append(order)

    return orders


def _size_one(
    target: TargetWeight,
    portfolio: Portfolio,
    prices: dict[str, float],
    equity: float,
) -> Order | None:
    price = prices.get(target.symbol)
    if price is None:
        raise ValueError(f"cannot size {target.symbol!r}: no price this bar")
    if price <= 0:
        raise ValueError(f"cannot size {target.symbol!r}: non-positive price {price}")

    desired = target.weight * equity / price
    current = portfolio.position(target.symbol).qty
    delta = round(desired - current, SHARE_PRECISION)

    if delta > _DUST:
        return Order(target.symbol, Side.BUY, delta)
    if delta < -_DUST:
        return Order(target.symbol, Side.SELL, -delta)
    return None
