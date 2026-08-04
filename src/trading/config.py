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
    """

    max_position_pct: float = 0.25
    max_gross_exposure: float = 1.0
    max_drawdown_pct: float = 0.20
    max_daily_loss_pct: float | None = None
    target_volatility: float | None = None
    sector_map: Mapping[str, str] | None = None
    max_sector_exposure: float | None = None

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
