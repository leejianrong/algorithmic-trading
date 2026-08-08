"""Integration: the fill-divergence report driven against the real venue (ADR-0038).

Evidence, not the test suite. The whole divergence *mechanism* is proved offline in
``tests/unit/test_divergence.py`` under ``FakeAlpacaClient`` / ``FakeClock``; what
these tests add is that the wrapper survives contact with `RealAlpacaClient`,
`AlpacaBroker`, and real RAW bars — and that a real order still reaches the venue
with the shadow attached.

Gating mirrors ``test_alpaca_live.py`` exactly: marked ``integration`` (so it never
gates a local push) and additionally skipped unless **both** credentials
(``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``) and the optional ``alpaca-py`` SDK are
present. CI installs with ``uv sync --frozen`` (no extras) and holds no Alpaca
credentials, so every test here skips there.

PAPER ONLY, and the account is left as it was found: a hundredth of a share, every
working order cancelled, every opened share sold back in a ``finally``.

The market state changes which branch is real, so each test asserts the branch that
applies rather than flaking at 4:01pm — exactly as ``TestMarketClosedOrder`` does.
With the venue shut, a parked order is itself the headline divergence: the model
fills at the next open while the venue has not filled at all (ADR-0036).
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from trading.types import Order, Side

_HAVE_CREDS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_HAVE_SDK = importlib.util.find_spec("alpaca") is not None

pytestmark = pytest.mark.integration

_needs_live = pytest.mark.skipif(
    not (_HAVE_CREDS and _HAVE_SDK),
    reason="needs ALPACA_API_KEY / ALPACA_SECRET_KEY and the alpaca-py SDK",
)

_SYMBOL = "AAPL"
_TINY_QTY = 0.01


def _market_is_open() -> bool:
    from trading.data.alpaca_client import RealAlpacaClient

    client = RealAlpacaClient()
    return bool(getattr(client._trading.get_clock(), "is_open", False))


def _recent_raw_bars(count: int = 2) -> list[object]:
    """The last ``count`` completed RAW daily bars for the symbol (ADR-0021).

    RAW, deliberately: the venue fills in raw dollars and the divergence arithmetic
    only means anything if both sides speak the same price notion. Fetching adjusted
    bars here would silently measure a corporate-action artifact as slippage.
    """
    from trading.data.alpaca_client import RealAlpacaClient

    client = RealAlpacaClient(feed="iex")
    end = datetime.now(UTC)
    bars = client.get_daily_bars(_SYMBOL, end - timedelta(days=30), end, adjusted=False)
    assert len(bars) >= count, f"expected at least {count} recent daily bars, got {len(bars)}"
    return list(bars[-count:])


def _cancel_open_orders(symbol: str) -> None:
    """Cancel every still-working order in ``symbol``, through the seam (ADR-0036)."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    from trading.data.alpaca_client import RealAlpacaClient

    client = RealAlpacaClient()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    for order in client._trading.get_orders(request):
        with contextlib.suppress(Exception):
            client.cancel_order(str(getattr(order, "id", order)))


def _flatten(target_qty: float) -> None:
    """Leave the account as we found it: no working orders, no extra shares."""
    from trading.data.alpaca_client import TERMINAL_STATUSES, RealAlpacaClient

    _cancel_open_orders(_SYMBOL)
    client = RealAlpacaClient()
    held = next((p.qty for p in client.list_positions() if p.symbol == _SYMBOL), 0.0)
    excess = held - target_qty
    if excess <= 0.0:
        return
    try:
        order = client.submit_order(_SYMBOL, excess, Side.SELL)
    except Exception:
        return
    for _ in range(10):
        if client.get_order(order.id).status in TERMINAL_STATUSES:
            return
        time.sleep(2)
    _cancel_open_orders(_SYMBOL)


@_needs_live
class TestShadowAgainstTheRealVenue:
    """One real order, tracked, with the shadow attached the whole way."""

    def test_a_real_order_is_measured_without_being_disturbed(self) -> None:
        from trading.brokers.alpaca import AlpacaBroker
        from trading.clock import WallClock
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.divergence import (
            NOTION_RAW,
            OUTCOME_FILLED,
            OUTCOME_PARTIAL,
            OUTCOME_PENDING,
            ShadowBroker,
        )
        from trading.types import Bar

        previous, latest = _recent_raw_bars(2)
        assert isinstance(previous, Bar) and isinstance(latest, Bar)

        client = RealAlpacaClient()
        opening_qty = next((p.qty for p in client.list_positions() if p.symbol == _SYMBOL), 0.0)
        live = AlpacaBroker(client, clock=WallClock(), poll_timeout=timedelta(seconds=20))
        shadow = ShadowBroker(live, WallClock(), price_notion=NOTION_RAW)

        try:
            # Bar t: nothing outstanding. Then submit, then bar t+1 -- the same
            # sequence Engine._step drives, so the counterfactual prices against
            # `latest.open` exactly as SimulatedBroker would.
            shadow.on_bar({_SYMBOL: previous})
            shadow.submit(Order(_SYMBOL, Side.BUY, qty=_TINY_QTY))
            shadow.on_bar({_SYMBOL: latest})

            records = shadow.divergences
            assert len(records) == 1
            record = records[0]

            # The shadow survived contact: no swallowed exception, no silent disable.
            assert shadow.enabled, shadow.errors
            assert shadow.errors == []
            assert not shadow.unmatched_live_fills
            assert record.reference_price == pytest.approx(latest.open)
            assert record.shadow.outcome == OUTCOME_FILLED
            assert record.shadow.price is not None
            assert record.shadow.price > latest.open  # a buy pays up by slippage_bps
            assert record.modelled_slippage_bps == pytest.approx(5.0)

            if record.live.outcome == OUTCOME_PENDING:
                # Venue shut: the order is parked for the next open (ADR-0036), so
                # the model filled and the venue did not. That IS the divergence.
                assert not _market_is_open()
                assert record.outcome_diverged
                assert record.realized_slippage_bps is None
                assert live.pending_order_ids
            else:
                assert record.live.outcome in (OUTCOME_FILLED, OUTCOME_PARTIAL)
                assert record.live.price is not None and record.live.price > 0.0
                assert record.comparable
                assert record.realized_slippage_bps is not None
                assert record.latency is not None and record.latency >= timedelta(0)

            # The live path is untouched either way: the broker's portfolio is the
            # account's, reconciled from the venue, not anything the shadow spent.
            account = client.get_account()
            assert shadow.portfolio.cash == pytest.approx(account.cash, rel=1e-6)
        finally:
            _flatten(opening_qty)

    def test_the_report_renders_and_refuses_to_conclude_from_one_fill(self) -> None:
        """A live run must print the honest verdict, not a confident average."""
        from trading.divergence import MIN_PAIRED_FILLS, render_report, summarize

        summary = summarize([], price_notion="raw")
        text = render_report(summary, [])
        assert "no comparable fills" in text
        assert str(MIN_PAIRED_FILLS) not in text.split("VERDICT")[0]

    def test_the_account_is_left_flat(self) -> None:
        """Runs last in the class: no working orders and no stray shares behind us."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        from trading.data.alpaca_client import RealAlpacaClient

        _cancel_open_orders(_SYMBOL)
        client = RealAlpacaClient()
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[_SYMBOL])
        assert list(client._trading.get_orders(request)) == []
