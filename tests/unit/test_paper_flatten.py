"""Fast, offline tests for scripts/paper_flatten.py (KAN-829, EPIC-78).

``paper_flatten.py`` lives outside ``src/trading`` (operator tooling, ADR-0017),
so it is loaded here by file path rather than imported as a package. Every test
drives its core decision function, :func:`plan_and_flatten`, against
:class:`~trading.data.alpaca_client.FakeAlpacaClient` -- no network, no real
credentials, no real orders. Nothing here submits or cancels a real order.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from trading.data.alpaca_client import FakeAlpacaClient
from trading.types import Side


def _load_paper_flatten() -> ModuleType:
    """Load ``scripts/paper_flatten.py`` by path (it is not a package)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "paper_flatten.py"
    spec = importlib.util.spec_from_file_location("paper_flatten", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


paper_flatten = _load_paper_flatten()
Action = paper_flatten.Action
WorkingOrder = paper_flatten.WorkingOrder
plan_and_flatten = paper_flatten.plan_and_flatten


def _client_with_position(symbol: str, qty: float, price: float = 100.0) -> FakeAlpacaClient:
    """A fake client with one filled long position and nothing working."""
    client = FakeAlpacaClient(cash=100_000.0)
    client.set_price(symbol, price)
    client.submit_order(symbol, qty, Side.BUY)
    return client


class TestAlreadyFlat:
    def test_flat_account_takes_no_actions(self) -> None:
        client = FakeAlpacaClient(cash=100_000.0)

        report = plan_and_flatten(client, working_orders=[])

        assert report.already_flat is True
        assert report.positions_found == 0
        assert report.sells_submitted == 0
        assert report.working_buys_canceled == 0
        assert report.already_flattening == 0
        assert not report.failed_actions

    def test_flat_account_performs_zero_write_calls(self) -> None:
        """Idempotent / read-only-safe: nothing is submitted or cancelled when flat."""
        client = FakeAlpacaClient(cash=100_000.0)

        plan_and_flatten(client, working_orders=[])

        # No orders exist at all -- proof nothing was submitted or cancelled.
        assert client.list_positions() == []
        assert client.get_account().cash == pytest.approx(100_000.0)


class TestPlainPosition:
    def test_position_with_no_working_orders_gets_sold(self) -> None:
        client = _client_with_position("AAPL", 10.0, price=150.0)

        report = plan_and_flatten(client, working_orders=[])

        assert report.already_flat is False
        assert report.positions_found == 1
        assert report.sells_submitted == 1
        assert report.working_buys_canceled == 0
        assert report.already_flattening == 0
        assert not report.failed_actions
        assert client.list_positions() == []  # the fake auto-fills the SELL

    def test_sell_uses_the_exact_held_quantity(self) -> None:
        client = _client_with_position("AAPL", 3.14159, price=150.0)

        plan_and_flatten(client, working_orders=[])

        # Auto-fill applies the trade for real, so an exact-qty sell nets flat;
        # a wrong quantity would leave a residual position or oversell.
        assert client.list_positions() == []


class TestWorkingBuyCanceledFirst:
    def test_working_buy_is_canceled_before_the_sell(self) -> None:
        # auto_fill=False so the second BUY genuinely stays "new" (working) --
        # simulating ADR-0036's parked-order case, where a working entry must
        # not be left free to fill and reopen the position after we sell.
        client = FakeAlpacaClient(cash=100_000.0, auto_fill=False)
        client.set_price("AAPL", 150.0)
        opening = client.submit_order("AAPL", 10.0, Side.BUY)
        client.fill_order(opening.id)  # establish the position to flatten
        parked = client.submit_order("AAPL", 5.0, Side.BUY)  # left working
        assert client.get_order(parked.id).status == "new"

        working = [WorkingOrder(id=parked.id, symbol="AAPL", side=Side.BUY, qty=5.0)]
        report = plan_and_flatten(client, working_orders=working)

        assert report.working_buys_canceled == 1
        assert report.sells_submitted == 1
        assert not report.failed_actions
        assert client.get_order(parked.id).status == "canceled"
        cancel_actions = [a for a in report.actions if "canceled working BUY" in a.description]
        assert len(cancel_actions) == 1
        assert cancel_actions[0].status == paper_flatten.PASS

    def test_a_failed_cancel_is_reported_but_the_sell_still_proceeds(self) -> None:
        """Canceling one bad BUY does not stop the position from being sold."""
        client = _client_with_position("AAPL", 10.0, price=150.0)
        working = [WorkingOrder(id="does-not-exist", symbol="AAPL", side=Side.BUY, qty=5.0)]

        report = plan_and_flatten(client, working_orders=working)

        assert len(report.failed_actions) == 1
        assert "could not cancel" in report.failed_actions[0].description
        assert report.sells_submitted == 1


class TestAlreadyWorkingSell:
    def test_a_working_sell_is_recognized_and_not_resubmitted(self) -> None:
        client = _client_with_position("AAPL", 10.0, price=150.0)
        working = [WorkingOrder(id="venue-order-1", symbol="AAPL", side=Side.SELL, qty=10.0)]

        report = plan_and_flatten(client, working_orders=working)

        assert report.already_flattening == 1
        assert report.sells_submitted == 0
        assert not report.failed_actions
        # No submit was attempted at all: the position is untouched by this pass.
        assert client.list_positions()[0].qty == pytest.approx(10.0)
        already = [a for a in report.actions if "already flattening" in a.description]
        assert len(already) == 1
        assert already[0].status == paper_flatten.INFO

    def test_a_venue_refusal_for_an_already_working_sell_is_not_a_failure(self) -> None:
        """The reactive fallback: no working-order entry, but the venue refuses anyway."""
        client = _client_with_position("AAPL", 10.0, price=150.0)
        client.set_submit_refusal(
            "AAPL", "insufficient qty available ... held_for_orders", side=Side.SELL
        )

        report = plan_and_flatten(client, working_orders=[])

        assert not report.failed_actions
        assert report.already_flattening == 1
        assert report.sells_submitted == 0


class TestSellOutcomes:
    def test_a_filled_sell_and_a_parked_sell_are_both_success(self) -> None:
        filled_client = _client_with_position("AAPL", 10.0, price=150.0)
        filled_report = plan_and_flatten(filled_client, working_orders=[])
        filled_action = next(a for a in filled_report.actions if "AAPL" in a.description)
        assert filled_action.status == paper_flatten.PASS
        assert "filled" in filled_action.description

        parked_client = FakeAlpacaClient(cash=100_000.0, auto_fill=False)
        parked_client.set_price("MSFT", 300.0)
        parked_client.submit_order("MSFT", 4.0, Side.BUY)
        parked_client.fill_order("1")  # settle the opening BUY so a position exists
        parked_report = plan_and_flatten(parked_client, working_orders=[])
        parked_action = next(a for a in parked_report.actions if "MSFT" in a.description)
        assert parked_action.status == paper_flatten.PASS
        assert "parked" in parked_action.description
        assert "filled" not in parked_action.description

        # Both are reported as success, but distinguishably so.
        assert filled_action.description != parked_action.description


class TestGenuineFailure:
    def test_an_unrelated_refusal_is_reported_as_a_failure(self) -> None:
        client = _client_with_position("AAPL", 10.0, price=150.0)
        client.set_submit_refusal("AAPL", "insufficient buying power", side=Side.SELL)

        report = plan_and_flatten(client, working_orders=[])

        assert len(report.failed_actions) == 1
        assert report.already_flattening == 0
        assert report.sells_submitted == 0
        assert "refused" in report.failed_actions[0].description

    def test_a_transport_failure_is_reported_as_a_failure_not_swallowed(self) -> None:
        client = _client_with_position("AAPL", 10.0, price=150.0)
        client.set_submit_failure("AAPL", RuntimeError("connection reset"), side=Side.SELL)

        report = plan_and_flatten(client, working_orders=[])

        assert len(report.failed_actions) == 1
        assert "connection reset" in report.failed_actions[0].description


class TestMultiplePositions:
    def test_a_mixed_book_reports_each_position_on_its_own_merits(self) -> None:
        client = FakeAlpacaClient(cash=1_000_000.0)
        client.set_price("AAPL", 150.0)
        client.set_price("MSFT", 300.0)
        client.set_price("GOOG", 140.0)
        client.submit_order("AAPL", 10.0, Side.BUY)
        client.submit_order("MSFT", 5.0, Side.BUY)
        client.submit_order("GOOG", 20.0, Side.BUY)

        working = [WorkingOrder(id="w1", symbol="MSFT", side=Side.SELL, qty=5.0)]
        report = plan_and_flatten(client, working_orders=working)

        assert report.positions_found == 3
        assert report.already_flattening == 1  # MSFT
        assert report.sells_submitted == 2  # AAPL, GOOG
        assert not report.failed_actions
        remaining = {p.symbol: p.qty for p in client.list_positions()}
        assert remaining == {"MSFT": pytest.approx(5.0)}  # AAPL/GOOG sold, MSFT untouched


class TestFakeSatisfiesTheSeam:
    def test_fake_alpaca_client_satisfies_the_protocol_used_by_plan_and_flatten(self) -> None:
        from trading.data.alpaca_client import AlpacaClient

        client = FakeAlpacaClient(cash=100_000.0)
        assert isinstance(client, AlpacaClient)
