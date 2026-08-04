"""Unit tests for the enforced guardrails (ADR-0009, ADR-0013).

Fast, no infrastructure: the checker and the kill switch are exercised directly
against hand-built portfolios. The pre-trade check accepts an in-limit order,
clamps an over-cap one to the cap with the right reason, and rejects when the
clamp collapses to nothing; the drawdown monitor fires exactly at the threshold,
not just before it, and latches once tripped.
"""

from __future__ import annotations

import pytest

from trading.config import RiskConfig
from trading.risk import Guardrails
from trading.types import Order, Portfolio, Position, Side


def _portfolio(cash: float, positions: list[Position] | None = None) -> Portfolio:
    return Portfolio(cash=cash, positions={p.symbol: p for p in (positions or [])})


class TestRiskConfig:
    def test_defaults_are_the_decided_posture(self) -> None:
        cfg = RiskConfig()
        assert cfg.max_position_pct == 0.25
        assert cfg.max_gross_exposure == 1.0
        assert cfg.max_drawdown_pct == 0.20
        assert cfg.max_daily_loss_pct is None

    def test_unlimited_is_fully_permissive(self) -> None:
        cfg = RiskConfig.unlimited()
        assert cfg.max_position_pct == float("inf")
        assert cfg.max_gross_exposure == float("inf")
        assert cfg.max_drawdown_pct == 1.0
        assert cfg.max_daily_loss_pct is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_position_pct": 0.0},
            {"max_gross_exposure": -1.0},
            {"max_drawdown_pct": 0.0},
            {"max_drawdown_pct": 1.5},
            {"max_daily_loss_pct": 0.0},
            {"max_daily_loss_pct": 2.0},
        ],
    )
    def test_out_of_range_limits_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            RiskConfig(**kwargs)  # type: ignore[arg-type]


class TestPreTradeCheck:
    def test_in_limit_buy_passes_through_unchanged(self) -> None:
        # $1,000 flat; a 20% ($200 = 2 shares @ $100) buy is under the 25% cap.
        guard = Guardrails(RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0))
        order = Order("AAA", Side.BUY, 2.0)
        checked = guard.check(order, _portfolio(1_000.0), {"AAA": 100.0})
        assert checked is order
        assert guard.last_reason is None

    def test_over_cap_buy_is_clamped_to_the_position_cap(self) -> None:
        # $1,000 flat; a 60% (6 shares @ $100) buy exceeds the 25% position cap,
        # which allows 0.25 * 1000 / 100 = 2.5 shares.
        guard = Guardrails(RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0))
        order = Order("AAA", Side.BUY, 6.0)
        checked = guard.check(order, _portfolio(1_000.0), {"AAA": 100.0})
        assert checked is not None
        assert checked.qty == pytest.approx(2.5)
        assert checked.symbol == "AAA" and checked.side is Side.BUY
        assert "position cap" in (guard.last_reason or "")

    def test_over_cap_buy_is_clamped_to_the_gross_exposure_cap(self) -> None:
        # Already 90% gross in BBB (9 @ $100); gross cap 100% leaves room for only
        # 0.10 * 1000 / 100 = 1 more share of AAA, not the 5 requested.
        guard = Guardrails(RiskConfig(max_position_pct=1.0, max_gross_exposure=1.0))
        pf = _portfolio(100.0, [Position("BBB", qty=9.0, avg_price=100.0)])
        prices = {"AAA": 100.0, "BBB": 100.0}
        checked = guard.check(Order("AAA", Side.BUY, 5.0), pf, prices)
        assert checked is not None
        assert checked.qty == pytest.approx(1.0)
        assert "gross exposure cap" in (guard.last_reason or "")

    def test_buy_is_rejected_when_the_clamp_collapses_to_zero(self) -> None:
        # Already at the 25% position cap in AAA (2.5 @ $100 of $1,000 equity);
        # any further buy has no room and is rejected outright.
        guard = Guardrails(RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0))
        pf = _portfolio(750.0, [Position("AAA", qty=2.5, avg_price=100.0)])
        checked = guard.check(Order("AAA", Side.BUY, 5.0), pf, {"AAA": 100.0})
        assert checked is None
        assert "rejected" in (guard.last_reason or "")

    def test_sell_that_reduces_a_holding_is_always_allowed(self) -> None:
        guard = Guardrails(RiskConfig(max_position_pct=0.01, max_gross_exposure=0.01))
        pf = _portfolio(0.0, [Position("AAA", qty=10.0, avg_price=100.0)])
        order = Order("AAA", Side.SELL, 4.0)
        checked = guard.check(order, pf, {"AAA": 100.0})
        assert checked is order

    def test_gross_cap_accounts_for_earlier_same_bar_orders(self) -> None:
        # Same-bar multi-order rebalance: the orders queue and don't fill until the
        # next bar, so the pre-trade portfolio never changes between checks. The
        # second symbol must still be clamped by the room the first one *committed*,
        # not by the full cap. First buy 50% (under 80%), second wants 50% but only
        # 30% of gross room is left → clamped to 3 shares.
        guard = Guardrails(
            RiskConfig(max_gross_exposure=0.8, max_position_pct=1.0, max_drawdown_pct=1.0)
        )
        pf = _portfolio(1_000.0)
        prices = {"AAA": 100.0, "BBB": 100.0}
        guard.halted(pf, prices)  # begin the bar → reset the within-bar tally
        first = guard.check(Order("AAA", Side.BUY, 5.0), pf, prices)
        assert first is not None and first.qty == pytest.approx(5.0)
        second = guard.check(Order("BBB", Side.BUY, 5.0), pf, prices)
        assert second is not None
        assert second.qty == pytest.approx(3.0)
        assert "gross exposure cap" in (guard.last_reason or "")

    def test_position_cap_accounts_for_earlier_same_symbol_orders(self) -> None:
        # Two raw buys of the *same* symbol in one bar: the position cap must see
        # the first buy's committed quantity. 25% cap = 2.5 shares; first takes 2,
        # so the second (wanting 2 more) is clamped to the remaining 0.5.
        guard = Guardrails(
            RiskConfig(max_position_pct=0.25, max_gross_exposure=10.0, max_drawdown_pct=1.0)
        )
        pf = _portfolio(1_000.0)
        prices = {"AAA": 100.0}
        guard.halted(pf, prices)  # begin the bar → reset the within-bar tally
        first = guard.check(Order("AAA", Side.BUY, 2.0), pf, prices)
        assert first is not None and first.qty == pytest.approx(2.0)
        second = guard.check(Order("AAA", Side.BUY, 2.0), pf, prices)
        assert second is not None
        assert second.qty == pytest.approx(0.5)
        assert "position cap" in (guard.last_reason or "")


class TestKillSwitch:
    def _monitor(self, threshold: float = 0.20) -> Guardrails:
        return Guardrails(RiskConfig(max_drawdown_pct=threshold))

    def test_fires_exactly_at_the_drawdown_threshold(self) -> None:
        guard = self._monitor(0.20)
        # Peak equity 1,000 on bar 1.
        assert guard.halted(_portfolio(1_000.0), {}) is False
        # Exactly 20% down (equity 800): fires.
        assert guard.halted(_portfolio(800.0), {}) is True

    def test_does_not_fire_just_before_the_threshold(self) -> None:
        guard = self._monitor(0.20)
        assert guard.halted(_portfolio(1_000.0), {}) is False
        # 19.99% down: still safe.
        assert guard.halted(_portfolio(800.1), {}) is False
        assert guard.is_halted is False

    def test_halt_latches_after_recovery(self) -> None:
        guard = self._monitor(0.20)
        guard.halted(_portfolio(1_000.0), {})
        assert guard.halted(_portfolio(750.0), {}) is True  # 25% down → trips
        # Equity fully recovers past the old peak, yet the halt stays latched.
        assert guard.halted(_portfolio(1_200.0), {}) is True
        assert guard.is_halted is True
        assert guard.halt_reason is not None

    def test_daily_loss_breaker_fires_on_a_single_bar_drop(self) -> None:
        # Drawdown cap high so only the daily-loss breaker can fire.
        guard = Guardrails(RiskConfig(max_drawdown_pct=1.0, max_daily_loss_pct=0.10))
        assert guard.halted(_portfolio(1_000.0), {}) is False
        # One bar down 12% > 10%: fires.
        assert guard.halted(_portfolio(880.0), {}) is True
        assert "daily loss" in (guard.halt_reason or "")

    def test_halted_order_check_blocks_new_entries_but_allows_exits(self) -> None:
        guard = self._monitor(0.20)
        pf = _portfolio(500.0, [Position("AAA", qty=5.0, avg_price=100.0)])
        prices = {"AAA": 100.0}
        guard.halted(_portfolio(1_000.0), {})
        guard.halted(_portfolio(700.0), {})  # 30% down → latch
        assert guard.is_halted is True
        # A new buy is blocked while halted...
        assert guard.check(Order("AAA", Side.BUY, 1.0), pf, prices) is None
        # ...but the position can still be exited.
        exit_order = Order("AAA", Side.SELL, 5.0)
        assert guard.check(exit_order, pf, prices) is exit_order


def test_clamp_rounding_to_zero_rejects_instead_of_crashing() -> None:
    """A clamp leaving positive room below share precision rejects, never crashes.

    Regression: a positive ``allowed`` that rounds to 0.0 at SHARE_PRECISION passed
    the ``> SHARE_EPS`` reject check and then blew up building a zero-qty Order.
    """
    from trading.config import RiskConfig
    from trading.risk import Guardrails
    from trading.types import Order, Portfolio, Position, Side

    guardrails = Guardrails(
        RiskConfig(max_position_pct=1.0, max_gross_exposure=1.0, max_drawdown_pct=1.0)
    )
    # equity 100, gross already 99.9999996 -> ~4e-7 shares of room at price 1.0:
    # above SHARE_EPS (1e-9) but rounds to 0.0 at 6 dp.
    portfolio = Portfolio(cash=0.0000004, positions={"AAA": Position("AAA", 99.9999996, 1.0)})
    prices = {"AAA": 1.0, "BBB": 1.0}
    result = guardrails.check(Order("BBB", Side.BUY, 1.0), portfolio, prices)
    assert result is None  # rejected cleanly, no exception
