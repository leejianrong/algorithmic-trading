#!/usr/bin/env python3
"""Read the crypto venue fee off the account, which the price report cannot (KAN-710).

ADR-0038's divergence report compares fill *prices*; Alpaca's crypto fee is taken
out of the received asset and is deliberately outside the modelled price
(ADR-0060 §6). So the largest term in the crypto cost model is invisible to the one
instrument this bench has for checking a cost model, and ADR-0060 printed
``NOT MEASURED BY THIS REPORT`` next to it rather than leaving that implicit.

This is the other half: the fee recovered from what actually arrived. It reads the
venue's own closed orders and the positions and cash they left, and hands them to
:mod:`trading.fees`, which owns the arithmetic (and its tests). Read-only -- every
call here is a GET, nothing is submitted and nothing is cancelled.

It also reconstructs the account's trailing 30-day crypto notional, because the
published schedule is tiered on exactly that and the ``TradeAccount`` object
exposes no volume or tier field (ADR-0060's closing note). **The tier an operator
is charged at is a property of their account, not of the market**, so a measurement
that does not state it is not reproducible.

Two calls go straight to the SDK rather than through the ``AlpacaClient`` seam,
for the reason ``paper_preflight.py`` already states: the seam has no
``list_orders``, widening it is an ADR-0017 decision, and a live *run* needs
neither. Confined to ``scripts/``, which is the rule ADR-0017 actually states.

Usage::

    uv run --env-file .env python scripts/crypto_fee_reconcile.py \\
        --since 2026-08-16T09:05:00Z --opening-cash 99572.77
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from trading.fees import TradeLeg, cash_fee, quantity_fees, tier_for_volume, traded_notional
from trading.types import Side

TIER_WINDOW = timedelta(days=30)


def _parse_when(text: str) -> datetime:
    """An ISO instant, tolerating the trailing ``Z`` the rest of the bench prints."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _closed_orders(after: datetime) -> list[object]:
    """Every closed order since ``after``, paged until the venue stops answering."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    client = TradingClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )
    orders: list[object] = []
    cursor = after
    while True:
        batch = client.get_orders(
            filter=GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, after=cursor, limit=500, direction="asc"
            )
        )
        if not batch:
            break
        orders.extend(batch)
        if len(batch) < 500:
            break
        cursor = batch[-1].submitted_at
    return orders


def _legs(orders: list[object]) -> list[TradeLeg]:
    """Filled crypto orders as gross legs. A slash is the venue's own pair marker."""
    legs: list[TradeLeg] = []
    for order in orders:
        symbol = str(getattr(order, "symbol", ""))
        if "/" not in symbol:
            continue  # an equity fill; a different fee schedule entirely
        qty = float(getattr(order, "filled_qty", 0) or 0)
        price = float(getattr(order, "filled_avg_price", 0) or 0)
        if qty <= 0 or price <= 0:
            continue  # canceled, expired, or filled nothing -- no notional either way
        side = Side.BUY if "buy" in str(getattr(order, "side", "")).lower() else Side.SELL
        legs.append(TradeLeg(symbol=symbol, side=side, qty=qty, price=price))
    return legs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        required=True,
        help="Session start, ISO-8601 (e.g. 2026-08-16T09:05:00Z). Fills before it are excluded.",
    )
    parser.add_argument(
        "--opening-cash",
        type=float,
        default=None,
        help="Account cash at --since. Without it the sell-side (cash) reading is skipped.",
    )
    args = parser.parse_args()
    since = _parse_when(args.since)

    from trading.data.alpaca_client import ASSET_CLASS_CRYPTO, RealAlpacaClient

    # `asset_class` is load-bearing, not decoration. Alpaca echoes `BTC/USD` on the
    # order and `BTCUSD` on the position it creates, and only a crypto-class client
    # canonicalizes the second back to the first (ADR-0058 §2). Built as the default
    # equity class, every position key here misses its order key, every closing
    # quantity reads zero, and the implied fee comes out at 10,000 bps -- i.e. "the
    # whole position vanished". Observed on the first run of this script, and loudly
    # wrong rather than subtly wrong, which is the only reason it was caught.
    seam = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
    account = seam.get_account()
    closing = {p.symbol: p.qty for p in seam.list_positions()}

    tier_start = datetime.now(UTC) - TIER_WINDOW
    trailing_orders = _closed_orders(min(tier_start, since))
    session_orders = [
        o
        for o in trailing_orders
        if (filled_at := getattr(o, "filled_at", None)) is not None and filled_at >= since
    ]
    trailing_legs = _legs(trailing_orders)
    session_legs = _legs(session_orders)

    trailing_volume = traded_notional(trailing_legs)
    tier = tier_for_volume(trailing_volume)

    print("Crypto fee reconciliation -- read-only (KAN-710, ADR-0060 §2's method)")
    print()
    print(f"Session start          {since.isoformat()}")
    print(f"Filled crypto orders   {len(session_legs)} in session, {len(trailing_legs)} in 30d")
    print(f"Trailing 30d notional  ${trailing_volume:,.2f}")
    print(
        f"  -> published tier {tier.tier} (${tier.min_volume_usd:,.0f}"
        f"{'+' if tier.max_volume_usd is None else f'-${tier.max_volume_usd:,.0f}'}): "
        f"maker {tier.maker_bps:g} bps, TAKER {tier.taker_bps:g} bps"
    )
    print("     Every order this bench emits is a market order, so every fill is taker.")
    print(f"Session notional       ${traded_notional(session_legs):,.2f}")
    print(f"Account now            cash ${account.cash:,.2f}  equity ${account.equity:,.2f}")
    held = ", ".join(f"{s} {q:g}" for s, q in sorted(closing.items())) or "none"
    print(f"Positions held         {held}")
    print()

    print("BUY-side fee, taken in the received coin (closing position vs gross ordered):")
    print(f"  {'symbol':10s} {'bought':>16s} {'sold':>16s} {'closing':>16s} {'implied bps':>12s}")
    per_symbol = quantity_fees(session_legs, closing)
    measured = [(f.implied_fee_bps, f.gross_bought) for f in per_symbol if f.implied_fee_bps]
    for fee in per_symbol:
        rate = fee.implied_fee_bps
        shown = "n/a (no buys)" if rate is None else f"{rate:12.4f}"
        print(
            f"  {fee.symbol:10s} {fee.gross_bought:16.9f} {fee.gross_sold:16.9f} "
            f"{fee.closing_qty:16.9f} {shown:>12s}"
        )
    if measured:
        # Weighted by gross bought, so a pair traded once does not outvote one
        # traded twenty times -- the pooled figure is the account's actual rate.
        pooled = sum(r * w for r, w in measured) / sum(w for _, w in measured)
        print(f"  {'pooled':10s} {'':16s} {'':16s} {'':16s} {pooled:12.4f}")
        print(f"  (weighted by gross bought; the published taker row is {tier.taker_bps:g} bps)")
    print()

    if args.opening_cash is None:
        print("SELL-side fee, taken in the received fiat: SKIPPED -- pass --opening-cash.")
    else:
        cash = cash_fee(session_legs, args.opening_cash, account.cash)
        rate = cash.implied_fee_bps
        print("SELL-side fee, taken in the received fiat (cash vs realized notionals):")
        print(f"  buy notional   ${cash.buy_notional:,.2f}")
        print(f"  sell notional  ${cash.sell_notional:,.2f}")
        print(f"  cash           ${cash.opening_cash:,.2f} -> ${cash.closing_cash:,.2f}")
        print(f"  missing cash   ${cash.missing_cash:,.4f}")
        print("  implied bps    " + ("n/a (nothing sold)" if rate is None else f"{rate:.4f}"))
        print("  NOTE: valid only if nothing else moved cash in the window -- no equity")
        print("        fill, no deposit, no manual order.")
    print()
    print("The two readings are independent: coin measures the buy side, cash the sell")
    print("side. ADR-0060 §2 argues both come to qty*price*f, so agreement is evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
