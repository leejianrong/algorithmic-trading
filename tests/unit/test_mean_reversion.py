"""Mean-reversion buys an oversold dip and exits once RSI recovers.

A crafted single-symbol series drives the strategy through one entry (RSI below
the oversold floor) and one exit (RSI back above the recovery level) via the
engine's fill blotter, plus a smoke run on synthetic data.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.broker import SimulatedBroker
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.synthetic import SyntheticAdapter
from trading.engine import Engine
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.strategies.mean_reversion import MeanReversion
from trading.types import Bar, Portfolio, Side


def test_registry_resolves_mean_reversion() -> None:
    assert isinstance(get_strategy("mean_reversion"), MeanReversion)


def _bar(day: int, close: float) -> Bar:
    ts = datetime(2024, 1, day, tzinfo=UTC)
    return Bar("AAA", ts, open=close, high=close, low=close, close=close, volume=1_000)


def test_mean_reversion_buys_oversold_and_exits_on_recovery() -> None:
    # period 3: falling into day4 drives RSI to 0 (< oversold 30) -> long signal;
    # the day6 rebound lifts RSI to 62.5 (> exit 50) -> flat signal. Fills next bar.
    closes = {1: 100, 2: 90, 3: 80, 4: 70, 5: 65, 6: 90, 7: 90, 8: 90}
    adapter = FakeAdapter([_bar(day, c) for day, c in closes.items()])
    broker = SimulatedBroker(
        Portfolio(cash=1_000.0), CostConfig(commission_per_share=0, slippage_bps=0)
    )

    result = Engine(adapter, broker, Guardrails(RiskConfig.unlimited())).run(
        MeanReversion(period=3, oversold=30.0, exit_level=50.0, weight=0.9),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 8, tzinfo=UTC),
    )

    trades = [(ts.day, fill.side) for ts, fill in result.fills]
    # Entry signal day 4 -> fills day 5; exit signal day 6 -> fills day 7.
    assert trades == [(5, Side.BUY), (7, Side.SELL)]
    assert result.final_portfolio.positions == {}


def test_mean_reversion_runs_on_synthetic_data_with_a_sane_curve() -> None:
    adapter = SyntheticAdapter(seed=11)
    broker = SimulatedBroker(Portfolio(cash=1_000.0))

    result = Engine(adapter, broker, Guardrails(RiskConfig.unlimited())).run(
        MeanReversion(period=14, weight=0.9),
        ["AAA", "BBB", "CCC"],
        datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2021, 12, 31, tzinfo=UTC),
    )

    assert 250 <= len(result.equity_curve) <= 262
    assert result.final_equity > 0
    assert all(p.equity > 0 for p in result.equity_curve)
