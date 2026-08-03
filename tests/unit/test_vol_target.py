"""Fast, no-infra tests for volatility-target exposure scaling (ADR-0015).

The scaling lives inside :class:`~trading.risk.Guardrails`: when
``RiskConfig.target_volatility`` is set, a rolling window of the per-bar equity
the drawdown monitor already observes drives a multiplier on the effective
gross-exposure cap. A turbulent book earns *less* gross; a calm one earns more;
with the target unset the scale is a constant 1.0 and every path is unchanged.

The window is fed directly through :meth:`Guardrails.halted` (flat books, so
equity equals cash), then a single buy is pushed through :meth:`Guardrails.check`
and the accepted quantity compared with and without the target.
"""

from __future__ import annotations

from collections.abc import Sequence

from trading.config import RiskConfig
from trading.risk import Guardrails
from trading.types import Order, Portfolio, Side

# A deliberately violent equity path: big alternating up/down swings so realized
# volatility is far above any sane target, forcing the scale well below 1.0.
_VOLATILE = [1000.0, 700.0, 1000.0, 700.0, 1000.0]
# A near-flat path: tiny swings, so realized volatility is far below the target
# and the scale saturates at the upper clamp.
_CALM = [1000.0, 1000.5, 1000.0, 1000.5, 1000.0]


def _portfolio(cash: float) -> Portfolio:
    return Portfolio(cash=cash, positions={})


def _feed(guard: Guardrails, equities: Sequence[float]) -> None:
    """Drive ``halted`` over a flat-book equity path to seed the return window."""
    for eq in equities:
        guard.halted(_portfolio(eq), {})


class TestHighVolScalesDown:
    def test_effective_gross_cap_is_scaled_down_versus_off(self) -> None:
        base = dict(max_gross_exposure=1.0, max_position_pct=1.0, max_drawdown_pct=1.0)
        on = Guardrails(RiskConfig(target_volatility=0.10, **base))
        off = Guardrails(RiskConfig(**base))
        _feed(on, _VOLATILE)
        _feed(off, _VOLATILE)

        pf = _portfolio(1000.0)
        prices = {"AAA": 100.0}
        on.halted(pf, prices)  # begin a clean bar on the book we will check
        off.halted(pf, prices)

        order = Order("AAA", Side.BUY, 10.0)  # 100% of equity at $100
        checked_on = on.check(order, pf, prices)
        checked_off = off.check(order, pf, prices)

        # Off: the full 10-share order fits under the unscaled 100% gross cap.
        assert checked_off is order
        # On: the turbulent book scaled the gross cap down, so the order is clamped.
        assert on.vol_scale < 1.0
        assert checked_on is not None
        assert checked_on.qty < 10.0 - 1e-9
        assert "gross exposure cap" in (on.last_reason or "")


class TestLowVolScalesUp:
    def test_calm_book_earns_more_than_base_gross(self) -> None:
        # Position cap held loose so the *gross* cap is what binds; a calm book
        # scales it above the base 100%, letting a 200%-of-equity order through.
        on = Guardrails(
            RiskConfig(
                target_volatility=0.10,
                max_gross_exposure=1.0,
                max_position_pct=5.0,
                max_drawdown_pct=1.0,
            )
        )
        _feed(on, _CALM)
        pf = _portfolio(1000.0)
        prices = {"AAA": 100.0}
        on.halted(pf, prices)

        assert on.vol_scale > 1.0
        # Base gross cap allows only 10 shares; the scaled cap admits all 20.
        checked = on.check(Order("AAA", Side.BUY, 20.0), pf, prices)
        assert checked is not None
        assert checked.qty == 20.0


class TestOffByDefault:
    def test_no_target_is_a_noop_even_after_volatile_history(self) -> None:
        guard = Guardrails(
            RiskConfig(max_gross_exposure=1.0, max_position_pct=1.0, max_drawdown_pct=1.0)
        )
        _feed(guard, _VOLATILE)
        assert guard.vol_scale == 1.0

        pf = _portfolio(1000.0)
        prices = {"AAA": 100.0}
        guard.halted(pf, prices)
        order = Order("AAA", Side.BUY, 10.0)
        # Unscaled cap: the whole order passes untouched — behavior is unchanged.
        assert guard.check(order, pf, prices) is order

    def test_scale_stays_one_until_two_returns_are_seen(self) -> None:
        guard = Guardrails(RiskConfig(target_volatility=0.10, max_drawdown_pct=1.0))
        assert guard.vol_scale == 1.0
        guard.halted(_portfolio(1000.0), {})  # first bar: no return yet
        assert guard.vol_scale == 1.0
        guard.halted(_portfolio(700.0), {})  # one return: still not enough
        assert guard.vol_scale == 1.0
        guard.halted(_portfolio(1000.0), {})  # two returns: scaling engages
        assert guard.vol_scale < 1.0
