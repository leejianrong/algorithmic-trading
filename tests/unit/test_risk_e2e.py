"""End-to-end acceptance for enforced guardrails (SLICES V3, ADR-0009/0013).

Fast, no infrastructure (FakeAdapter): the guardrails ride the real engine order
path. Two acceptance criteria from SLICES V3:

* a strategy asking for 200% of equity is *clamped* so final gross ≈ 100%, and
* a scripted drawdown past the threshold *halts new entries* while an existing
  position can still be exited.

Plus, from ADR-0031, the halt-recovery acceptance: a scripted crash-then-recover
series must resume entries once the switch re-arms, the default config must still
latch for the whole run, and the run's :class:`HaltEpisode` list must show each
halt stretch with sane timestamps.

The helper strategies live here (per the slice brief) rather than in
``src/trading/strategies`` — they exist only to drive these guards.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.engine import BacktestResult, Engine
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


class _AlwaysTarget:
    """Target ``weight`` of equity in ``symbol`` on *every* bar.

    Sizing drops dust deltas, so at the target this emits nothing; after a crash has
    pushed the holding below the target it asks to top back up — which is exactly
    the order a halt must block and a re-armed switch must let through.
    """

    def __init__(self, symbol: str, weight: float) -> None:
        self._symbol = symbol
        self._weight = weight

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._symbol not in bars:
            return []
        return [TargetWeight(self._symbol, self._weight)]


# A crash-then-recover series: 50% invested at $100, a halving on day 4 (equity
# 1,000 -> 750, a 25% drawdown), a flat day, then a recovery back to $100.
_CRASH_THEN_RECOVER = [
    _bar("AAA", 1, o=100, c=100),
    _bar("AAA", 2, o=100, c=100),  # the 50% buy fills here -> 5 shares, $500 cash
    _bar("AAA", 3, o=100, c=100),  # peak equity $1,000
    _bar("AAA", 4, o=100, c=50),  # crash: equity $750 -> 25% drawdown, halt
    _bar("AAA", 5, o=50, c=50),  # still underwater
    _bar("AAA", 6, o=50, c=50),  # recovery config re-arms here
    _bar("AAA", 7, o=50, c=50),  # the re-entry fills here
    _bar("AAA", 8, o=50, c=50),
]


def _run_crash_then_recover(risk: RiskConfig, bars: list[Bar] | None = None) -> BacktestResult:
    """Run ``_AlwaysTarget`` over a scripted crash series under ``risk``."""
    series = bars if bars is not None else _CRASH_THEN_RECOVER
    broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
    return Engine(FakeAdapter(series), broker, Guardrails(risk)).run(
        _AlwaysTarget("AAA", 0.5),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, len(series), tzinfo=UTC),
    )


def _buy_fill_days(result: BacktestResult) -> list[int]:
    return [ts.day for ts, fill in result.fills if fill.side is Side.BUY]


def test_default_config_never_re_enters_after_the_halt() -> None:
    """The measured status quo (ADR-0031 motivation): one halt disables the run.

    This is the backward-compatibility half of the pair below — with no recovery
    configured the switch latches, every post-crash top-up is rejected for the rest
    of the series, and the single episode never closes.
    """
    result = _run_crash_then_recover(
        RiskConfig(max_position_pct=1.0, max_gross_exposure=1.0, max_drawdown_pct=0.20)
    )

    assert result.halted is True
    assert result.halt_ts == datetime(2024, 1, 4, tzinfo=UTC)
    assert _buy_fill_days(result) == [2], "no entry may resume under the permanent latch"
    assert result.halt_episode_count == 1
    (episode,) = result.halt_episodes
    assert episode.halt_ts == datetime(2024, 1, 4, tzinfo=UTC)
    assert episode.resume_ts is None  # still in force at the end of the run
    assert "drawdown" in episode.reason
    # Every bar from the crash on tried to top up and was blocked.
    blocked = [reason for order, reason in result.rejections if order.side is Side.BUY]
    assert len(blocked) >= 4 and all("drawdown" in reason for reason in blocked)


def test_cooldown_recovery_lets_entries_resume_after_the_crash() -> None:
    """The behavior the real-data finding demands (ADR-0031).

    Same series, same caps, plus ``halt_cooldown_bars=2``: the halt holds for days 4
    and 5, re-arms on day 6, the day-6 top-up is accepted and fills on day 7. That
    second buy fill is the whole point of the feature.
    """
    result = _run_crash_then_recover(
        RiskConfig(
            max_position_pct=1.0,
            max_gross_exposure=1.0,
            max_drawdown_pct=0.20,
            halt_cooldown_bars=2,
        )
    )

    assert result.halted is True  # a halt *occurred* — the run-level flag is sticky
    assert result.halt_ts == datetime(2024, 1, 4, tzinfo=UTC)
    assert _buy_fill_days(result) == [2, 7], "the re-armed switch must let an entry through"

    (episode,) = result.halt_episodes
    assert episode.halt_ts == datetime(2024, 1, 4, tzinfo=UTC)
    assert episode.resume_ts == datetime(2024, 1, 6, tzinfo=UTC)

    # Exactly the two halted bars rejected a buy; nothing after the re-arm did.
    blocked = [
        order
        for order, reason in result.rejections
        if order.side is Side.BUY and "drawdown" in reason
    ]
    assert len(blocked) == 2


def test_two_distinct_drawdown_events_report_two_episodes() -> None:
    """Episode recording: two crashes, two timestamped stretches (ADR-0031).

    A buy-and-hold-style single entry keeps the equity path arithmetic simple
    (``500 cash + 5 * price``): $100 -> $1,000, $50 -> $750 (a 25% drawdown), and
    back to $100 -> $1,000 (fully recovered, so the 5% recovery threshold re-arms).
    """
    bars = [
        _bar("AAA", 1, o=100, c=100),
        _bar("AAA", 2, o=100, c=100),  # the 50% buy fills here -> 5 shares
        _bar("AAA", 3, o=100, c=100),  # peak $1,000
        _bar("AAA", 4, o=100, c=50),  # crash #1 -> $750, halt
        _bar("AAA", 5, o=50, c=50),  # still 25% down
        _bar("AAA", 6, o=50, c=100),  # recovered to $1,000 -> re-arm
        _bar("AAA", 7, o=100, c=100),
        _bar("AAA", 8, o=100, c=50),  # crash #2 -> $750, halt again
        _bar("AAA", 9, o=50, c=50),
        _bar("AAA", 10, o=50, c=100),  # recovered again -> re-arm
    ]
    broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
    guardrails = Guardrails(
        RiskConfig(
            max_position_pct=1.0,
            max_gross_exposure=1.0,
            max_drawdown_pct=0.20,
            halt_recovery_drawdown_pct=0.05,
        )
    )
    result = Engine(FakeAdapter(bars), broker, guardrails).run(
        _EqualWeightOnce(["AAA"], weight=0.5),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 10, tzinfo=UTC),
    )

    def day(number: int) -> datetime:
        return datetime(2024, 1, number, tzinfo=UTC)

    assert result.halt_episode_count == 2
    spans = [(e.halt_ts, e.resume_ts) for e in result.halt_episodes]
    assert spans == [(day(4), day(6)), (day(8), day(10))]
    assert all("drawdown" in e.reason for e in result.halt_episodes)
    # The legacy fields still describe the FIRST halt, so old readers are unaffected —
    # timestamp *and* reason, which must not be paired across different episodes.
    assert result.halted is True
    assert result.halt_ts == day(4)
    assert result.halt_reason == result.halt_episodes[0].reason


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
