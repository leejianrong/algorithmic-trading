"""Core value types shared across the bench.

These are the decided, stable contracts from the ADRs: a price :class:`Bar`
(adjusted, per ADR-0008), an :class:`Order`, a :class:`TargetWeight` (the sizing
intent from ADR-0007), a :class:`Position`, and a multi-symbol :class:`Portfolio`
(ADR-0006). They carry only accounting logic that is fixed by those decisions;
the engine, broker, and sizing layer that *drive* them arrive in later slices.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

# Share quantities are fractional (ADR-0011), so "flat" and "over-sell" are
# tolerance comparisons, not exact-zero ones. Positions closer than this to zero
# are treated as closed; a sell within this slack of the held size is allowed.
SHARE_EPS = 1e-9


class Side(StrEnum):
    """Order direction."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Supported order types."""

    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily, split/dividend-adjusted OHLCV bar for a single symbol.

    The timestamp is timezone-aware so the same type can carry intraday bars
    later without a rewrite (ADR-0005). Prices are adjusted (ADR-0008).
    """

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError(f"Bar.ts must be timezone-aware, got naive {self.ts!r}")
        if self.high < self.low:
            raise ValueError(f"Bar high {self.high} < low {self.low} for {self.symbol}")
        if self.volume < 0:
            raise ValueError(f"Bar volume must be non-negative, got {self.volume}")


@dataclass(frozen=True, slots=True)
class Order:
    """An instruction to trade a (possibly fractional) number of shares.

    Produced either directly by a strategy or by the sizing layer from a
    :class:`TargetWeight` (ADR-0007). Quantity is a positive share count —
    fractional is allowed (ADR-0011); direction is carried by :attr:`side`.
    """

    symbol: str
    side: Side
    qty: float
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"Order.qty must be a positive share count, got {self.qty}")
        if self.type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("A limit order requires a limit_price")
        if self.type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("A market order must not carry a limit_price")


@dataclass(frozen=True, slots=True)
class TargetWeight:
    """A strategy's intent to hold ``weight`` of current equity in ``symbol``.

    The engine's sizing layer (V2) turns this into a fractional-share
    :class:`Order`; the guardrails clamp anything over the position cap
    (ADR-0007, ADR-0009, ADR-0011).
    """

    symbol: str
    weight: float

    def __post_init__(self) -> None:
        if not -1.0 <= self.weight <= 1.0:
            raise ValueError(f"TargetWeight.weight must be in [-1, 1], got {self.weight}")


@dataclass(frozen=True, slots=True)
class Fill:
    """The result of a broker executing an order."""

    symbol: str
    side: Side
    qty: float
    price: float
    commission: float = 0.0

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"Fill.qty must be positive, got {self.qty}")
        if self.price < 0:
            raise ValueError(f"Fill.price must be non-negative, got {self.price}")
        if self.commission < 0:
            raise ValueError(f"Fill.commission must be non-negative, got {self.commission}")


@dataclass(frozen=True, slots=True)
class Position:
    """A holding in one symbol: signed (fractional) share count and average price."""

    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0

    def market_value(self, price: float) -> float:
        """Mark-to-market value of the holding at ``price``."""
        return self.qty * price


@dataclass(slots=True)
class Portfolio:
    """Cash plus per-symbol positions, with multi-symbol accounting (ADR-0006).

    :meth:`apply_fill` is the single accounting path a fill takes; the simulated
    and (later) real brokers both route through it so backtest and paper stay
    identical (ADR-0002).
    """

    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def position(self, symbol: str) -> Position:
        """Current position in ``symbol`` (a flat zero position if none)."""
        return self.positions.get(symbol, Position(symbol))

    def equity(self, prices: dict[str, float]) -> float:
        """Total equity: cash plus every position marked at ``prices``.

        Every held symbol must have a price; a missing one is an error rather
        than a silent zero, so stale marks can't inflate equity.
        """
        total = self.cash
        for symbol, pos in self.positions.items():
            if abs(pos.qty) <= SHARE_EPS:
                continue
            if symbol not in prices:
                raise KeyError(f"No price to mark held position {symbol!r}")
            total += pos.market_value(prices[symbol])
        return total

    def gross_exposure(self, prices: dict[str, float]) -> float:
        """Gross exposure as a fraction of equity: Σ|position value| / equity."""
        eq = self.equity(prices)
        if eq <= 0:
            raise ValueError("Cannot compute exposure with non-positive equity")
        gross = sum(
            abs(pos.market_value(prices[symbol]))
            for symbol, pos in self.positions.items()
            if abs(pos.qty) > SHARE_EPS
        )
        return gross / eq

    def apply_fill(self, fill: Fill) -> None:
        """Update cash and the affected position from an executed ``fill``.

        Cash always pays the commission. A buy increases shares and blends the
        average price; a sell reduces shares and realizes against cash. Selling
        more than held is rejected — the bench never shorts implicitly.
        """
        pos = self.position(fill.symbol)
        signed = fill.qty if fill.side is Side.BUY else -fill.qty
        new_qty = pos.qty + signed

        if fill.side is Side.SELL and fill.qty > pos.qty + SHARE_EPS:
            raise ValueError(
                f"Cannot sell {fill.qty} of {fill.symbol}; only {pos.qty} held "
                "(implicit shorting is disallowed)"
            )

        # Cash: buys spend, sells receive; commission always costs.
        self.cash -= signed * fill.price
        self.cash -= fill.commission

        if abs(new_qty) <= SHARE_EPS:
            self.positions.pop(fill.symbol, None)
            return

        if fill.side is Side.BUY:
            # Blend the average entry price over the enlarged position.
            cost_basis = pos.qty * pos.avg_price + fill.qty * fill.price
            avg_price = cost_basis / new_qty
        else:
            # A partial sell leaves the average entry price unchanged.
            avg_price = pos.avg_price

        self.positions[fill.symbol] = replace(pos, qty=new_qty, avg_price=avg_price)
