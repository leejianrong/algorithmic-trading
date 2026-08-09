#!/usr/bin/env python3
"""Read-only preflight for the overnight live paper session (ADR-0051).

The "Before you start" list in ``docs/monday-divergence-run.md``, automated. It
places no orders, cancels nothing, and changes nothing at the venue: every call
below is a GET. Run it with ``make paper-preflight``.

Two of the five checks cannot go through :class:`trading.data.alpaca_client.AlpacaClient`
because the seam has neither call: there is no ``list_orders`` and no market
clock. Widening the seam is an ADR-0017 decision and a live *run* does not need
either, so this operator script reads them from the SDK directly. That is
deliberate and confined to ``scripts/`` -- nothing in ``src/trading/`` touches an
SDK type outside the seam, which is the rule ADR-0017 actually states.

Exit code is 0 only when everything the run depends on is verified clean.
"""

import os
import sys
from dataclasses import dataclass

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
INFO = "INFO"

KEY_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")


@dataclass(frozen=True)
class Check:
    """One preflight line: a verdict, what was checked, and what was seen."""

    status: str
    name: str
    detail: str


def _sdk_installed(results: list[Check]) -> bool:
    """Is the optional ``alpaca`` extra importable? (`uv sync --extra alpaca`)"""
    try:
        import alpaca
    except ImportError as exc:
        results.append(
            Check(FAIL, "alpaca extra", f"{exc} -- install it with: uv sync --extra alpaca")
        )
        return False
    version = getattr(alpaca, "__version__", "unknown version")
    results.append(Check(PASS, "alpaca extra", f"alpaca-py {version} importable"))
    return True


def _credentials(results: list[Check]) -> bool:
    """Are both keys readable from the environment? Presence only -- never a value."""
    missing = [name for name in KEY_VARS if not os.environ.get(name)]
    if missing:
        results.append(
            Check(
                FAIL,
                "credentials",
                f"{', '.join(missing)} not set -- the code reads os.environ and does not "
                "load .env; use `uv run --env-file .env` (make paper-preflight does)",
            )
        )
        return False
    results.append(Check(PASS, "credentials", f"{' and '.join(KEY_VARS)} set (values not printed)"))
    return True


def _account_and_positions(results: list[Check]) -> None:
    """Cash, equity and open positions -- through the seam (ADR-0017)."""
    from trading.data.alpaca_client import RealAlpacaClient

    try:
        client = RealAlpacaClient()
        account = client.get_account()
        positions = client.list_positions()
    except Exception as exc:  # any failure here means "cannot confirm"
        results.append(Check(FAIL, "account", f"could not read the account: {exc!r}"))
        return

    results.append(
        Check(PASS, "account", f"cash ${account.cash:,.2f}  equity ${account.equity:,.2f}")
    )
    if positions:
        held = ", ".join(f"{p.symbol} {p.qty:g} @ {p.avg_price:,.2f}" for p in positions)
        results.append(Check(FAIL, "positions", f"{len(positions)} open -- {held}"))
    else:
        results.append(Check(PASS, "positions", "0 open (account is flat)"))


def _open_orders(results: list[Check]) -> None:
    """Working orders at the venue -- read straight from the SDK; see the module docstring."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    try:
        client = TradingClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
        )
        orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as exc:  # any failure here means "cannot confirm"
        results.append(Check(FAIL, "working orders", f"could not list orders: {exc!r}"))
        return

    if orders:
        listed = ", ".join(f"{o.symbol} {o.side} {o.qty} [{o.status}] id={o.id}" for o in orders)
        results.append(
            Check(
                FAIL,
                "working orders",
                f"{len(orders)} working -- {listed}; cancel them before starting "
                "(a parked order blocks the opposite side, ADR-0041)",
            )
        )
    else:
        results.append(Check(PASS, "working orders", "0 working"))


def _market_clock(results: list[Check]) -> None:
    """The venue's own view of whether it is open, and the next open/close."""
    from alpaca.trading.client import TradingClient

    try:
        client = TradingClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
        )
        clock = client.get_clock()
    except Exception as exc:  # informational, never a blocker
        results.append(Check(SKIP, "market clock", f"could not read the venue clock: {exc!r}"))
        return

    state = "OPEN" if clock.is_open else "closed"
    results.append(
        Check(
            INFO,
            "market clock",
            f"venue is {state} at {clock.timestamp}; "
            f"next open {clock.next_open}, next close {clock.next_close}",
        )
    )


def main() -> int:
    print("Paper preflight -- read-only (docs/monday-divergence-run.md, 'Before you start')")
    print()
    results: list[Check] = []
    ready = _sdk_installed(results)
    ready = _credentials(results) and ready
    if ready:
        _account_and_positions(results)
        _open_orders(results)
        _market_clock(results)
    else:
        results.append(
            Check(SKIP, "account", "not checked -- needs the alpaca extra and credentials")
        )
        results.append(Check(SKIP, "working orders", "not checked -- same reason"))
        results.append(Check(SKIP, "market clock", "not checked -- same reason"))

    width = max(len(check.name) for check in results)
    for check in results:
        print(f"[{check.status}] {check.name.ljust(width)}  {check.detail}")

    print()
    print(
        "Note: the AlpacaClient seam (ADR-0017) has no market-calendar call and none was\n"
        "      added for this; the clock line above is a direct read in scripts/ and is\n"
        "      informational -- it never fails the preflight."
    )
    failures = [check for check in results if check.status == FAIL]
    if failures:
        print()
        print(f"NOT CLEAN: {len(failures)} check(s) failed -- do not start the run yet.")
        return 1
    print()
    print("Clean. Two things this cannot check for you:")
    print("  - the machine must not sleep (WSL2 goes down with Windows; EPIC-86 is unbuilt)")
    print("  - launch detached, or a closed terminal kills the run: make paper-live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
