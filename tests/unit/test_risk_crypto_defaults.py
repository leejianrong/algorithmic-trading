"""The risk defaults are an equity posture; a 24/7 market needs a bounded halt.

ADR-0055 (KAN-709). Two halves, and the second is the point.

**The failure, measured.** A synthetic series at 80% annualized volatility — four
times :class:`~trading.data.synthetic.SyntheticParams`' default, with drift held
equal so volatility is the *only* thing that changed — driven through the real
:class:`~trading.engine.Engine` with the real :class:`~trading.risk.Guardrails` and
the default ``RiskConfig()``. The drawdown latch trips early and then holds for the
rest of the run, which is ADR-0031's measured failure (``cross_sectional``: -3.91%
latched vs +1727% neutralized on 2000-2020 equities) arriving in the first year.

**The calibration.** :meth:`RiskConfig.crypto` changes *one field*. The tests here
pin that (``test_crypto_posture_widens_nothing``) and pin the arithmetic floor under
the cooldown (``test_cooldown_clears_the_dispersion_floor``), so the two ways this
could quietly stop being a calibration — widening a cap until nothing trips, or
shortening the cooldown until the halt is cosmetic — both turn red.

**The caveat, which no test can remove.** A GBM series at crypto-like volatility is
*not* crypto: no fat tails, no regime breaks, no 2022-style 75% drawdown. These tests
establish the *shape* of the failure and that the posture bounds it. They do not
establish the right level for real crypto — see ADR-0055 for what would.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.config import CRYPTO_HALT_COOLDOWN_BARS, BacktestConfig, RiskConfig
from trading.data.synthetic import SyntheticAdapter, SyntheticParams
from trading.engine import BacktestResult, Engine
from trading.metrics import compute
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Order, Portfolio, Position, Side

# The measurement's fixed conditions. Drift is the synthetic default in both cases so
# the comparison isolates volatility; 0.80 is roughly four times the equity default
# and in the neighbourhood of a large-cap crypto's realized annual volatility.
EQUITY_VOL = 0.20
CRYPTO_VOL = 0.80
DRIFT = 0.08
SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
START = datetime(2015, 1, 1, tzinfo=UTC)
END = datetime(2020, 1, 1, tzinfo=UTC)


def _run(vol: float, risk: RiskConfig, *, seed: int = 0) -> BacktestResult:
    """One ``sma_crossover`` backtest at ``vol``, through the real engine path."""
    adapter = SyntheticAdapter(
        seed=seed, params=SyntheticParams(annual_drift=DRIFT, annual_vol=vol)
    )
    config = BacktestConfig()
    broker = SimulatedBroker(Portfolio(cash=config.starting_cash), config.costs)
    engine = Engine(adapter, broker, Guardrails(risk))
    return engine.run(get_strategy("sma_crossover"), list(SYMBOLS), START, END)


class TestPostureSurface:
    """What the two named postures are, and what they are not."""

    def test_equity_posture_is_exactly_the_field_defaults(self) -> None:
        assert RiskConfig.equity() == RiskConfig()

    def test_crypto_posture_widens_nothing(self) -> None:
        """The whole card: the crypto posture differs in ONE field, the cooldown.

        A guardrail relaxed until nothing trips is not a calibration. This asserts
        the position cap, the gross cap, the drawdown threshold, the daily-loss
        breaker and the sector cap are all still the equity numbers.
        """
        equity = dataclasses.asdict(RiskConfig.equity())
        crypto = dataclasses.asdict(RiskConfig.crypto())
        differing = {k for k in equity if equity[k] != crypto[k]}
        assert differing == {"halt_cooldown_bars"}
        assert crypto["max_position_pct"] == 0.25
        assert crypto["max_gross_exposure"] == 1.0
        assert crypto["max_drawdown_pct"] == 0.20

    def test_crypto_posture_cannot_latch_for_the_whole_run(self) -> None:
        assert RiskConfig.crypto().halt_recovery_enabled is True
        assert RiskConfig.equity().halt_recovery_enabled is False

    def test_crypto_posture_refuses_a_missing_cooldown(self) -> None:
        """Recovery is not optional here — so "off" is a refusal, not a config."""
        with pytest.raises(ValueError, match="requires a halt cooldown"):
            RiskConfig.crypto(halt_cooldown_bars=None)

    def test_crypto_posture_leaves_the_single_bar_breaker_off(self) -> None:
        """Deliberately un-calibrated: see ADR-0055.

        Over 26,090 bar-to-bar portfolio returns at 80% volatility the worst single
        bar lost 9.29% and nothing reached 10%, so any breaker at or above 10% is a
        dead knob on this evidence — and GBM has no fat tails, which is precisely
        where a real crypto flash crash lives. Sizing it needs real returns.
        """
        assert RiskConfig.crypto().max_daily_loss_pct is None

    def test_cooldown_clears_the_dispersion_floor(self) -> None:
        """The cooldown's floor is arithmetic, not taste.

        A cooldown shorter than ``(threshold / per-bar sigma)²`` bars re-arms before
        the market has moved a threshold's worth, i.e. inside the same move that
        tripped the switch. At 80% annualized volatility on daily bars that floor is
        ~16 bars. This is the guard against "shorten the cooldown until the halt
        stops costing anything".
        """
        sigma_bar = CRYPTO_VOL / math.sqrt(252)
        floor = math.ceil((RiskConfig.crypto().max_drawdown_pct / sigma_bar) ** 2)
        assert floor == 16
        assert floor <= CRYPTO_HALT_COOLDOWN_BARS

    def test_overriding_the_cooldown_keeps_everything_else(self) -> None:
        assert RiskConfig.crypto(halt_cooldown_bars=90) == dataclasses.replace(
            RiskConfig.crypto(), halt_cooldown_bars=90
        )


class TestEquityDefaultsOnACryptoVolatilitySeries:
    """The failure being calibrated against, driven through the real engine."""

    def test_the_latch_trips_early_and_never_lifts(self) -> None:
        result = _run(CRYPTO_VOL, RiskConfig())
        bars = len(result.equity_curve)
        assert result.halted, "the equity default did not trip at 4x volatility"
        # One episode, opened and never closed: the ADR-0013 latch.
        assert len(result.halt_episodes) == 1
        assert result.halt_episodes[0].resume_ts is None
        # Early: inside the first fifth of the run, not near the end.
        halt_index = next(
            i for i, point in enumerate(result.equity_curve) if point.ts == result.halt_ts
        )
        assert halt_index < bars / 5, f"halted at bar {halt_index} of {bars}"

    def test_the_latch_then_dominates_the_run(self) -> None:
        """Not just "a halt happened" — the halt *is* the result after that."""
        latched = _run(CRYPTO_VOL, RiskConfig())
        bounded = _run(CRYPTO_VOL, RiskConfig.crypto())
        latched_metrics = compute(latched)
        bounded_metrics = compute(bounded)
        # The book drains to cash and stays there: exposure collapses by >5x, and
        # every bar's worth of entries is refused instead of sized.
        assert latched_metrics.avg_exposure < bounded_metrics.avg_exposure / 5
        assert len(latched.rejections) > 5 * len(bounded.rejections)

    def test_the_same_defaults_do_not_trip_at_equity_volatility(self) -> None:
        """The defaults are not broken — they are calibrated for a different market."""
        result = _run(EQUITY_VOL, RiskConfig())
        assert not result.halted


class TestCryptoPostureOnTheSameSeries:
    """What the chosen posture does to the series that broke the default."""

    def test_the_halt_still_fires_and_still_blocks_entries(self) -> None:
        """A guardrail that never trips is not a guardrail. This one trips."""
        result = _run(CRYPTO_VOL, RiskConfig.crypto())
        assert result.halted
        assert result.halt_episodes, "the crypto posture stopped guarding anything"
        assert result.rejections, "no entry was ever refused: the halt is cosmetic"

    def test_every_halt_is_bounded_and_the_run_keeps_trading(self) -> None:
        result = _run(CRYPTO_VOL, RiskConfig.crypto())
        # More than one episode is the observable difference from a latch, and each
        # one closes: the switch re-arms rather than ending the run.
        assert len(result.halt_episodes) > 1
        unresolved = [ep for ep in result.halt_episodes if ep.resume_ts is None]
        assert len(unresolved) <= 1, "a bounded halt was left open mid-run"
        assert compute(result).avg_exposure > 0.20

    def test_halts_cannot_recur_faster_than_the_cooldown_plus_one(self) -> None:
        """ADR-0031's anti-flap bound, re-asserted at crypto volatility.

        The cost of un-latching is halt/re-enter churn, so the bound matters more
        here than it did for equities: this is what stops "recovery is mandatory"
        from meaning "the switch flaps every bar".
        """
        result = _run(CRYPTO_VOL, RiskConfig.crypto())
        index = {point.ts: i for i, point in enumerate(result.equity_curve)}
        starts = [index[ep.halt_ts] for ep in result.halt_episodes]
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert all(gap >= CRYPTO_HALT_COOLDOWN_BARS + 1 for gap in gaps), gaps

    def test_the_posture_is_not_a_return_improvement_claim(self) -> None:
        """Honesty rail: the posture recovers the latch's damage, nothing more.

        Two different claims, and only the first is systematic. Against the latch the
        bounded halt wins on **every** seed — that is the defect being fixed. Against
        *no* drawdown halt at all it is a coin flip (4 of 10 seeds measured; 2 of the
        6 run here), because a cooldown re-arms on the calendar rather than on
        evidence that anything improved (ADR-0031's stated cost). If this ever came
        out unanimous the honest reading would be that the halt had stopped costing
        anything, not that risk management had started paying.
        """
        seeds = 6
        wins_against_no_halt = 0
        for seed in range(seeds):
            latched = compute(_run(CRYPTO_VOL, RiskConfig(), seed=seed)).total_return
            bounded = compute(_run(CRYPTO_VOL, RiskConfig.crypto(), seed=seed)).total_return
            neutralized = compute(
                _run(CRYPTO_VOL, RiskConfig(max_drawdown_pct=0.99), seed=seed)
            ).total_return
            assert latched < bounded, f"seed {seed}: the latch was not the problem"
            wins_against_no_halt += bounded > neutralized
        assert 0 < wins_against_no_halt < seeds, wins_against_no_halt

    def test_a_recovery_threshold_alone_is_a_latch_in_disguise(self) -> None:
        """Why the posture is a *cooldown*, not a drawdown-recovery threshold.

        A halted long-or-flat book drains to cash and freezes its drawdown, so a
        drawdown-recovery condition not already satisfied at that moment never
        becomes satisfiable — ADR-0031 §2's deadlock, which bites at this volatility
        even under OR. Measured: 1 halt, never resumed.
        """
        drawdown_only = _run(CRYPTO_VOL, RiskConfig(halt_recovery_drawdown_pct=0.10))
        assert len(drawdown_only.halt_episodes) == 1
        assert drawdown_only.halt_episodes[0].resume_ts is None

    def test_the_posture_changes_nothing_at_equity_volatility(self) -> None:
        """It is a posture for a different market, not a covert loosening.

        On the series the equity defaults were written for, the cooldown never has
        anything to re-arm, so the two postures produce the same curve.
        """
        equity_run = _run(EQUITY_VOL, RiskConfig.equity())
        crypto_run = _run(EQUITY_VOL, RiskConfig.crypto())
        assert equity_run.equity_curve == crypto_run.equity_curve


class TestExitsStayAllowedWhileHalted:
    """ADR-0013 §2 / ADR-0031 §4 under the new posture: always a way out."""

    def test_a_sell_passes_and_a_buy_is_refused_while_halted(self) -> None:
        guardrails = Guardrails(RiskConfig.crypto())
        prices = {"AAA": 100.0}
        # Trip the switch: a peak, then a >20% decline.
        guardrails.halted(Portfolio(cash=1_000.0), prices)
        crashed = Portfolio(cash=700.0)
        assert guardrails.halted(crashed, prices) is True

        held = Portfolio(cash=700.0, positions={"AAA": Position("AAA", 2.0, 100.0)})
        exit_order = Order("AAA", Side.SELL, 2.0)
        assert guardrails.check(exit_order, held, prices) == exit_order
        assert guardrails.check(Order("AAA", Side.BUY, 1.0), held, prices) is None

    def test_an_exit_is_allowed_on_every_bar_of_a_crypto_run(self) -> None:
        """End to end: no episode ever refused a reduction.

        Sells are what ``sma_crossover`` uses to leave a position, so a halt that
        blocked them would trap the book. Every rejection in the run must be a buy.
        """
        result = _run(CRYPTO_VOL, RiskConfig.crypto())
        refused_sells = [
            (order, reason) for order, reason in result.rejections if order.side is Side.SELL
        ]
        assert refused_sells == []
