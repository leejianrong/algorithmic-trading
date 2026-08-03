"""Run configuration and cost defaults.

Defaults reflect a small, real account (ADR-0011, Q22): $1,000 of capital and
commission-free trades with a modest, deliberately pessimistic slippage so
backtests don't flatter themselves (Q14).
"""

from __future__ import annotations

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
class BacktestConfig:
    """Everything a backtest run needs beyond the strategy and the data."""

    starting_cash: float = 1_000.0
    costs: CostConfig = CostConfig()

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
