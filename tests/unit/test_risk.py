"""Unit tests for the enforced guardrails (ADR-0009, ADR-0013).

Fast, no infrastructure: the checker and the kill switch are exercised directly
against hand-built portfolios. The pre-trade check accepts an in-limit order,
clamps an over-cap one to the cap with the right reason, and rejects when the
clamp collapses to nothing; the drawdown monitor fires exactly at the threshold,
not just before it, and latches once tripped.
"""

from __future__ import annotations

from itertools import pairwise

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
        # Halt recovery (ADR-0031) is opt-in: unset means the ADR-0013 permanent latch.
        assert cfg.halt_recovery_drawdown_pct is None
        assert cfg.halt_cooldown_bars is None
        assert cfg.halt_recovery_enabled is False

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
            {"halt_recovery_drawdown_pct": -0.1},
            {"halt_recovery_drawdown_pct": 1.0},
            {"halt_cooldown_bars": 0},
            {"halt_cooldown_bars": -3},
        ],
    )
    def test_out_of_range_limits_are_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            RiskConfig(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("recovery", [0.20, 0.25])
    def test_recovery_threshold_at_or_above_the_trip_level_is_rejected(
        self, recovery: float
    ) -> None:
        """The hysteresis band must be non-empty — anti-flap guarantee 1 (ADR-0031).

        Re-arming at (or below) the same drawdown that trips the switch would let it
        halt and re-arm on adjacent bars forever, so the config refuses it outright
        rather than leaving the oscillation to be discovered in a run.
        """
        with pytest.raises(ValueError, match="strictly below max_drawdown_pct"):
            RiskConfig(max_drawdown_pct=0.20, halt_recovery_drawdown_pct=recovery)

    def test_a_recovery_knob_turns_recovery_on(self) -> None:
        assert RiskConfig(halt_cooldown_bars=5).halt_recovery_enabled is True
        assert RiskConfig(halt_recovery_drawdown_pct=0.10).halt_recovery_enabled is True


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


class TestHaltRecovery:
    """Opt-in re-arming of the drawdown kill switch (ADR-0031).

    Every case drives :meth:`Guardrails.halted` directly with hand-written equity
    paths (flat books, so equity is just cash), one call per bar, and asserts the
    latch state bar by bar. The first test is the load-bearing one: with no recovery
    configured the switch must behave exactly as it did before this feature existed.
    """

    @staticmethod
    def _walk(guard: Guardrails, equities: list[float]) -> list[bool]:
        """Feed one bar per equity value; return the latch state after each bar."""
        return [guard.halted(_portfolio(equity), {}) for equity in equities]

    def test_default_config_latches_for_the_whole_run(self) -> None:
        # Backward compatibility, non-negotiable: no recovery configured means the
        # ADR-0013 permanent latch, no matter how far equity recovers afterwards.
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20))
        states = self._walk(guard, [1_000.0, 700.0, 900.0, 1_000.0, 1_500.0, 2_000.0])
        assert states == [False, True, True, True, True, True]
        assert guard.halt_count == 1
        assert guard.resume_count == 0
        assert guard.is_halted is True

    def test_cooldown_re_arms_on_exactly_the_nth_bar(self) -> None:
        # Cooldown 3: the halt is in force for exactly 3 bars — the bar it fired on
        # plus two more — and the 4th bar trades again, even though equity is still
        # deeply underwater relative to the original peak.
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_cooldown_bars=3))
        states = self._walk(guard, [1_000.0, 700.0, 700.0, 700.0, 700.0, 700.0])
        assert states == [False, True, True, True, False, False]
        assert guard.halt_count == 1
        assert guard.resume_count == 1

    def test_cooldown_off_by_one_neither_early_nor_late(self) -> None:
        # Cooldown 1 re-arms on the very next bar; cooldown 2 holds one bar longer.
        one = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_cooldown_bars=1))
        assert self._walk(one, [1_000.0, 700.0, 700.0]) == [False, True, False]
        two = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_cooldown_bars=2))
        assert self._walk(two, [1_000.0, 700.0, 700.0, 700.0]) == [False, True, True, False]

    def test_bars_halted_counts_the_firing_bar(self) -> None:
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_cooldown_bars=5))
        assert guard.bars_halted == 0
        guard.halted(_portfolio(1_000.0), {})
        assert guard.bars_halted == 0
        guard.halted(_portfolio(700.0), {})  # the bar it fires on is bar 1 of the halt
        assert guard.bars_halted == 1
        guard.halted(_portfolio(700.0), {})
        assert guard.bars_halted == 2

    def test_drawdown_recovery_waits_until_the_threshold_is_regained(self) -> None:
        # Trip at 20% down, re-arm only once drawdown is back to 10% or better.
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_recovery_drawdown_pct=0.10))
        states = self._walk(
            guard,
            [
                1_000.0,  # peak
                750.0,  # 25% down -> halt
                820.0,  # 18% down -> still underwater, still halted
                899.0,  # 10.1% down -> a hair short, still halted
                900.0,  # exactly 10% down -> re-arms
            ],
        )
        assert states == [False, True, True, True, False]
        assert guard.halt_count == 1 and guard.resume_count == 1

    def test_re_arming_restarts_the_drawdown_reference_at_the_resume_level(self) -> None:
        # Anti-flap guarantee 2: after re-arming at 900 the peak *is* 900, so a fall
        # to 800 is an 11% drawdown and passes. Measured from the original 1,000 peak
        # it would be exactly 20% and would trip the switch straight back on.
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_recovery_drawdown_pct=0.10))
        states = self._walk(guard, [1_000.0, 750.0, 900.0, 800.0])
        assert states == [False, True, False, False]
        assert guard.halt_count == 1
        # A fresh, full 20% decline from the resume level (900 -> 720) does trip it.
        assert guard.halted(_portfolio(720.0), {}) is True
        assert guard.halt_count == 2

    @staticmethod
    def _both() -> Guardrails:
        return Guardrails(
            RiskConfig(
                max_drawdown_pct=0.20,
                halt_recovery_drawdown_pct=0.10,
                halt_cooldown_bars=3,
            )
        )

    def test_both_knobs_re_arm_on_whichever_triggers_first(self) -> None:
        # Recovery first: equity is back to a 5% drawdown on the very next bar, so the
        # switch re-arms there rather than serving out the 3-bar cooldown.
        recovery_first = self._both()
        assert self._walk(recovery_first, [1_000.0, 750.0, 950.0, 950.0]) == [
            False,
            True,
            False,
            False,
        ]
        assert recovery_first.resume_count == 1

        # Cooldown first: equity stays 25% underwater, so the drawdown condition never
        # fires and the 3-bar cooldown is what re-arms the switch.
        cooldown_first = self._both()
        assert self._walk(cooldown_first, [1_000.0, 750.0, 750.0, 750.0, 750.0]) == [
            False,
            True,
            True,
            True,
            False,
        ]
        assert cooldown_first.resume_count == 1

    def test_a_book_that_froze_flat_still_re_arms(self) -> None:
        """Regression for the deadlock that ruled out AND (ADR-0031).

        A halted long-or-flat strategy may exit but not enter, so it drains to cash
        and equity stops moving. Drawdown then freezes above the recovery threshold
        and can *never* be regained. Requiring both conditions would reinstate the
        permanent latch here; because the cooldown is an independent trigger, the
        frozen book still resumes. Measured on a 2005-2020 synthetic run, where an
        AND build left the second episode in force for the final eleven years.
        """
        guard = self._both()
        frozen = [1_000.0, 750.0] + [750.0] * 30
        states = self._walk(guard, frozen)
        assert states[1] is True  # halted on the crash bar
        assert guard.resume_count == 1
        assert guard.is_halted is False, "a frozen equity curve must not deadlock the switch"

    def test_sawtooth_inside_the_resume_band_never_re_halts(self) -> None:
        """Anti-flap: an oscillation whose legs stay inside the threshold halts once.

        Crash to 750 (halt), recover to 900 (re-arm, peak reset to 900), then swing
        900 <-> 760 for twenty cycles. Each dip is 15.6% from the resume level — under
        the 20% trigger — so the bound is **exactly one** halt episode. Without the
        peak reset every dip would read 24% from the old 1,000 peak and the switch
        would halt and re-arm on every cycle.
        """
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_recovery_drawdown_pct=0.10))
        path = [1_000.0, 750.0, 900.0]
        for _ in range(20):
            path.extend([760.0, 900.0])
        self._walk(guard, path)
        assert guard.halt_count == 1
        assert guard.resume_count == 1
        assert guard.is_halted is False

    def test_hostile_per_bar_sawtooth_stays_bounded_by_the_cooldown(self) -> None:
        """Anti-flap: a curve engineered to flap cannot halt more than once per N+1 bars.

        Equity alternates 1,000 / 800 every bar, so every down bar is a fresh 20%
        drawdown from a fresh peak — the worst case for a re-arming switch. With a
        3-bar cooldown each episode occupies 3 bars and guarantee 3 grants at least
        one trading bar before the next trip, so the period is at least 4 bars.
        """
        cooldown = 3
        guard = Guardrails(RiskConfig(max_drawdown_pct=0.20, halt_cooldown_bars=cooldown))
        path = [1_000.0]
        for _ in range(20):
            path.extend([800.0, 1_000.0])
        states = self._walk(guard, path)

        bars = len(path)
        bound = bars // (cooldown + 1) + 1
        assert guard.halt_count <= bound, f"{guard.halt_count} halts in {bars} bars exceeds {bound}"
        assert guard.halt_count > 1, "the hostile path should re-halt; this is not a latch"
        # Strict alternation: a re-arm bar never re-halts, so transitions can't stack.
        assert guard.resume_count in (guard.halt_count - 1, guard.halt_count)
        transitions = sum(1 for prev, cur in pairwise(states) if prev != cur)
        assert transitions == guard.halt_count + guard.resume_count

    def test_entries_blocked_and_exits_allowed_while_halted_then_entries_resume(self) -> None:
        # ADR-0013's invariant is untouched by recovery: while halted, only exits
        # pass. Once re-armed, a new entry is accepted again — the whole point.
        guard = Guardrails(
            RiskConfig(
                max_position_pct=1.0,
                max_gross_exposure=1.0,
                max_drawdown_pct=0.20,
                halt_cooldown_bars=1,
            )
        )
        pf = _portfolio(500.0, [Position("AAA", qty=5.0, avg_price=100.0)])
        prices = {"AAA": 100.0}

        guard.halted(_portfolio(1_000.0), {})
        assert guard.halted(_portfolio(700.0), {}) is True
        assert guard.check(Order("AAA", Side.BUY, 1.0), pf, prices) is None
        exit_order = Order("AAA", Side.SELL, 5.0)
        assert guard.check(exit_order, pf, prices) is exit_order

        assert guard.halted(_portfolio(700.0), {}) is False  # cooldown served → re-armed
        entry = Order("AAA", Side.BUY, 1.0)
        assert guard.check(entry, pf, prices) is entry


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
