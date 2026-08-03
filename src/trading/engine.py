"""The event-driven engine, shared by backtest and paper trading (ADR-0001, ADR-0002).

One loop advances through a merged multi-symbol feed a timestamp at a time. The
invariant that makes results trustworthy: on each bar we first *execute* orders
queued on the previous bar (against this bar's open), then show the strategy the
now-complete bar, then queue whatever it decides — so an order can never fill on
the bar that produced it (no look-ahead). Equity is marked to each bar's close.

Backtest and paper trading share this exact per-bar step (:meth:`Engine._step`);
only the feed and the clock differ (ADR-0002, ADR-0014):

* **Backtest** (:meth:`Engine.run`) builds the whole feed from the adapter over
  ``[start, end]`` and iterates it immediately.
* **Paper** (:class:`PaperSession`) polls a completed-bar feed on a wall clock,
  processing each newly completed bar exactly once, then sleeping until the next
  bar is due.

Both drive the *same* ``_step`` over the *same* broker, guardrails, and sizing, so
the two modes cannot drift.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from trading.config import RiskConfig
from trading.interfaces import Broker, DataAdapter, RiskGuardrails, Strategy
from trading.risk import Guardrails
from trading.sizing import size
from trading.types import SHARE_EPS, Bar, Fill, Order, Portfolio, TargetWeight

if TYPE_CHECKING:
    from trading.clock import Clock

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


@runtime_checkable
class CompletedBarFeed(Protocol):
    """A source of *completed* recent bars, polled repeatedly in paper mode.

    The one real implementation is
    :class:`~trading.data.recent_window.RecentWindowFeed`; tests substitute a fake
    with the same ``poll`` shape. Declared here (rather than importing the concrete
    feed) so the engine never depends back on ``data.recent_window`` — that module
    imports :func:`build_feed`.
    """

    def poll(self, symbols: list[str], lookback: int) -> Feed:
        """Return the completed cross-section available now, ascending by time."""
        ...


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Portfolio equity — and its gross exposure — marked at one bar's close.

    ``exposure`` is Σ|position value| / equity at the close (0.0 when the book is
    flat or equity is non-positive), the per-bar series the V4 metrics average and
    peak. It defaults to 0.0 so pre-V4 constructions stay valid.
    """

    ts: datetime
    equity: float
    exposure: float = 0.0


@dataclass(frozen=True, slots=True)
class BarOutcome:
    """What one :meth:`Engine._step` did — the paper driver's per-bar record.

    A backtest ignores this (it reads the accumulated result); paper mode logs and
    prints it. It carries only *this bar's* events, not the running totals.
    """

    ts: datetime
    fills: list[Fill]
    intents: list[Order | TargetWeight]
    submitted: list[Order]
    clamps: list[tuple[Order, Order, str]]
    guardrail_rejections: list[tuple[Order, str]]
    broker_rejections: list[tuple[Order, str]]
    halted_now: bool
    halted: bool
    equity: float
    exposure: float


@dataclass(slots=True)
class BacktestResult:
    """The outcome of a run: the equity curve plus final state (ADR for metrics: V4)."""

    symbols: list[str]
    starting_cash: float
    equity_curve: list[EquityPoint]
    final_portfolio: Portfolio
    fills: list[tuple[datetime, Fill]] = field(default_factory=list)
    # Guardrail rejections (halt block / cap collapse) merged with the broker's.
    rejections: list[tuple[Order, str]] = field(default_factory=list)
    # (original, clamped, reason) for each order a guardrail cap trimmed down.
    clamps: list[tuple[Order, Order, str]] = field(default_factory=list)
    halted: bool = False
    halt_ts: datetime | None = None
    halt_reason: str | None = None

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1].equity if self.equity_curve else self.starting_cash

    @property
    def total_return(self) -> float:
        return self.final_equity / self.starting_cash - 1.0


@dataclass(slots=True)
class _RunState:
    """Mutable per-run accumulator threaded through :meth:`Engine._step`.

    Holds exactly the state the old ``Engine.run`` kept in locals, so extracting
    the loop body into a shared step leaves backtest behavior byte-identical.
    """

    starting_cash: float
    history: dict[str, list[Bar]] = field(default_factory=lambda: defaultdict(list))
    last_close: dict[str, float] = field(default_factory=dict)
    curve: list[EquityPoint] = field(default_factory=list)
    blotter: list[tuple[datetime, Fill]] = field(default_factory=list)
    # Guardrail rejections only; the broker's are merged in at finalize (as before).
    rejections: list[tuple[Order, str]] = field(default_factory=list)
    clamps: list[tuple[Order, Order, str]] = field(default_factory=list)
    halt_ts: datetime | None = None


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

    def __init__(
        self,
        adapter: DataAdapter,
        broker: Broker,
        guardrails: RiskGuardrails | None = None,
    ) -> None:
        self._adapter = adapter
        self._broker = broker
        # Enforced by default (ADR-0009); pass Guardrails(RiskConfig.unlimited())
        # to opt out.
        self._guardrails: RiskGuardrails = guardrails or Guardrails(RiskConfig())

    def run(
        self,
        strategy: Strategy,
        symbols: list[str],
        start: datetime,
        end: datetime,
    ) -> BacktestResult:
        """Backtest ``strategy`` over ``[start, end]`` — build the feed and iterate."""
        series = {s: self._adapter.get_bars(s, start, end) for s in symbols}
        feed = build_feed(series)

        state = _RunState(starting_cash=self._broker.portfolio.cash)
        for ts, bars in feed:
            self._step(strategy, ts, bars, state)
        return self._finalize(symbols, state)

    def _step(
        self,
        strategy: Strategy,
        ts: datetime,
        bars: BarSlice,
        state: _RunState,
    ) -> BarOutcome:
        """Process one bar. THE shared execution path for backtest and paper.

        The order of operations here *is* the no-look-ahead invariant (ADR-0001):
        execute the previous bar's queued orders at this open, then reveal the now
        complete bar, run the kill switch, size intents, pre-trade check, submit
        survivors (they fill no earlier than the next bar), and finally mark equity
        and exposure to this close. It mutates ``state`` and returns a per-bar
        :class:`BarOutcome`; a backtest discards the return, paper logs it.
        """
        # 1. Execute orders queued on the previous bar at this bar's open.
        broker_rej_before = len(getattr(self._broker, "rejections", []))
        fills = self._broker.on_bar(bars)
        state.blotter.extend((ts, fill) for fill in fills)
        broker_rejections = list(getattr(self._broker, "rejections", [])[broker_rej_before:])

        # 2. Reveal the now-complete bar to the strategy (never the future).
        for symbol, bar in bars.items():
            state.history[symbol].append(bar)
            state.last_close[symbol] = bar.close

        # 3. Portfolio monitor: update/latch the kill switch on the marked book.
        halted_now = False
        halted = self._guardrails.halted(self._broker.portfolio, state.last_close)
        if halted and state.halt_ts is None:
            state.halt_ts = ts
            halted_now = True

        context = _Context(self._broker.portfolio, state.history)
        intents = strategy.on_bar(ts, bars, context)

        # 4. Size intents into orders, run each through the pre-trade check,
        #    then queue survivors (they fill no earlier than the next bar).
        submitted: list[Order] = []
        bar_clamps: list[tuple[Order, Order, str]] = []
        bar_rejections: list[tuple[Order, str]] = []
        for order in size(intents, self._broker.portfolio, state.last_close):
            checked = self._guardrails.check(order, self._broker.portfolio, state.last_close)
            reason = getattr(self._guardrails, "last_reason", None)
            if checked is None:
                rejection = (order, reason or "rejected by guardrails")
                state.rejections.append(rejection)
                bar_rejections.append(rejection)
                continue
            if abs(checked.qty - order.qty) > SHARE_EPS:
                clamp = (order, checked, reason or "clamped by guardrails")
                state.clamps.append(clamp)
                bar_clamps.append(clamp)
            self._broker.submit(checked)
            submitted.append(checked)

        # 5. Mark equity — and gross exposure — to this bar's close.
        portfolio = self._broker.portfolio
        equity = portfolio.equity(state.last_close)
        # gross_exposure raises on non-positive equity; a flat book is 0.
        if equity <= 0 or not portfolio.positions:
            exposure = 0.0
        else:
            exposure = portfolio.gross_exposure(state.last_close)
        state.curve.append(EquityPoint(ts, equity, exposure))

        return BarOutcome(
            ts=ts,
            fills=fills,
            intents=intents,
            submitted=submitted,
            clamps=bar_clamps,
            guardrail_rejections=bar_rejections,
            broker_rejections=broker_rejections,
            halted_now=halted_now,
            halted=state.halt_ts is not None,
            equity=equity,
            exposure=exposure,
        )

    def _finalize(self, symbols: list[str], state: _RunState) -> BacktestResult:
        """Assemble a :class:`BacktestResult` from the accumulated run state.

        The broker's own rejections (underfunded buys, oversells) are merged in
        here, after the guardrail rejections gathered per bar — exactly the order
        the pre-refactor ``run`` produced, so results stay byte-identical.
        """
        rejections = list(state.rejections)
        rejections.extend(getattr(self._broker, "rejections", []))
        return BacktestResult(
            symbols=list(symbols),
            starting_cash=state.starting_cash,
            equity_curve=state.curve,
            final_portfolio=self._broker.portfolio,
            fills=state.blotter,
            rejections=rejections,
            clamps=state.clamps,
            halted=state.halt_ts is not None,
            halt_ts=state.halt_ts,
            halt_reason=getattr(self._guardrails, "halt_reason", None),
        )


# The paper driver's default: how many recent completed bars to request each poll.
# Large enough to replay a full run when the feed reveals everything at once, and
# to cover any strategy's history window; the engine accumulates history across
# polls, so this only bounds how far back a single poll looks.
DEFAULT_PAPER_LOOKBACK = 512


class PaperSession:
    """Drives the shared per-bar step over a completed-bar feed on a clock (ADR-0014).

    Reuses an :class:`Engine` — its broker, guardrails, and sizing — and calls the
    *same* :meth:`Engine._step`, so paper trading and backtest cannot drift
    (ADR-0002). Each iteration polls the feed for completed bars, processes any
    timestamp not seen before *exactly once* (idempotent — a re-polled bar is never
    reprocessed), records a :class:`BarOutcome`, then sleeps until the next bar is
    due. Time comes from the injected :class:`~trading.clock.Clock`: a
    ``WallClock`` for real paper trading, a ``FakeClock`` in tests (no real wait).

    The loop is bounded for tests and offline demos: it stops once ``max_new_bars``
    have been processed, or after ``max_empty_polls`` consecutive polls that reveal
    nothing new, whichever comes first (with ``max_polls`` as a hard safety).
    """

    def __init__(
        self,
        engine: Engine,
        strategy: Strategy,
        symbols: list[str],
        feed: CompletedBarFeed,
        clock: Clock,
        *,
        lookback: int = DEFAULT_PAPER_LOOKBACK,
        poll_interval: timedelta = timedelta(days=1),
    ) -> None:
        self._engine = engine
        self._strategy = strategy
        self._symbols = list(symbols)
        self._feed = feed
        self._clock = clock
        self._lookback = lookback
        self._poll_interval = poll_interval
        self._state = _RunState(starting_cash=engine._broker.portfolio.cash)
        self._seen: set[datetime] = set()
        # The session log: one BarOutcome per completed bar processed, in order.
        self.session_log: list[BarOutcome] = []

    @property
    def state(self) -> _RunState:
        return self._state

    def _next_due(self) -> datetime:
        """The next poll instant: one ``poll_interval`` past the current UTC day.

        Anchored to the start of the day so a daily cadence wakes just after each
        session rolls over — the moment the previous day's bar becomes complete.
        """
        now = self._clock.now().astimezone(UTC)
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        return day_start + self._poll_interval

    def run(
        self,
        *,
        max_new_bars: int | None = None,
        max_empty_polls: int = 2,
        max_polls: int = 100_000,
        reporter: object = None,
    ) -> BacktestResult:
        """Poll → process new completed bars → sleep, until a stop condition.

        ``reporter``, if callable, is invoked ``reporter(outcome)`` for each newly
        processed bar (the CLI uses it to print status and persist state). Returns
        the final :class:`BacktestResult`, assembled from the shared run state so it
        is identical in shape to a backtest's.
        """
        call_reporter = reporter if callable(reporter) else None
        empty_polls = 0

        for _ in range(max_polls):
            feed = self._feed.poll(self._symbols, self._lookback)
            fresh = [(ts, bars) for ts, bars in feed if ts not in self._seen]

            for ts, bars in fresh:
                outcome = self._engine._step(self._strategy, ts, bars, self._state)
                self._seen.add(ts)
                self.session_log.append(outcome)
                if call_reporter is not None:
                    call_reporter(outcome)
                if max_new_bars is not None and len(self.session_log) >= max_new_bars:
                    return self._engine._finalize(self._symbols, self._state)

            if fresh:
                empty_polls = 0
            else:
                empty_polls += 1
                if empty_polls >= max_empty_polls:
                    break

            self._clock.sleep_until(self._next_due())

        return self._engine._finalize(self._symbols, self._state)
