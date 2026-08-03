"""Momentum enters on a positive trailing return and exits when it turns down.

A crafted single-symbol series drives the strategy through one entry and one exit
via the engine's fill blotter (mirroring test_sma_crossover), plus a smoke run on
synthetic data to prove it completes with a sane equity curve.
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
from trading.strategies.momentum import Momentum
from trading.types import Bar, Portfolio, Side


def test_registry_resolves_momentum() -> None:
    assert isinstance(get_strategy("momentum"), Momentum)


def _bar(day: int, close: float) -> Bar:
    ts = datetime(2024, 1, day, tzinfo=UTC)
    return Bar("AAA", ts, open=close, high=close, low=close, close=close, volume=1_000)


def test_momentum_enters_on_positive_return_and_exits_when_it_turns_down() -> None:
    # lookback 3: at day5 close(12) > close 3 bars back(9) -> long signal;
    # at day7 close(6) < close 3 bars back(7) -> flat signal. Orders fill next bar.
    closes = {1: 10, 2: 9, 3: 8, 4: 7, 5: 12, 6: 13, 7: 6, 8: 6, 9: 6}
    adapter = FakeAdapter([_bar(day, c) for day, c in closes.items()])
    broker = SimulatedBroker(
        Portfolio(cash=1_000.0), CostConfig(commission_per_share=0, slippage_bps=0)
    )

    result = Engine(adapter, broker, Guardrails(RiskConfig.unlimited())).run(
        Momentum(lookback=3, weight=0.9),
        ["AAA"],
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 9, tzinfo=UTC),
    )

    trades = [(ts.day, fill.side) for ts, fill in result.fills]
    # Entry signal day 5 -> fills day 6; exit signal day 7 -> fills day 8.
    assert trades == [(6, Side.BUY), (8, Side.SELL)]
    assert result.final_portfolio.positions == {}


def test_momentum_runs_on_synthetic_data_with_a_sane_curve() -> None:
    adapter = SyntheticAdapter(seed=7)
    broker = SimulatedBroker(Portfolio(cash=1_000.0))

    result = Engine(adapter, broker, Guardrails(RiskConfig.unlimited())).run(
        Momentum(lookback=20, weight=0.9),
        ["AAA", "BBB", "CCC"],
        datetime(2021, 1, 1, tzinfo=UTC),
        datetime(2021, 12, 31, tzinfo=UTC),
    )

    assert 250 <= len(result.equity_curve) <= 262
    assert result.final_equity > 0
    assert all(p.equity > 0 for p in result.equity_curve)
    assert result.fills, "expected momentum to trade at least once on synthetic data"
