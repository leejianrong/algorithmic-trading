"""Run configuration and cost defaults.

Defaults reflect a small, real account (ADR-0011, Q22): $1,000 of capital and
commission-free trades with a modest, deliberately pessimistic slippage so
backtests don't flatter themselves (Q14).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Trading cost assumptions applied by the simulated broker."""

    commission_per_share: float = 0.0
    slippage_bps: float = 5.0  # 5 basis points = 0.05% adverse move on each fill.

    def __post_init__(self) -> None:
        if self.commission_per_share < 0:
            raise ValueError("commission_per_share must be non-negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")


# The one number the 24/7 (crypto) posture changes (ADR-0055). Everything else in
# :meth:`RiskConfig.crypto` is the equity default, deliberately: the measurement
# behind this card found the *latch*, not the levels, is what breaks at crypto
# volatility, and widening a cap until nothing trips is a disabled guardrail with
# extra steps. The floor is arithmetic — a cooldown shorter than
# ``(max_drawdown_pct / per-bar sigma)²`` bars re-arms inside the same move that
# tripped the switch; at 80% annualized vol on daily bars that is ~16 bars, and 30
# is the next legible unit above it (a month of a market that never closes).
CRYPTO_HALT_COOLDOWN_BARS = 30


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Enforced risk limits (ADR-0009, ADR-0013).

    Guardrails are on by default with a small, real-account posture: no single
    symbol over a quarter of equity, no leverage (gross ≤ 100%), and a hard halt
    once drawdown from the equity peak reaches a fifth. ``max_daily_loss_pct`` is
    an optional single-bar circuit breaker, off by default. ``target_volatility``
    is an optional annualized volatility target (e.g. 0.10 for 10%) that scales the
    effective gross-exposure cap up or down toward that target (ADR-0015); off by
    default, so behavior is unchanged unless it is set. ``max_sector_exposure`` with
    a ``sector_map`` is an optional per-sector gross cap (ADR-0019) that limits how
    much of equity may sit in any one sector; off by default. Every limit is
    overridable per run; :meth:`unlimited` returns the permissive opt-out.

    ``halt_recovery_drawdown_pct`` and ``halt_cooldown_bars`` are the two optional
    **halt-recovery** knobs (ADR-0031). Both are ``None`` by default, which keeps the
    kill switch **latching for the whole run** exactly as ADR-0013 decided. Set
    either (or both) and a tripped halt can *re-arm*:

    * ``halt_recovery_drawdown_pct`` — re-arm once drawdown from the peak has
      recovered back to at most this fraction. It must be strictly **below**
      ``max_drawdown_pct``: that gap is the hysteresis band, and validating it here
      is the first of the two anti-flap guarantees (a config where the trip and
      re-arm levels coincide is rejected, not silently allowed to oscillate).
    * ``halt_cooldown_bars`` — re-arm only after the halt has been in force for this
      many bars (counting the bar it fired on). Must be a positive integer.

    With both set the **earlier** trigger re-arms the switch (OR). ADR-0031 explains
    why the more conservative AND was rejected: a halted long-or-flat strategy may
    exit but not enter, so it drains to cash and its equity — hence its drawdown —
    freezes, and a drawdown condition not already met at that moment can never be
    met. AND would therefore silently reinstate the permanent latch.

    **The defaults above are an equity posture (ADR-0055).** They are calibrated for
    mega-cap US equities at roughly 20% annualized volatility, and the field defaults
    do not move. :meth:`equity` names that posture explicitly and returns exactly
    ``RiskConfig()``; :meth:`crypto` is the 24/7 posture, which differs in **one
    field** — it makes halt recovery mandatory rather than optional. Measured, the
    levels are not what breaks at four times the volatility; the *latch* is.
    """

    max_position_pct: float = 0.25
    max_gross_exposure: float = 1.0
    max_drawdown_pct: float = 0.20
    max_daily_loss_pct: float | None = None
    target_volatility: float | None = None
    sector_map: Mapping[str, str] | None = None
    max_sector_exposure: float | None = None
    halt_recovery_drawdown_pct: float | None = None
    halt_cooldown_bars: int | None = None

    @property
    def halt_recovery_enabled(self) -> bool:
        """Whether any recovery mechanism is configured (ADR-0031).

        ``False`` — the default — means the halt latches for the run, the ADR-0013
        behavior. The monitor checks this once per bar and skips the whole re-arm
        path when it is off, so the latching path is untouched.
        """
        return self.halt_recovery_drawdown_pct is not None or self.halt_cooldown_bars is not None

    def __post_init__(self) -> None:
        if self.max_position_pct <= 0:
            raise ValueError("max_position_pct must be positive")
        if self.max_gross_exposure <= 0:
            raise ValueError("max_gross_exposure must be positive")
        if not 0 < self.max_drawdown_pct <= 1.0:
            raise ValueError("max_drawdown_pct must be in (0, 1]")
        if self.max_daily_loss_pct is not None and not 0 < self.max_daily_loss_pct <= 1.0:
            raise ValueError("max_daily_loss_pct must be None or in (0, 1]")
        if self.target_volatility is not None and self.target_volatility <= 0:
            raise ValueError("target_volatility must be None or positive")
        if self.max_sector_exposure is not None and not 0 < self.max_sector_exposure <= 1.0:
            raise ValueError("max_sector_exposure must be None or in (0, 1]")
        recovery = self.halt_recovery_drawdown_pct
        if recovery is not None:
            if not 0 <= recovery < 1.0:
                raise ValueError("halt_recovery_drawdown_pct must be None or in [0, 1)")
            # The hysteresis band must be non-empty: re-arming at (or above) the
            # trip level would let the switch halt and re-arm on adjacent bars
            # forever (ADR-0031, anti-flap guarantee 1).
            if recovery >= self.max_drawdown_pct:
                raise ValueError(
                    "halt_recovery_drawdown_pct must be strictly below max_drawdown_pct "
                    f"(got {recovery} >= {self.max_drawdown_pct}); the gap is the "
                    "hysteresis band that stops the kill switch from flapping"
                )
        if self.halt_cooldown_bars is not None and self.halt_cooldown_bars < 1:
            raise ValueError("halt_cooldown_bars must be None or a positive integer")

    @classmethod
    def unlimited(cls) -> RiskConfig:
        """A fully permissive config — the explicit opt-out from enforcement.

        Position and gross caps are infinite (never clamp), the drawdown halt is
        unreachable (fires only at total wipe-out), and the daily-loss breaker is
        off. Pass this to disable guardrails without forking the engine's path.
        """
        return cls(
            max_position_pct=float("inf"),
            max_gross_exposure=float("inf"),
            max_drawdown_pct=1.0,
            max_daily_loss_pct=None,
        )

    @classmethod
    def equity(cls) -> RiskConfig:
        """The equity posture — exactly the field defaults, named (ADR-0055).

        Returns ``cls()``. It exists so a caller choosing a market chooses one
        *explicitly*, instead of the equity assumption being the unnamed thing that
        happens when nobody chooses. Pinned equal to ``RiskConfig()`` by a test, so
        this can never quietly become a third posture.
        """
        return cls()

    @classmethod
    def crypto(cls, *, halt_cooldown_bars: int | None = CRYPTO_HALT_COOLDOWN_BARS) -> RiskConfig:
        """The 24/7 posture: the equity levels, but the kill switch may not latch.

        ADR-0055. This differs from :meth:`equity` in **exactly one field** —
        ``halt_cooldown_bars`` — and a test asserts that, so widening a cap here
        turns red. Nothing is loosened: ``max_position_pct`` (25%),
        ``max_gross_exposure`` (100%, i.e. no leverage) and ``max_drawdown_pct``
        (20%) are the equity numbers, unchanged.

        The reason is measured rather than assumed. Driven through the real
        :class:`~trading.engine.Engine` on a synthetic series at 80% annualized
        volatility (four times the equity default, drift held equal so volatility is
        the only change), the default latching config halted in **20 of 20 seeds**,
        typically about 250 bars into a 2,610-bar run, and then spent a median
        **90.5%** of the run refusing entries: median total return **+8.95%** against
        **+561.93%** with the drawdown halt neutralized. That is ADR-0031's measured
        failure — the same one that turned ``cross_sectional`` into -3.91% on
        2000-2020 equities — arriving in the first year instead of the second.

        The fix is therefore *bounding* the halt, not raising the bar it trips over.
        A 20% drawdown genuinely is ordinary here (84% of bars in that series sit at
        or below 20% off their running peak, against 30% of the equity-volatility
        bars), so the switch fires often — roughly 7-8 bounded episodes per ten
        years, holding a median 8.6% of the run — and that is a working circuit
        breaker rather than a broken kill switch. Raising the threshold to the same
        *tail rank* that 20% occupies for equities would mean **78%**, past even a
        2022-style crypto drawdown: a number that never fires, which is a disabled
        guardrail, not a calibrated one.

        ``halt_recovery_drawdown_pct`` stays ``None`` deliberately, also on evidence:
        alone, at this volatility, it re-armed **nothing** (1 halt, never resumed,
        +11.72% — the permanent latch in disguise), because a halted long-or-flat
        book drains to cash and freezes its drawdown above the threshold, exactly the
        deadlock ADR-0031 §2 measured. The cooldown is the liveness guarantee; a
        recovery threshold is an early re-arm a caller may add.

        ``halt_cooldown_bars`` may be overridden for a different volatility or bar
        interval, but it may **not** be ``None``: a 24/7 posture whose halt latches
        for the whole run is the thing this preset exists to prevent. The parameter
        admits ``None`` in its type only so the refusal is expressible — the field
        itself is ``int | None``, and an untyped caller (a future CLI flag, a config
        file) can hand one over. It is a ``ValueError``, not a silently latching
        config.
        """
        if halt_cooldown_bars is None:
            raise ValueError(
                "the 24/7 posture requires a halt cooldown: halt recovery is not "
                "optional there (ADR-0055). A latching kill switch was measured to "
                "spend ~90% of a crypto-volatility run refusing entries. Use "
                "RiskConfig() for the latching equity posture instead."
            )
        return cls(halt_cooldown_bars=halt_cooldown_bars)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything a backtest run needs beyond the strategy and the data."""

    starting_cash: float = 1_000.0
    costs: CostConfig = CostConfig()

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
