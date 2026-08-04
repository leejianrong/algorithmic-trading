"""Load-bearing guard for the per-mode price policy (ADR-0021).

The claim under test: the paper path decides and marks on RAW quotes while a
backtest of the *same* bars uses ADJUSTED prices. We drive both modes over one
adapter whose raw and adjusted closes deliberately differ, then assert each mode
saw — and marked equity against — its own price notion. These tests import
``Engine``/``PaperSession`` and ``FakeClock`` but never modify them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.broker import SimulatedBroker
from trading.clock import FakeClock
from trading.config import CostConfig, RiskConfig
from trading.data.recent_window import RecentWindowFeed
from trading.engine import Engine, PaperSession
from trading.interfaces import StrategyContext
from trading.risk import Guardrails
from trading.strategies.buy_and_hold import BuyAndHold
from trading.types import Bar, Order, Portfolio, TargetWeight

_SYM = "SYM"
_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 1, 31, tzinfo=UTC)
# A clock well past the last bar, so every bar counts as completed for the feed.
_LATER = datetime(2024, 1, 10, tzinfo=UTC)


def _bar(day: int, price: float) -> Bar:
    ts = _START + timedelta(days=day)
    return Bar(_SYM, ts, open=price, high=price, low=price, close=price, volume=1_000)


def _series(prices: list[float]) -> list[Bar]:
    return [_bar(i, p) for i, p in enumerate(prices)]


class _DualAdapter:
    """A DataAdapter that returns DIFFERENT closes for raw vs adjusted requests.

    This is the crux of the fixture: a mode's choice of price notion becomes
    observable because the two series diverge.
    """

    def __init__(self, adjusted_closes: list[float], raw_closes: list[float]) -> None:
        self._adjusted = _series(adjusted_closes)
        self._raw = _series(raw_closes)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        source = self._adjusted if adjusted else self._raw
        return [b for b in source if b.symbol == symbol and start <= b.ts <= end]


class _RecordingStrategy:
    """Records the close it is shown each bar; never trades."""

    def __init__(self) -> None:
        self.seen: list[float] = []

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        self.seen.append(bars[_SYM].close)
        return []


def _run_backtest(adapter: _DualAdapter, strategy: object) -> object:
    broker = SimulatedBroker(Portfolio(cash=1_000.0), CostConfig(slippage_bps=0.0))
    engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
    return engine.run(strategy, [_SYM], _START, _END)  # type: ignore[arg-type]


def _run_paper(adapter: _DualAdapter, strategy: object) -> object:
    broker = SimulatedBroker(Portfolio(cash=1_000.0), CostConfig(slippage_bps=0.0))
    engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
    clock = FakeClock(_LATER)
    feed = RecentWindowFeed(adapter, clock)  # defaults to RAW (ADR-0021)
    session = PaperSession(engine, strategy, [_SYM], feed, clock)  # type: ignore[arg-type]
    return session.run()


def test_paper_decides_on_raw_while_backtest_decides_on_adjusted() -> None:
    # Fully distinct series so "which did the strategy see?" is unambiguous.
    adapter = _DualAdapter(
        adjusted_closes=[10.0, 11.0, 12.0, 13.0],
        raw_closes=[100.0, 110.0, 120.0, 130.0],
    )

    bt_strategy = _RecordingStrategy()
    _run_backtest(adapter, bt_strategy)
    paper_strategy = _RecordingStrategy()
    _run_paper(adapter, paper_strategy)

    assert bt_strategy.seen == [10.0, 11.0, 12.0, 13.0]  # backtest → adjusted
    assert paper_strategy.seen == [100.0, 110.0, 120.0, 130.0]  # paper → raw


def test_paper_equity_marks_raw_while_backtest_marks_adjusted() -> None:
    # Bars 0 and 1 agree (so the initial buy fills at the same price and qty in
    # both modes); bars 2 and 3 diverge, so only the marking price notion differs.
    adjusted_closes = [10.0, 10.0, 10.0, 10.0]  # flat → backtest equity flat
    raw_closes = [10.0, 10.0, 30.0, 40.0]  # rising → paper equity rises
    adapter = _DualAdapter(adjusted_closes, raw_closes)

    backtest = _run_backtest(adapter, BuyAndHold())
    paper = _run_paper(adapter, BuyAndHold())

    # Same fills → same held quantity and residual cash in both modes.
    qty = backtest.final_portfolio.position(_SYM).qty  # type: ignore[attr-defined]
    cash = backtest.final_portfolio.cash  # type: ignore[attr-defined]
    assert qty > 0
    assert paper.final_portfolio.position(_SYM).qty == pytest.approx(qty)  # type: ignore[attr-defined]

    # Backtest marks the final bar at the ADJUSTED close (10); paper at RAW (40).
    assert backtest.final_equity == pytest.approx(cash + qty * 10.0)  # type: ignore[attr-defined]
    assert paper.final_equity == pytest.approx(cash + qty * 40.0)  # type: ignore[attr-defined]
    assert paper.final_equity > backtest.final_equity + 100.0  # type: ignore[attr-defined]
