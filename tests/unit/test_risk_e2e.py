"""End-to-end acceptance for enforced guardrails (SLICES V3, ADR-0009/0013).

Fast, no infrastructure (FakeAdapter): the guardrails ride the real engine order
path. Two acceptance criteria from SLICES V3:

* a strategy asking for 200% of equity is *clamped* so final gross ≈ 100%, and
* a scripted drawdown past the threshold *halts new entries* while an existing
  position can still be exited.

The helper strategies live here (per the slice brief) rather than in
``src/trading/strategies`` — they exist only to drive these guards.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.engine import Engine
from trading.interfaces import StrategyContext
from trading.risk import Guardrails
from trading.types import Bar, Order, Portfolio, Side, TargetWeight

_ZERO_COST = CostConfig(commission_per_share=0.0, slippage_bps=0.0)


def _bar(symbol: str, day: int, o: float, c: float) -> Bar:
    return Bar(symbol, datetime(2024, 1, day, tzinfo=UTC), o, max(o, c), min(o, c), c, 1_000)


class _OverleverOnce:
    """On the first bar, ask (via a raw order) to hold ~`target` times equity in one symbol."""

    def __init__(self, symbol: str, target: float) -> None:
        self._symbol = symbol
        self._target = target
        self._done = False

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._done or self._symbol not in bars:
            return []
        self._done = True
        price = bars[self._symbol].close
        # Flat on bar 1, so equity ≈ cash; ask for `target` times it in shares.
        qty = self._target * context.portfolio.cash / price
        return [Order(self._symbol, Side.BUY, qty)]


class _EqualWeightOnce:
    """On the first bar, target ``weight`` of equity in each of ``symbols``."""

    def __init__(self, symbols: list[str], weight: float) -> None:
        self._symbols = symbols
        self._weight = weight
        self._done = False

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._done or not all(s in bars for s in self._symbols):
            return []
        self._done = True
        return [TargetWeight(s, self._weight) for s in self._symbols]


class _ScriptedCrash:
    """Invest half on bar 1, try to add more after the crash, then exit."""

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol
        self._bar = 0

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._symbol not in bars:
            return []
        self._bar += 1
        if self._bar == 1:
            return [TargetWeight(self._symbol, 0.5)]  # deploy 50%, keep 50% cash
        if self._bar == 4:
            # Post-crash: attempt to increase exposure — should be halt-blocked
            # even though there is cash on hand to fund it.
            return [Order(self._symbol, Side.BUY, 1.0)]
        if self._bar == 5:
            held = context.portfolio.position(self._symbol).qty
            if held > 0:
                return [Order(self._symbol, Side.SELL, held)]  # exit — must be allowed
        return []


def test_two_hundred_percent_target_is_clamped_to_the_gross_cap() -> None:
    # Flat $1,000 book, price a constant $100. The strategy asks for 200% (20
    # shares); the 100% caps trim it to 10 shares so final gross ≈ 100%, not 200%.
    bars = [_bar("AAA", d, o=100, c=100) for d in (1, 2, 3)]
    broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
    guardrails = Guardrails(RiskConfig(max_position_pct=1.0, max_gross_exposure=1.0))
    result = Engine(FakeAdapter(bars), broker, guardrails).run(
        _OverleverOnce("AAA", target=2.0),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )

    assert result.clamps, "expected the over-cap buy to be clamped"
    original, clamped, reason = result.clamps[0]
    assert original.qty == pytest.approx(20.0)
    assert clamped.qty == pytest.approx(10.0)
    assert "cap" in reason

    final_gross = result.final_portfolio.gross_exposure({"AAA": 100.0})
    assert final_gross == pytest.approx(1.0)  # ≈100%, decisively not 200%


def test_multi_symbol_rebalance_respects_gross_cap_across_orders() -> None:
    # Two symbols targeted 50% + 50% (100% gross intended) under an 80% gross cap.
    # The orders queue together, so the cap must account for exposure the sibling
    # order committed this bar; realized gross must land at ≤ 80%, not sail to 100%.
    bars = [_bar(s, d, o=100, c=100) for s in ("AAA", "BBB") for d in (1, 2, 3)]
    broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
    guardrails = Guardrails(
        RiskConfig(max_gross_exposure=0.8, max_position_pct=1.0, max_drawdown_pct=1.0)
    )
    result = Engine(FakeAdapter(bars), broker, guardrails).run(
        _EqualWeightOnce(["AAA", "BBB"], weight=0.5),
        ["AAA", "BBB"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )

    assert result.clamps, "expected the shared gross cap to clamp a sibling order"
    final_gross = result.final_portfolio.gross_exposure({"AAA": 100.0, "BBB": 100.0})
    assert final_gross <= 0.8 + 1e-6  # cap enforced across the whole rebalance


def test_scripted_drawdown_halts_new_entries_but_allows_the_exit() -> None:
    bars = [
        _bar("AAA", 1, o=100, c=100),
        _bar("AAA", 2, o=100, c=100),  # 50% buy fills here → 5 shares, $500 cash left
        _bar("AAA", 3, o=100, c=100),  # peak equity $1,000
        _bar("AAA", 4, o=100, c=50),  # crash: equity $750 → 25% drawdown, halt
        _bar("AAA", 5, o=50, c=50),  # exit submitted here
        _bar("AAA", 6, o=50, c=50),  # exit fills here
    ]
    broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
    guardrails = Guardrails(
        RiskConfig(max_position_pct=1.0, max_gross_exposure=1.0, max_drawdown_pct=0.20)
    )
    result = Engine(FakeAdapter(bars), broker, guardrails).run(
        _ScriptedCrash("AAA"),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 6, tzinfo=UTC),
    )

    # The kill switch latched on the crash bar (day 4).
    assert result.halted is True
    assert result.halt_ts == datetime(2024, 1, 4, tzinfo=UTC)

    # The post-crash BUY was blocked by the halt (not by cash — $500 was free).
    blocked_buys = [
        (order, reason)
        for order, reason in result.rejections
        if order.side is Side.BUY and "drawdown" in reason
    ]
    assert blocked_buys, f"expected a halt-blocked buy, got {result.rejections}"

    # The exit still went through: the SELL filled and the position is flat.
    assert any(fill.side is Side.SELL for _, fill in result.fills), "exit should be allowed"
    assert result.final_portfolio.position("AAA").qty == pytest.approx(0.0)
