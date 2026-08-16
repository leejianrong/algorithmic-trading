"""The venue fee ADR-0038's instrument cannot see, recovered from what arrived.

ADR-0038's divergence report is the only thing this bench owns that checks a cost
assumption against reality, and every statistic in it derives from a ratio of *fill
price* to reference price. Alpaca's crypto fee is taken out of the **received
asset** (ADR-0058 §5) and is deliberately kept out of the modelled price
(ADR-0060 §6), so it moves neither side of that ratio. A crypto divergence run can
therefore print a clean slippage verdict while the largest term in the crypto cost
model has never been examined — which is exactly what ADR-0060 recorded, printed
next to ``modelled_taker_fee_bps``, and handed to KAN-710.

ADR-0060 also named the route: ``filled_qty`` is reported **gross**, so the fee is
the gap between what was ordered and what the account was actually credited. This
module is that arithmetic and nothing else. It is a *measurement* module — nothing
here is on the execution path, no broker or engine imports it, and it models
nothing: :class:`~trading.config.CostConfig` owns the modelling side.

Two independent recoveries, because the venue charges the two sides in two
different assets and each is visible in a different ledger:

* **The buy side is visible in quantity.** A BUY credits coin and is docked the fee
  *in coin*, so a symbol's closing position falls short of ``bought - sold`` by
  exactly the fee. :class:`QuantityFee`.
* **The sell side is visible in cash.** A SELL credits fiat and is docked the fee
  *in fiat*, so the account's cash falls short of the traded notionals by exactly
  the fee. :class:`CashFee`.

Neither derives from the other, and they are kept apart rather than averaged: they
are the two halves of ADR-0060 §2's claim that both sides come to ``qty*price*f``,
and agreeing is evidence while a blended number would hide a disagreement.

Both are **exact arithmetic on observed quantities**, not fits. Each also assumes
the account did nothing else in the window — no deposit, no other asset class, no
manual order — which is a property of the *run*, not of the arithmetic, and is why
:func:`quantity_fees` takes the opening position explicitly instead of assuming a
flat start.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from trading.types import Side

#: Basis points in one, as a float so the divisions below stay in floating point.
BPS = 10_000.0


@dataclass(frozen=True, slots=True)
class FeeTier:
    """One row of a published maker/taker schedule, keyed by trailing volume."""

    tier: int
    min_volume_usd: float
    max_volume_usd: float | None
    maker_bps: float
    taker_bps: float

    def covers(self, volume_usd: float) -> bool:
        """Whether ``volume_usd`` falls in this row's half-open band."""
        if volume_usd < self.min_volume_usd:
            return False
        return self.max_volume_usd is None or volume_usd < self.max_volume_usd


#: Alpaca's published crypto fee schedule, read 2026-08-14 from
#: https://docs.alpaca.markets/us/docs/crypto-fees (page stamped "Updated
#: September 24, 2025") and transcribed in full by ADR-0060 §1. Tiered by trailing
#: **30-day crypto** volume; equities volume is explicitly excluded.
#:
#: This is the *published* schedule, kept here so a measured rate can be reconciled
#: against the row it should land on rather than against a remembered number.
#: :data:`trading.config.CRYPTO_TAKER_FEE_BPS` is tier 1's taker column and is what
#: the cost model uses; the tier an *operator's* account is actually charged at is
#: a property of that account, not of the market (ADR-0060 §4).
ALPACA_CRYPTO_FEE_TIERS: tuple[FeeTier, ...] = (
    FeeTier(1, 0.0, 100_000.0, 15.0, 25.0),
    FeeTier(2, 100_000.0, 500_000.0, 12.0, 22.0),
    FeeTier(3, 500_000.0, 1_000_000.0, 10.0, 20.0),
    FeeTier(4, 1_000_000.0, 10_000_000.0, 8.0, 18.0),
    FeeTier(5, 10_000_000.0, 25_000_000.0, 5.0, 15.0),
    FeeTier(6, 25_000_000.0, 50_000_000.0, 2.0, 13.0),
    FeeTier(7, 50_000_000.0, 100_000_000.0, 2.0, 12.0),
    FeeTier(8, 100_000_000.0, None, 0.0, 10.0),
)


def tier_for_volume(
    volume_usd: float,
    tiers: Sequence[FeeTier] = ALPACA_CRYPTO_FEE_TIERS,
) -> FeeTier:
    """The schedule row a trailing 30-day volume falls in.

    Raises on a negative volume rather than clamping to tier 1: a negative
    trailing volume means the caller's reconstruction is wrong, and silently
    answering with the most expensive row would hide that.
    """
    if volume_usd < 0.0:
        raise ValueError(f"trailing volume cannot be negative, got {volume_usd}")
    for tier in tiers:
        if tier.covers(volume_usd):
            return tier
    raise ValueError(f"no tier covers a volume of {volume_usd}")


@dataclass(frozen=True, slots=True)
class TradeLeg:
    """One filled order as the venue reported it: **gross** quantity and price."""

    symbol: str
    side: Side
    qty: float
    price: float

    @property
    def notional(self) -> float:
        return self.qty * self.price


def traded_notional(legs: Iterable[TradeLeg]) -> float:
    """Total gross notional across both sides — what a volume tier is measured on."""
    return sum(leg.notional for leg in legs)


@dataclass(frozen=True, slots=True)
class QuantityFee:
    """The **buy-side** fee for one symbol, read off the position it left behind.

    A BUY credits coin and the fee is taken from that credit, so::

        closing = opening + bought * (1 - f) - sold

    Everything on the right except ``f`` is observed, which makes this exact rather
    than fitted. A full round trip works too: selling the credited quantity leaves
    ``closing = 0`` and ``bought - sold`` is still precisely the fee.

    ``sold`` must be the **gross** quantity delivered (the venue takes a SELL's fee
    in fiat, so the coin leaves the position in full). Mixing a net figure in here
    would understate the fee, which is the one error worth naming.
    """

    symbol: str
    gross_bought: float
    gross_sold: float
    opening_qty: float
    closing_qty: float

    @property
    def missing_qty(self) -> float:
        """Coin ordered that never arrived — the fee, in the asset it was taken in."""
        return self.opening_qty + self.gross_bought - self.gross_sold - self.closing_qty

    @property
    def implied_fee_bps(self) -> float | None:
        """The buy-side fee rate, or ``None`` when nothing was bought to charge it on."""
        if self.gross_bought <= 0.0:
            return None
        return self.missing_qty / self.gross_bought * BPS


@dataclass(frozen=True, slots=True)
class CashFee:
    """The **sell-side** fee, read off cash across the whole session.

    A SELL credits fiat and the fee is taken from that credit, while a BUY pays its
    notional in full and is charged in coin instead. So::

        closing_cash = opening_cash - buy_notional + sell_notional * (1 - f)

    The shortfall is therefore ``sell_notional * f`` exactly, whatever the prices
    did in between — the notionals are the *realized* ones, so price movement is
    already inside them and cancels.

    This is one number for the account rather than one per symbol, because cash is
    one pool. It is only the sell-side fee, and it is only valid if nothing else
    moved cash in the window.
    """

    buy_notional: float
    sell_notional: float
    opening_cash: float
    closing_cash: float

    @property
    def missing_cash(self) -> float:
        """Fiat the sells should have credited and did not."""
        expected = self.opening_cash - self.buy_notional + self.sell_notional
        return expected - self.closing_cash

    @property
    def implied_fee_bps(self) -> float | None:
        """The sell-side fee rate, or ``None`` when nothing was sold to charge it on."""
        if self.sell_notional <= 0.0:
            return None
        return self.missing_cash / self.sell_notional * BPS


def quantity_fees(
    legs: Iterable[TradeLeg],
    closing: dict[str, float],
    opening: dict[str, float] | None = None,
) -> list[QuantityFee]:
    """One :class:`QuantityFee` per symbol traded, in first-seen order.

    A symbol that was traded and is absent from ``closing`` closed at zero — a fully
    exited position is not reported as a position, so treating "missing" as "flat"
    is reading the venue rather than guessing.
    """
    opened = opening or {}
    bought: dict[str, float] = {}
    sold: dict[str, float] = {}
    order: list[str] = []
    for leg in legs:
        if leg.symbol not in bought:
            order.append(leg.symbol)
            bought[leg.symbol] = 0.0
            sold[leg.symbol] = 0.0
        if leg.side is Side.BUY:
            bought[leg.symbol] += leg.qty
        else:
            sold[leg.symbol] += leg.qty
    return [
        QuantityFee(
            symbol=symbol,
            gross_bought=bought[symbol],
            gross_sold=sold[symbol],
            opening_qty=opened.get(symbol, 0.0),
            closing_qty=closing.get(symbol, 0.0),
        )
        for symbol in order
    ]


def cash_fee(legs: Iterable[TradeLeg], opening_cash: float, closing_cash: float) -> CashFee:
    """The session's :class:`CashFee`, summing each side's realized notional."""
    buy_notional = 0.0
    sell_notional = 0.0
    for leg in legs:
        if leg.side is Side.BUY:
            buy_notional += leg.notional
        else:
            sell_notional += leg.notional
    return CashFee(
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
    )
