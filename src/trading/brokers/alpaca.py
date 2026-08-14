"""The Alpaca paper broker: submit-then-poll behind the Broker seam (ADR-0020).

Unlike :class:`~trading.broker.SimulatedBroker`, which *is* the accounting
authority and prices fills itself, :class:`AlpacaBroker` delegates execution to a
real venue. It submits a market order, polls the order until it settles or a
timeout elapses, and then reconciles its :class:`~trading.types.Portfolio`
straight from the account and positions Alpaca reports. Alpaca is the source of
truth: cash is never re-simulated and no :class:`~trading.broker.CostModel` is
applied, because the returned fill already carries real costs (paper commission is
zero). This keeps the *one execution path* invariant (ADR-0002) intact -- the
engine drives the very same ``submit`` / ``on_bar`` seam -- while accepting that
paper accounting is authoritative rather than byte-identical to a backtest.

The poll uses the injected :class:`~trading.clock.Clock`, so a
:class:`~trading.data.alpaca_client.FakeAlpacaClient` with ``auto_fill=True``
settles on the first poll with no real waiting, and a scripted pending order can
be driven through the timeout branch deterministically under
:class:`~trading.clock.FakeClock`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

from trading.calendar import US_EQUITY, MarketCalendar
from trading.clock import Clock, WallClock
from trading.data.alpaca_client import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_US_EQUITY,
    STATUS_FILLED,
    TERMINAL_STATUSES,
    AlpacaClient,
    AlpacaOrder,
    OrderRejectedError,
    RealAlpacaClient,
)
from trading.sizing import SHARE_PRECISION
from trading.types import Bar, Fill, Order, Portfolio, Position, Side

logger = logging.getLogger(__name__)

# The most a *rounded* exit can exceed the holding it was rounded from: half a
# unit at the sizer's precision (ADR-0058).
#
# `sizing.size` emits `round(desired - current, SHARE_PRECISION)`, and on US
# equities that is exact, because Alpaca quantizes fractional shares at six
# decimals or fewer -- so `current` already has no seventh digit to round. Crypto
# publishes `min_trade_increment = 1e-9`, so a reconciled quantity routinely
# carries nine decimals and rounding to six rounds **up** about half the time.
#
# Observed live on 2026-08-14, a full exit of a real position::
#
#     insufficient balance for ETH (requested: 13.338989, available: 13.33898895)
#
# That is a long-or-flat position this bench could not sell -- ADR-0011's only exit
# blocked by arithmetic, which ADR-0013/0031 and ADR-0036 each go out of their way
# to prevent.
_MAX_ROUNDING_OVERSELL = 0.5 * 10.0**-SHARE_PRECISION

# How long a single ``on_bar`` will poll a pending order before giving up and
# leaving it to a later bar, and how long to wait between polls. Kept short
# relative to the daily bar cadence: in live mode a market order placed after a
# completed daily bar fills at the *next* session open (possibly hours away), so
# blocking on_bar until then is wrong -- the order stays pending and is retried
# on a subsequent on_bar, reconciling once the venue reports the fill (ADR-0020).
DEFAULT_POLL_TIMEOUT = timedelta(seconds=30)
DEFAULT_POLL_INTERVAL = timedelta(seconds=2)


class AlpacaBroker:
    """A submit-then-poll paper broker over the Alpaca client seam (ADR-0020).

    Implements the :class:`~trading.interfaces.Broker` protocol so the engine
    drives it exactly as it drives :class:`~trading.broker.SimulatedBroker`.
    Orders that settle are turned into :class:`~trading.types.Fill`\\ s at the
    venue's reported price and quantity; the portfolio is then reconciled from the
    account rather than mutated locally.

    An order the venue *ends* without filling it -- ``rejected``, ``canceled``,
    ``expired``, ``replaced`` -- is recorded on :attr:`rejections` rather than
    raised, so one bad order never aborts a run; one still *working* at the poll
    timeout is neither, it simply stays pending and is retried on the next bar.
    An order the venue refuses at *submit* time, before it exists at all, lands
    there too (ADR-0041) -- the same promise, on the other end of the lifecycle.
    :attr:`rejections` holds ``(Order, reason)`` exactly as
    :class:`~trading.broker.SimulatedBroker` does, because
    :class:`~trading.engine.Engine` merges both into the same
    ``BacktestResult.rejections`` (ADR-0036).

    An order the broker is already working in the same symbol and direction makes
    a new one a **duplicate**, and :meth:`submit` refuses it rather than sending
    it (ADR-0036 as amended). A parked order leaves the account flat, so a
    target-weight strategy asks again every bar; without this the orders compound
    for as long as the venue holds them and then all fill at once. Nothing else in
    the bench needs this -- :class:`~trading.broker.SimulatedBroker` fills within
    the bar, so it never has a working order to duplicate -- which is why the guard
    lives here and the backtest path is untouched.

    ``calendar`` selects the venue for a client this broker builds itself
    (ADR-0058), the same value and for the same reason
    :class:`~trading.data.alpaca_adapter.AlpacaAdapter` takes one. **No logic in
    this class is asset-class aware**, which is the finding rather than the
    omission: the poll loop, the terminal-status set, the duplicate guard's
    ``(symbol, side)`` key (ADR-0036) and the reconcile-from-the-account rule
    (ADR-0020) were all written without a market in mind and all held against a
    real crypto fill. Everything that had to change lives one layer down in the
    client, where the venue's own inconsistencies are.

    One consequence of the venue and not of this code, recorded in ADR-0058: a
    crypto order is ``GTC`` because the venue refuses ``DAY``, so an unfilled one
    **never expires**. On equities a parked order is cleaned up by the close
    (ADR-0036); here it is not, so a session that ends with a working crypto order
    leaves it working. ADR-0052 already learned that a session ends holding its
    book; on this venue that is now also true of its orders indefinitely.
    """

    def __init__(
        self,
        client: AlpacaClient | None = None,
        *,
        clock: Clock | None = None,
        poll_timeout: timedelta = DEFAULT_POLL_TIMEOUT,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        calendar: MarketCalendar = US_EQUITY,
    ) -> None:
        # Default to a live client / wall clock only when nothing is injected, so
        # the fast test layer never touches the network or a real clock.
        asset_class = ASSET_CLASS_CRYPTO if calendar.is_continuous else ASSET_CLASS_US_EQUITY
        self._client: AlpacaClient = (
            client if client is not None else RealAlpacaClient(asset_class=asset_class)
        )
        self._clock: Clock = clock if clock is not None else WallClock()
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self._pending: list[str] = []
        # What each pending order id was asked to do, so a rejection can name the
        # Order rather than the id (ADR-0036): BacktestResult.rejections and
        # report.result_to_dict both read order.symbol / .qty / .side.
        self._requested: dict[str, Order] = {}
        self.rejections: list[tuple[Order, str]] = []
        # Reconcile once up front so the portfolio is valid before the first bar.
        self._portfolio = self._reconcile()

    @property
    def portfolio(self) -> Portfolio:
        """The last portfolio reconciled from the Alpaca account (authoritative)."""
        return self._portfolio

    @property
    def pending_order_ids(self) -> tuple[str, ...]:
        """Ids of orders submitted but not yet settled, in submission order.

        A public read-only view of the retry set, so a test (or an operator
        inspecting a live session) can see what the broker is still waiting on
        without reaching into a private attribute.
        """
        return tuple(self._pending)

    def submit(self, order: Order) -> None:
        """Place ``order`` as a market order and track its id as pending.

        The fill is realized later, in :meth:`on_bar`, once the venue reports the
        order settled (ADR-0001: an order submitted on bar *t* fills no earlier
        than *t+1*).

        **Unless the venue is already working the same intent.** A parked order
        leaves the account -- and therefore the reconciled portfolio -- flat, so a
        target-weight strategy re-emits its unmet target on every bar; submitting
        each one stacks orders that all fill at the next open. When
        :meth:`_working_order_id` finds a same-symbol, same-side order still
        working, this records the new one on :attr:`rejections` and does **not**
        send it (ADR-0036 as amended).

        **And the venue gets a veto of its own.** An order Alpaca refuses outright
        -- insufficient buying power, an unknown asset, or the "potential wash
        trade" refusal it answers an opposite-side order with while one is working
        -- arrives as an :class:`~trading.data.alpaca_client.OrderRejectedError`
        and is recorded on :attr:`rejections` too, rather than propagating out of
        the engine and ending the session (ADR-0041). A failure that means *we
        could not ask* still propagates: it is not a decision about this order.
        """
        order = self._trim_rounding_oversell(order)
        working_id = self._working_order_id(order.symbol, order.side)
        if working_id is not None:
            working = self._requested[working_id]
            self.rejections.append(
                (
                    order,
                    f"not submitted: order {working_id} ({working.qty:g} "
                    f"{order.side.value} {order.symbol}) is still working at the venue",
                )
            )
            return
        try:
            placed = self._client.submit_order(order.symbol, order.qty, order.side)
        except OrderRejectedError as exc:
            # No id came back, so nothing is pending and nothing needs polling --
            # the next bar is free to try the same intent again.
            self.rejections.append((order, str(exc)))
            return
        self._pending.append(placed.id)
        self._requested[placed.id] = order

    def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
        """Poll pending orders, emit fills for settled ones, then reconcile.

        Each pending order is polled until it reaches a **terminal** status or the
        poll timeout elapses. A filled order yields a :class:`~trading.types.Fill`
        at its ``filled_avg_price`` / ``filled_qty`` (no simulated slippage or
        commission -- the venue's fill is already real, ADR-0020). An order the
        venue ended without completing -- ``rejected``, and equally ``canceled`` /
        ``expired`` / ``replaced`` -- is dropped with a recorded reason, after
        emitting any partial fill it did get (ADR-0033). One still *working* at
        timeout stays pending and is retried on the next ``on_bar``. The portfolio
        is always reconciled from the account afterwards, whether or not anything
        filled, since positions can move between bars.
        """
        _ = bars  # Prices come from the venue, not our bars; kept for seam parity.
        fills: list[Fill] = []
        still_pending: list[str] = []

        for order_id in self._pending:
            settled = self._poll(order_id)
            if settled is None:
                still_pending.append(order_id)  # still working; retry next bar.
                continue
            # A terminal-but-unfilled order may still carry a partial fill, so
            # record the fill (if any) *and* the reason it ended early.
            fill = self._to_fill(settled)
            if settled.status != STATUS_FILLED:
                if fill is not None:
                    fills.append(fill)
                self.rejections.append(
                    (
                        self._requested[order_id],
                        f"order {order_id} ended {settled.status} at the venue",
                    )
                )
                del self._requested[order_id]
                continue
            if fill is None:
                still_pending.append(order_id)  # filled flag but no price yet.
                continue
            fills.append(fill)
            del self._requested[order_id]

        self._pending = still_pending
        self._portfolio = self._reconcile()
        return fills

    # -- internals --

    def _trim_rounding_oversell(self, order: Order) -> Order:
        """Trim an exit that rounding pushed a hair past the holding (ADR-0058).

        **Narrow on purpose, in three ways**, because silently rewriting an order
        is exactly the kind of helpfulness this bench refuses:

        * **SELL only.** A BUY has no holding it could exceed.
        * **Only when the excess is at most half a unit at
          :data:`~trading.sizing.SHARE_PRECISION`.** An exit for twice what is held
          is a bug upstream, and halving it would hide that — it goes to the venue,
          which refuses it, and the refusal is recorded (ADR-0041).
        * **Only against a position that exists.** Selling something unheld is not
          a rounding artifact; it is the implicit short ADR-0011 forbids, and the
          venue is the right thing to say so.

        The trim is logged rather than recorded as a rejection or a clamp: the exit
        *happens*, at the only quantity that could have worked, so calling it a
        guardrail action would misdescribe it. The guardrail clamps (ADR-0009) are
        policy decisions about how much to hold; this is arithmetic about how much
        exists.

        **The root cause is one layer up and is not fixed here.**
        ``SHARE_PRECISION = 6`` is a US-equity fractional-share convention applied
        to every market, and the honest fix is for the sizer to round *toward zero*
        (or to the venue's own ``min_trade_increment``). That lives in
        ``sizing.py``, is shared by the backtest path, and would move every equity
        figure this repo has published — so it belongs to a card that owns that
        file and can re-baseline the goldens. This guard is the symptom-level
        defence, the same shape ADR-0036 chose over KAN-678.
        """
        if order.side is not Side.SELL:
            return order
        held = self._portfolio.position(order.symbol).qty
        excess = order.qty - held
        if held <= 0.0 or not (0.0 < excess <= _MAX_ROUNDING_OVERSELL):
            return order
        logger.info(
            "trimmed %s exit from %.9f to the %.9f actually held (%.2e over, "
            "sizer rounding at %d decimals); a long-or-flat position must stay "
            "sellable (ADR-0058)",
            order.symbol,
            order.qty,
            held,
            excess,
            SHARE_PRECISION,
        )
        return replace(order, qty=held)

    def _working_order_id(self, symbol: str, side: Side) -> str | None:
        """The id of an order in ``symbol`` on ``side`` the venue is still working.

        Derived entirely from the state the broker already keeps -- the pending id
        list and the ``_requested`` map of what each id was asked to do -- so the
        guard adds no bookkeeping that could drift out of step with the venue.

        **Keyed on symbol *and* side, deliberately.** Two consequences, both
        wanted:

        * A working BUY can never block a SELL. This bench is long-or-flat
          (ADR-0011), so a SELL is the only exit there is, and an unsellable
          position is far worse than a duplicate buy -- the same asymmetry the kill
          switch already encodes by allowing exits while halted (ADR-0013/0031).
          Because the key includes the side, an exit is never even *compared*
          against a working entry.
        * A duplicate SELL is suppressed too, and that is not blocking an exit: the
          first SELL is already at the venue and will still fill. Sending a second
          would try to sell the position twice, which this bench forbids outright.

        **Working, not merely submitted.** A partially filled order that the venue
        then *ended* (``canceled`` / ``expired`` -- the routine end of a parked DAY
        order) is gone from the pending set by the time the next bar submits, so a
        follow-up order for the unfilled remainder is a fresh intent and goes
        through. One still reporting ``partially_filled`` is a working state
        (ADR-0033): the rest of that same order is live, so topping it up would
        double the remainder, and the guard suppresses it until the venue settles.
        """
        for order_id in self._pending:
            working = self._requested.get(order_id)
            if working is not None and working.symbol == symbol and working.side is side:
                return order_id
        return None

    def _poll(self, order_id: str) -> AlpacaOrder | None:
        """Poll one order until it reaches a terminal status or the timeout elapses.

        Returns the settled order, or ``None`` if it is still *working* when the
        timeout is reached. "Terminal" is the whole set Alpaca can end an order in
        (ADR-0033), not just filled/rejected: waiting out a ``canceled`` order
        would burn the full timeout on every bar for the rest of the session and
        never settle. Uses the injected clock to measure and to wait, so tests
        advance time with no real delay and an ``auto_fill`` client returns on the
        first poll without sleeping at all.
        """
        deadline = self._clock.now() + self._poll_timeout
        while True:
            current = self._client.get_order(order_id)
            if current.status in TERMINAL_STATUSES:
                return current
            if self._clock.now() >= deadline:
                return None
            self._clock.sleep_until(self._clock.now() + self._poll_interval)

    @staticmethod
    def _to_fill(order: AlpacaOrder) -> Fill | None:
        """Build a :class:`~trading.types.Fill` from a filled order, or ``None``.

        Defensive against a ``filled`` status with no usable price/quantity yet
        (which would violate :class:`~trading.types.Fill`'s positivity invariants).

        **``commission=0.0`` is true of equities and false of crypto** (ADR-0058,
        measured 2026-08-14). Alpaca's paper crypto venue charges roughly 25 bps
        and takes it **in the received asset**, not in cash and not in any field
        the order carries: four BUYs totalling ``0.000617391`` BTC produced a
        position of ``0.000615847`` (ratio ``0.99749936``), and an independent
        ``0.00016`` BUY added ``0.0001596`` (ratio ``0.99750000``). The venue
        reports ``filled_qty`` **gross**, so nothing here can compute the fee, and
        inventing a 25 bps constant from one afternoon is exactly the re-tuning
        ADR-0052 refused.

        Two consequences, both recorded rather than fixed. The account stays
        correct — the portfolio reconciles from Alpaca, never from this
        :class:`~trading.types.Fill` (ADR-0020) — but the **blotter overstates the
        received quantity by the fee**, and any fill-divergence measurement
        (ADR-0038, KAN-710) is comparing a 5 bps slippage-only model against a
        venue charging ~25 bps plus slippage. That is a five-fold gap in the
        *unsafe* direction, and the first crypto divergence number will show it.
        """
        if order.filled_avg_price is None or order.filled_qty <= 0:
            return None
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=order.filled_qty,
            price=order.filled_avg_price,
            commission=0.0,  # see the docstring: true for equities, understated for crypto.
        )

    def _reconcile(self) -> Portfolio:
        """Rebuild the portfolio from the account and open positions.

        The Alpaca account is authoritative (ADR-0020): cash comes straight from
        :meth:`~trading.data.alpaca_client.AlpacaClient.get_account` and positions
        from :meth:`~trading.data.alpaca_client.AlpacaClient.list_positions`, never
        from a local simulation.
        """
        account = self._client.get_account()
        positions = {
            snapshot.symbol: Position(
                symbol=snapshot.symbol, qty=snapshot.qty, avg_price=snapshot.avg_price
            )
            for snapshot in self._client.list_positions()
        }
        return Portfolio(cash=account.cash, positions=positions)
