"""The event-driven backtest engine (ADR-0001, ADR-0002).

One loop advances through the merged multi-symbol feed a timestamp at a time.
The invariant that makes results trustworthy: on each bar we first *execute*
orders queued on the previous bar (against this bar's open), then show the
strategy the now-complete bar, then queue whatever it decides — so an order can
never fill on the bar that produced it (no look-ahead). Equity is marked to each
bar's close.

Backtest and paper trading will share this exact loop; only the feed and the
clock differ (ADR-0002). The paper clock lands in V5.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from trading.interfaces import Broker, DataAdapter, Strategy
from trading.sizing import size
from trading.types import Bar, Fill, Order, Portfolio

# A single timestamp's bars across the universe, and the whole ordered feed.
BarSlice = dict[str, Bar]
Feed = list[tuple[datetime, BarSlice]]


def build_feed(series: dict[str, list[Bar]]) -> Feed:
    """Merge per-symbol bar lists into one timestamp-ordered cross-section.

    Symbols missing a bar on a given day simply don't appear in that day's slice
    (ADR-0006 time-alignment), so a holiday or a late listing is handled without
    inventing prices.
    """
    by_ts: dict[datetime, BarSlice] = defaultdict(dict)
    for symbol, bars in series.items():
        for bar in bars:
            by_ts[bar.ts][symbol] = bar
    return [(ts, by_ts[ts]) for ts in sorted(by_ts)]


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Portfolio equity marked at one bar's close."""

    ts: datetime
    equity: float


@dataclass(slots=True)
class BacktestResult:
    """The outcome of a run: the equity curve plus final state (ADR for metrics: V4)."""

    symbols: list[str]
    starting_cash: float
    equity_curve: list[EquityPoint]
    final_portfolio: Portfolio
    fills: list[tuple[datetime, Fill]] = field(default_factory=list)
    rejections: list[tuple[Order, str]] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1].equity if self.equity_curve else self.starting_cash

    @property
    def total_return(self) -> float:
        return self.final_equity / self.starting_cash - 1.0


class _Context:
    """Concrete StrategyContext: the portfolio and history seen *so far*."""

    def __init__(self, portfolio: Portfolio, history: dict[str, list[Bar]]) -> None:
        self._portfolio = portfolio
        self._history = history

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    def history(self, symbol: str, lookback: int) -> list[Bar]:
        bars = self._history.get(symbol, [])
        return bars[-lookback:]


class Engine:
    """Runs a strategy over a data feed through a broker."""

    def __init__(self, adapter: DataAdapter, broker: Broker) -> None:
        self._adapter = adapter
        self._broker = broker

    def run(
        self,
        strategy: Strategy,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> BacktestResult:
        series = {s: self._adapter.get_bars(s, start, end) for s in symbols}
        feed = build_feed(series)

        starting_cash = self._broker.portfolio.cash
        history: dict[str, list[Bar]] = defaultdict(list)
        last_close: dict[str, float] = {}
        curve: list[EquityPoint] = []
        blotter: list[tuple[datetime, Fill]] = []

        for ts, bars in feed:
            # 1. Execute orders queued on the previous bar at this bar's open.
            fills = self._broker.on_bar(bars)
            blotter.extend((ts, fill) for fill in fills)

            # 2. Reveal the now-complete bar to the strategy (never the future).
            for symbol, bar in bars.items():
                history[symbol].append(bar)
                last_close[symbol] = bar.close
            context = _Context(self._broker.portfolio, history)
            intents = strategy.on_bar(ts, bars, context)

            # 3. Size intents into orders; they can fill no earlier than next bar.
            for order in size(intents, self._broker.portfolio, last_close):
                self._broker.submit(order)

            # 4. Mark equity to this bar's close.
            equity = self._broker.portfolio.equity(last_close)
            curve.append(EquityPoint(ts, equity))

        return BacktestResult(
            symbols=list(symbols),
            starting_cash=starting_cash,
            equity_curve=curve,
            final_portfolio=self._broker.portfolio,
            fills=blotter,
            rejections=list(getattr(self._broker, "rejections", [])),
        )
