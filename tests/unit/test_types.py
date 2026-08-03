"""Fast, no-infra unit tests for the core value types (dev-playbook layer 1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.types import (
    Bar,
    Fill,
    Order,
    OrderType,
    Portfolio,
    Position,
    Side,
    TargetWeight,
)


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


class TestBar:
    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Bar("AAPL", datetime(2024, 1, 1), 1.0, 1.0, 1.0, 1.0, 100)

    def test_rejects_high_below_low(self) -> None:
        with pytest.raises(ValueError, match="high"):
            Bar("AAPL", _ts(1), open=10, high=9, low=11, close=10, volume=100)

    def test_rejects_negative_volume(self) -> None:
        with pytest.raises(ValueError, match="volume"):
            Bar("AAPL", _ts(1), 10, 11, 9, 10, volume=-1)


class TestOrder:
    def test_rejects_non_positive_qty(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            Order("AAPL", Side.BUY, qty=0)

    def test_limit_order_requires_price(self) -> None:
        with pytest.raises(ValueError, match="limit_price"):
            Order("AAPL", Side.BUY, qty=1, type=OrderType.LIMIT)

    def test_market_order_forbids_price(self) -> None:
        with pytest.raises(ValueError, match="must not carry"):
            Order("AAPL", Side.BUY, qty=1, type=OrderType.MARKET, limit_price=5.0)


class TestTargetWeight:
    @pytest.mark.parametrize("weight", [-1.5, 1.01, 2.0])
    def test_rejects_out_of_range(self, weight: float) -> None:
        with pytest.raises(ValueError, match=r"\[-1, 1\]"):
            TargetWeight("AAPL", weight)


class TestPortfolioAccounting:
    def test_equity_marks_positions_at_prices(self) -> None:
        pf = Portfolio(cash=1_000.0, positions={"AAPL": Position("AAPL", qty=10, avg_price=50.0)})
        assert pf.equity({"AAPL": 60.0}) == pytest.approx(1_000.0 + 600.0)

    def test_equity_errors_on_missing_price(self) -> None:
        pf = Portfolio(cash=0.0, positions={"AAPL": Position("AAPL", qty=1, avg_price=1.0)})
        with pytest.raises(KeyError, match="AAPL"):
            pf.equity({})

    def test_buy_then_partial_sell_tracks_cash_and_avg_price(self) -> None:
        pf = Portfolio(cash=1_000.0)
        pf.apply_fill(Fill("AAPL", Side.BUY, qty=10, price=50.0, commission=1.0))
        assert pf.cash == pytest.approx(1_000.0 - 500.0 - 1.0)
        assert pf.position("AAPL").qty == 10
        assert pf.position("AAPL").avg_price == pytest.approx(50.0)

        pf.apply_fill(Fill("AAPL", Side.SELL, qty=4, price=60.0, commission=1.0))
        assert pf.cash == pytest.approx(499.0 + 240.0 - 1.0)
        assert pf.position("AAPL").qty == 6
        # A partial sell leaves the average entry price unchanged.
        assert pf.position("AAPL").avg_price == pytest.approx(50.0)

    def test_selling_to_flat_drops_the_position(self) -> None:
        pf = Portfolio(cash=0.0, positions={"AAPL": Position("AAPL", qty=5, avg_price=10.0)})
        pf.apply_fill(Fill("AAPL", Side.SELL, qty=5, price=12.0))
        assert "AAPL" not in pf.positions
        assert pf.cash == pytest.approx(60.0)

    def test_overselling_is_rejected(self) -> None:
        pf = Portfolio(cash=0.0, positions={"AAPL": Position("AAPL", qty=2, avg_price=10.0)})
        with pytest.raises(ValueError, match="implicit shorting"):
            pf.apply_fill(Fill("AAPL", Side.SELL, qty=3, price=10.0))

    def test_averaging_up_blends_entry_price(self) -> None:
        pf = Portfolio(cash=10_000.0)
        pf.apply_fill(Fill("AAPL", Side.BUY, qty=10, price=100.0))
        pf.apply_fill(Fill("AAPL", Side.BUY, qty=10, price=120.0))
        assert pf.position("AAPL").qty == 20
        assert pf.position("AAPL").avg_price == pytest.approx(110.0)

    def test_gross_exposure_is_value_over_equity(self) -> None:
        pf = Portfolio(cash=500.0, positions={"AAPL": Position("AAPL", qty=10, avg_price=50.0)})
        # equity = 500 + 500 = 1000; gross = 500 → 0.5
        assert pf.gross_exposure({"AAPL": 50.0}) == pytest.approx(0.5)
