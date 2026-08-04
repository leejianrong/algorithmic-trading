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


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything a backtest run needs beyond the strategy and the data."""

    starting_cash: float = 1_000.0
    costs: CostConfig = CostConfig()

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
