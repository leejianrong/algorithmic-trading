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

from datetime import timedelta

from trading.clock import Clock, WallClock
from trading.data.alpaca_client import (
    STATUS_FILLED,
    TERMINAL_STATUSES,
    AlpacaClient,
    AlpacaOrder,
    RealAlpacaClient,
)
from trading.types import Bar, Fill, Order, Portfolio, Position

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
    :attr:`rejections` holds ``(Order, reason)`` exactly as
    :class:`~trading.broker.SimulatedBroker` does, because
    :class:`~trading.engine.Engine` merges both into the same
    ``BacktestResult.rejections`` (ADR-0035).
    """

    def __init__(
        self,
        client: AlpacaClient | None = None,
        *,
        clock: Clock | None = None,
        poll_timeout: timedelta = DEFAULT_POLL_TIMEOUT,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
    ) -> None:
        # Default to a live client / wall clock only when nothing is injected, so
        # the fast test layer never touches the network or a real clock.
        self._client: AlpacaClient = client if client is not None else RealAlpacaClient()
        self._clock: Clock = clock if clock is not None else WallClock()
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self._pending: list[str] = []
        # What each pending order id was asked to do, so a rejection can name the
        # Order rather than the id (ADR-0035): BacktestResult.rejections and
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
        """
        placed = self._client.submit_order(order.symbol, order.qty, order.side)
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
        """
        if order.filled_avg_price is None or order.filled_qty <= 0:
            return None
        return Fill(
            symbol=order.symbol,
            side=order.side,
            qty=order.filled_qty,
            price=order.filled_avg_price,
            commission=0.0,  # Alpaca paper commission is zero; real costs are in the fill.
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
