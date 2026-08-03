"""Integration: a real buy-and-hold backtest across a known stock split.

Marked ``integration`` (network / yfinance), so it's CI-only and never in the
fast gate. It guards ADR-0008: on adjusted prices, AAPL's 4-for-1 split on
2020-08-31 must NOT appear as a ~75% one-day crash in the equity curve.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading.broker import SimulatedBroker
from trading.data.yfinance_adapter import YFinanceAdapter
from trading.engine import Engine
from trading.strategies.buy_and_hold import BuyAndHold
from trading.types import Portfolio

pytestmark = pytest.mark.integration


def test_buy_and_hold_across_apple_split_has_no_phantom_crash(tmp_path: Path) -> None:
    adapter = YFinanceAdapter(tmp_path / "cache")
    broker = SimulatedBroker(Portfolio(cash=1_000.0))
    result = Engine(adapter, broker).run(
        BuyAndHold(),
        ["AAPL"],
        datetime(2020, 6, 1, tzinfo=UTC),
        datetime(2020, 12, 1, tzinfo=UTC),
    )

    assert len(result.equity_curve) > 100  # roughly six months of trading days
    assert result.final_equity > 0

    equities = [p.equity for p in result.equity_curve]
    daily_returns = [
        equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0
    ]
    # A raw (unadjusted) series would show ~ -0.75 on the split day; adjusted must not.
    assert min(daily_returns) > -0.35, "phantom split crash — prices are not adjusted"

    # Buy-and-hold ends holding a single position and essentially no idle cash.
    assert set(result.final_portfolio.positions) == {"AAPL"}
