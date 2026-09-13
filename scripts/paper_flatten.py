#!/usr/bin/env python3
"""Liquidate the paper account after a session (KAN-829, EPIC-78).

Automates the manual procedure in ``docs/monday-divergence-run.md``'s "After
the run": **a live session ends holding its book**, plus any orders that were
still working when it stopped (ADR-0052 observed exactly this: 10 positions
held, plus two BUY orders that would have rebuilt them at the next open). Left
alone, an unattended account drifts further from flat every session. This
script:

1. Cancels every working **BUY** first -- selling a position while a BUY for
   it is still working leaves the BUY free to fill later and silently reopen
   the position just closed.
2. Submits a market **SELL** for the exact held quantity of every open
   position.
3. Treats a venue refusal naming an already-working order for the same
   symbol as *success, not failure* -- that is ADR-0036's duplicate-order
   guard doing its job (the position is already being flattened by the order
   already at the venue), classified via the same
   :class:`~trading.data.alpaca_client.OrderRejectedError` the broker itself
   uses (ADR-0041), never by re-deriving the venue's refusal text ourselves.
4. Since the market may be closed, a submitted SELL can **park**
   (``status="accepted"``, not ``"filled"``) rather than resolve immediately
   (ADR-0036) -- reported as success either way, distinguished in the output.

Idempotent and read-only-safe when already flat: with zero positions and zero
working orders, this performs **no write calls at all** and reports
"already flat" immediately.

Run it with ``make paper-flatten``. Do this *before* the next session, not
after, so a run starts from a known state (see "After the run" in
``docs/monday-divergence-run.md``).

Why the working-orders list is a raw SDK call, not a seam widening: see
``scripts/paper_preflight.py``'s own docstring for the full reasoning -- the
:class:`~trading.data.alpaca_client.AlpacaClient` seam has no ``list_orders``
call, and adding one is an ADR-0017 decision nobody needs for this. Confined
to ``scripts/``, matching the same precedent ``paper_preflight.py`` and
``crypto_fee_reconcile.py`` already set. Everything the *actions* below take
(list positions, submit an order, cancel an order) goes through the seam,
reusing exactly the tested calls :class:`~trading.brokers.alpaca.AlpacaBroker`
itself relies on -- nothing here is a second, untested way to talk to Alpaca.

Exit code is 0 when the account ends the pass either already flat or with
every flattening action accounted for (no genuine failures); 1 if any action
failed for a reason other than the parked-order case above, or if the working
orders / credentials could not be read at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from trading.data.alpaca_client import AlpacaClient, OrderRejectedError
from trading.types import Side

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"

KEY_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")

# Substrings the venue has actually been observed to use (ADR-0036, ADR-0041)
# when refusing an order because another order for the same symbol is already
# working: a parked BUY makes a SELL a "potential wash trade", and a working
# SELL makes a second one "insufficient qty available ... held_for_orders".
# This is deliberately a *fallback*, not the primary signal -- the primary
# signal is the already-fetched ``working_orders`` list below, checked before
# a duplicate submit is ever attempted. This only catches the race where an
# order starts working between that fetch and the submit call.
_ALREADY_WORKING_MARKERS = ("held_for_orders", "wash trade")


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    """One order the venue reports as still working, in our own terms.

    Deliberately independent of *how* it was fetched: the real run reads this
    off the raw SDK (:func:`_fetch_working_orders`, mirroring
    ``paper_preflight.py``), while a test builds these directly, so
    :func:`plan_and_flatten` is exercisable against a plain
    :class:`~trading.data.alpaca_client.FakeAlpacaClient` -- which has no
    "list every order" call at all -- with no seam change.
    """

    id: str
    symbol: str
    side: Side
    qty: float


@dataclass(frozen=True, slots=True)
class Action:
    """One thing this pass did, or decided not to do, for the final report."""

    status: str
    description: str


@dataclass(frozen=True, slots=True)
class FlattenReport:
    """The result of one flatten pass: what was found, and what was done."""

    already_flat: bool
    positions_found: int
    working_buys_canceled: int
    sells_submitted: int
    already_flattening: int
    actions: tuple[Action, ...]

    @property
    def failed_actions(self) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if a.status == FAIL)


def plan_and_flatten(client: AlpacaClient, working_orders: list[WorkingOrder]) -> FlattenReport:
    """Cancel every working BUY, then SELL every open position (see module docstring).

    ``client`` is anything satisfying the :class:`AlpacaClient` seam -- a real
    run passes :class:`~trading.data.alpaca_client.RealAlpacaClient`, a test
    passes :class:`~trading.data.alpaca_client.FakeAlpacaClient`.
    ``working_orders`` is supplied by the caller rather than fetched in here,
    which is what keeps this function's decision logic (given what is known,
    what to do) separate from *how* a working order was discovered.
    """
    positions = client.list_positions()
    if not positions and not working_orders:
        return FlattenReport(
            already_flat=True,
            positions_found=0,
            working_buys_canceled=0,
            sells_submitted=0,
            already_flattening=0,
            actions=(Action(INFO, "account already flat: 0 positions, 0 working orders"),),
        )

    actions: list[Action] = []
    working_buys_canceled = 0
    sells_submitted = 0
    already_flattening = 0

    working_buys = [order for order in working_orders if order.side is Side.BUY]
    working_sells_by_symbol = {
        order.symbol: order for order in working_orders if order.side is Side.SELL
    }

    # Step 1: cancel every working BUY first (ADR-0036) -- a BUY that fills
    # after we sell would silently reopen the position we just closed.
    for order in working_buys:
        try:
            client.cancel_order(order.id)
        except Exception as exc:
            actions.append(
                Action(
                    FAIL,
                    f"{order.symbol}: could not cancel working BUY {order.qty:g} "
                    f"(id={order.id}): {exc!r}",
                )
            )
            continue
        working_buys_canceled += 1
        actions.append(
            Action(PASS, f"{order.symbol}: canceled working BUY {order.qty:g} (id={order.id})")
        )

    # Step 2: SELL the exact held quantity of every open position.
    for position in positions:
        qty = position.qty
        if qty <= 0:
            # No implicit shorting (ADR-0011): a non-positive held qty is not
            # a position this pass can flatten by selling.
            actions.append(
                Action(
                    FAIL,
                    f"{position.symbol}: held qty {qty:g} is not positive; "
                    "cannot flatten with a SELL",
                )
            )
            continue

        existing_sell = working_sells_by_symbol.get(position.symbol)
        if existing_sell is not None:
            # ADR-0036's parked-order case, recognized structurally from the
            # working-orders list -- no submit call is even attempted.
            already_flattening += 1
            actions.append(
                Action(
                    INFO,
                    f"{position.symbol}: already flattening (SELL {existing_sell.qty:g} "
                    f"already working, id={existing_sell.id})",
                )
            )
            continue

        try:
            order = client.submit_order(position.symbol, qty, Side.SELL)
        except OrderRejectedError as exc:
            if _looks_like_already_working(exc):
                already_flattening += 1
                actions.append(
                    Action(
                        INFO,
                        f"{position.symbol}: already flattening (venue refused the "
                        f"duplicate SELL {qty:g}, ADR-0036: {exc})",
                    )
                )
            else:
                actions.append(Action(FAIL, f"{position.symbol}: SELL {qty:g} refused: {exc}"))
            continue
        except Exception as exc:
            actions.append(Action(FAIL, f"{position.symbol}: SELL {qty:g} failed: {exc!r}"))
            continue

        sells_submitted += 1
        if order.status == "filled":
            actions.append(
                Action(
                    PASS,
                    f"{position.symbol}: SELL {qty:g} filled @ {order.filled_avg_price}",
                )
            )
        else:
            actions.append(
                Action(
                    PASS,
                    f"{position.symbol}: SELL {qty:g} submitted, parked "
                    f"(status={order.status!r}, id={order.id}) -- will resolve at the "
                    "next open (ADR-0036)",
                )
            )

    return FlattenReport(
        already_flat=False,
        positions_found=len(positions),
        working_buys_canceled=working_buys_canceled,
        sells_submitted=sells_submitted,
        already_flattening=already_flattening,
        actions=tuple(actions),
    )


def _looks_like_already_working(exc: OrderRejectedError) -> bool:
    """Whether a venue refusal names an already-working order for this symbol.

    A fallback for the race between fetching working orders and submitting
    (see the module-level comment on :data:`_ALREADY_WORKING_MARKERS`) -- the
    primary detection is structural, off the working-orders list, in
    :func:`plan_and_flatten` itself.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _ALREADY_WORKING_MARKERS)


def _fetch_working_orders() -> list[WorkingOrder]:
    """Working orders at the venue, read straight from the SDK; see the module docstring."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    client = TradingClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )
    orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    return [
        WorkingOrder(
            id=str(order.id),
            symbol=str(order.symbol),
            side=Side.BUY if "buy" in str(order.side).lower() else Side.SELL,
            qty=float(order.qty),
        )
        for order in orders
    ]


def _print_report(report: FlattenReport) -> None:
    width = max((len(action.status) for action in report.actions), default=4)
    for action in report.actions:
        print(f"[{action.status.ljust(width)}] {action.description}")
    print()
    if report.already_flat:
        print("Already flat -- nothing to do.")
        return
    print(
        f"Positions found: {report.positions_found}  "
        f"Working BUYs canceled: {report.working_buys_canceled}  "
        f"SELLs submitted: {report.sells_submitted}  "
        f"Already flattening: {report.already_flattening}  "
        f"Failures: {len(report.failed_actions)}"
    )


def _verify(client: AlpacaClient) -> bool:
    """Read back current positions and working orders; ``True`` iff flat."""
    positions = client.list_positions()
    if positions:
        held = ", ".join(f"{p.symbol} {p.qty:g}" for p in positions)
        print(f"[{INFO}] positions now: {len(positions)} open -- {held}")
    else:
        print(f"[{PASS}] positions now: 0 open (flat)")

    try:
        working = _fetch_working_orders()
    except Exception as exc:
        print(f"[{INFO}] could not re-read working orders: {exc!r}")
        return not positions

    if working:
        listed = ", ".join(f"{o.symbol} {o.side.value} {o.qty:g} (id={o.id})" for o in working)
        print(f"[{INFO}] working orders now: {len(working)} -- {listed}")
    else:
        print(f"[{PASS}] working orders now: 0")
    return not positions and not working


def main() -> int:
    print("Paper flatten -- liquidate the account (KAN-829, docs/monday-divergence-run.md)")
    print()

    try:
        import alpaca  # noqa: F401
    except ImportError as exc:
        print(f"[{FAIL}] alpaca extra not installed: {exc} -- install with: uv sync --extra alpaca")
        return 1

    missing = [name for name in KEY_VARS if not os.environ.get(name)]
    if missing:
        print(
            f"[{FAIL}] credentials: {', '.join(missing)} not set -- the code reads "
            "os.environ directly; use `uv run --env-file .env`"
        )
        return 1

    from trading.data.alpaca_client import RealAlpacaClient

    client = RealAlpacaClient()
    try:
        working_orders = _fetch_working_orders()
    except Exception as exc:
        print(f"[{FAIL}] could not list working orders: {exc!r}")
        return 1

    report = plan_and_flatten(client, working_orders)
    _print_report(report)

    print()
    print("Verifying...")
    is_flat = _verify(client)

    print()
    if report.failed_actions:
        print(f"NOT CLEAN: {len(report.failed_actions)} action(s) failed -- see above.")
        return 1
    if report.already_flat or is_flat:
        print("Clean: account is flat.")
    else:
        print(
            "Flattening in progress: SELLs submitted or already working will resolve "
            "at the next open (ADR-0036). Re-check with `make paper-preflight` once "
            "the market has opened."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
