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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from trading.config import RiskConfig
from trading.frequency import Frequency
from trading.interfaces import Broker, DataAdapter, RiskGuardrails, Strategy
from trading.risk import Guardrails
from trading.sizing import size
from trading.types import SHARE_EPS, Bar, Fill, Order, Portfolio, TargetWeight

if TYPE_CHECKING:
    from collections.abc import Sequence

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


# Why a requested symbol contributed nothing to a run (ADR-0032). Plain strings so
# they survive a round trip through a CSV/JSON report unchanged, matching the
# reason codes in ``trading.universe``.
REASON_NO_BARS = "no_bars_in_range"
REASON_FETCH_FAILED = "fetch_failed"

ABSENT_REASONS: frozenset[str] = frozenset({REASON_NO_BARS, REASON_FETCH_FAILED})


class EmptyUniverseError(Exception):
    """Not one requested symbol yielded a bar, so there is nothing to backtest.

    Raised instead of returning a vacuous zero-return result. A universe where
    *every* symbol is absent is a mistyped ticker list, a wrong date range, or a
    broken data source — never a legitimate run (ADR-0032). Partial absence is
    tolerated and reported; total absence is an error.
    """


@dataclass(frozen=True, slots=True)
class AbsentSymbol:
    """A requested symbol that contributed no bars, and why (ADR-0032).

    ``reason`` is one of :data:`ABSENT_REASONS` (machine-readable); ``detail`` is
    the human sentence a report prints. Every excluded symbol produces one of
    these — a universe is never silently shrunk, because a silently shrunk
    universe is indistinguishable from a typo in ``--symbols``.

    :data:`REASON_NO_BARS` and :data:`REASON_FETCH_FAILED` are kept apart on
    purpose: "this symbol had not listed yet" and "we could not ask" are different
    facts, and only the second suggests something is broken.
    """

    symbol: str
    reason: str
    detail: str

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("AbsentSymbol.symbol must be a non-empty ticker")
        if self.reason not in ABSENT_REASONS:
            known = ", ".join(sorted(ABSENT_REASONS))
            raise ValueError(f"unknown absent reason {self.reason!r}; known reasons: {known}")
        if not self.detail:
            raise ValueError(f"AbsentSymbol.detail must explain why {self.symbol} was absent")


def load_series(
    adapter: DataAdapter,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
) -> tuple[dict[str, list[Bar]], list[AbsentSymbol]]:
    """Fetch each symbol's bars, tolerating and *reporting* the ones with none.

    Returns ``(series, absent)`` where ``series`` holds only the symbols that
    actually produced bars, and ``absent`` records every one that did not. A
    symbol whose lookup raises is caught per symbol, so one bad ticker never
    aborts a whole universe — the same treatment
    :func:`trading.liquidity.screen_by_adv` and
    :func:`trading.universe.validate_universe` already give their inputs.

    This exists because a multi-decade backtest over a real universe *must*
    tolerate members that do not span the whole range (ADR-0032): a 2000-2020 run
    of today's mega-caps has no META before 2012, and a fetch for an earlier
    walk-forward fold legitimately returns nothing. Before this, one such symbol
    raised and killed the entire sweep.

    Duplicate symbols are collapsed to their first occurrence (one fetch each) and
    input order is preserved, so the result is deterministic.

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is never caught.
    """
    series: dict[str, list[Bar]] = {}
    absent: list[AbsentSymbol] = []
    seen: set[str] = set()

    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            bars = adapter.get_bars(symbol, start, end)
        except Exception as exc:  # one bad symbol must never abort the whole universe
            absent.append(
                AbsentSymbol(
                    symbol=symbol,
                    reason=REASON_FETCH_FAILED,
                    detail=f"data lookup failed ({type(exc).__name__}: {exc})",
                )
            )
            continue
        if not bars:
            absent.append(
                AbsentSymbol(
                    symbol=symbol,
                    reason=REASON_NO_BARS,
                    detail=(
                        f"no bars in {start.date()}..{end.date()} — "
                        "not listed in this window, or the source has no history"
                    ),
                )
            )
            continue
        series[symbol] = bars

    return series, absent


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
class HaltEpisode:
    """One stretch of the kill switch being in force (ADR-0031).

    ``halt_ts`` is the bar the switch tripped on; ``resume_ts`` is the bar it
    re-armed on, or ``None`` when the halt was still in force at the end of the run
    (always the case with the default, permanently-latching config). ``reason`` is
    the guardrails' explanation captured when the episode opened, so a run with two
    halts keeps both reasons instead of only the last.
    """

    halt_ts: datetime
    reason: str
    resume_ts: datetime | None = None


@dataclass(frozen=True, slots=True)
class BarOutcome:
    """What one :meth:`Engine._step` did — the paper driver's per-bar record.

    A backtest ignores this (it reads the accumulated result); paper mode logs and
    prints it. It carries only *this bar's* events, not the running totals — so
    ``halted`` is the kill switch's state *as of this bar*, ``halted_now`` marks the
    bar it tripped on, and ``resumed_now`` the bar an opt-in recovery re-armed it
    (ADR-0031). Under the default permanent latch, ``halted`` is ``True`` from the
    trip onward and ``resumed_now`` never fires.
    """

    ts: datetime
    fills: list[Fill]
    intents: list[Order | TargetWeight]
    # Orders the broker actually accepted this bar, at the quantity it was handed
    # (post-clamp). An order the broker refused at submit is *not* here — it is in
    # ``broker_rejections`` instead (ADR-0044).
    submitted: list[Order]
    clamps: list[tuple[Order, Order, str]]
    guardrail_rejections: list[tuple[Order, str]]
    # Everything the broker rejected on this bar, in the order it happened:
    # settlement rejections first (last bar's orders, ended by the venue), then
    # submit-time refusals (the duplicate guard, ADR-0036; a venue veto, ADR-0041).
    # Reporting only — ``Engine._finalize`` merges the broker's own list into
    # ``BacktestResult.rejections``, so these are copies, never a second tally.
    broker_rejections: list[tuple[Order, str]]
    halted_now: bool
    halted: bool
    equity: float
    exposure: float
    # True on the bar an opt-in recovery re-armed the kill switch (ADR-0031);
    # always False with the default permanently-latching config.
    resumed_now: bool = False


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
    # ``halted`` means "a halt occurred during this run" (not "is halted now"), and
    # halt_ts/halt_reason describe the *first* one — unchanged from V3, so the
    # report, result.json, and dashboard read exactly what they always did.
    halted: bool = False
    halt_ts: datetime | None = None
    halt_reason: str | None = None
    # Every halt stretch, in order (ADR-0031). Empty when the switch never tripped;
    # exactly one open-ended episode under the default permanent latch.
    halt_episodes: list[HaltEpisode] = field(default_factory=list)
    # Requested symbols that contributed no bars, with the reason each (ADR-0032).
    # ``symbols`` stays the *requested* universe so existing reports are unchanged;
    # :attr:`traded_symbols` is the set that actually had data.
    absent: list[AbsentSymbol] = field(default_factory=list)

    @property
    def traded_symbols(self) -> list[str]:
        """Requested symbols that actually contributed bars, in request order.

        ``symbols`` is what the caller asked for; this is what the run could
        actually see. They differ whenever :attr:`absent` is non-empty, and a
        report that quotes only the former overstates the universe.
        """
        missing = {a.symbol for a in self.absent}
        return [s for s in self.symbols if s not in missing]

    @property
    def halt_episode_count(self) -> int:
        """How many times the kill switch tripped during the run (ADR-0031)."""
        return len(self.halt_episodes)

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
    # Previous bar's latch state, so a step can spot the transitions that open and
    # close halt episodes (ADR-0031).
    halted: bool = False
    halt_episodes: list[HaltEpisode] = field(default_factory=list)


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
        """Backtest ``strategy`` over ``[start, end]`` — build the feed and iterate.

        Symbols with no bars in the range are excluded and reported on
        :attr:`BacktestResult.absent` rather than aborting the run (ADR-0032); if
        *every* symbol is absent, :class:`EmptyUniverseError` is raised instead of
        returning a vacuous result.
        """
        series, absent = load_series(self._adapter, symbols, start, end)
        if not series:
            detail = "; ".join(f"{a.symbol}: {a.detail}" for a in absent)
            raise EmptyUniverseError(
                f"no bars for any of {len(absent)} requested symbol(s) in "
                f"{start.date()}..{end.date()} — {detail}"
            )
        feed = build_feed(series)

        state = _RunState(starting_cash=self._broker.portfolio.cash)
        for ts, bars in feed:
            self._step(strategy, ts, bars, state)
        return self._finalize(symbols, state, absent=absent)

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

        The broker's rejections are diffed twice — around ``on_bar`` and around each
        ``submit`` — because a broker can say no at either end of an order's life
        and the per-bar record is the only real-time view a live session has
        (ADR-0044).
        """
        # 1. Execute orders queued on the previous bar at this bar's open.
        #    A broker rejects at two distinct moments and this bar owns both: at
        #    *settlement* here (an underfunded buy, an order the venue ended —
        #    ADR-0033) and at *submit* in step 4 below. ``rejections`` is read
        #    through ``getattr`` because the ``Broker`` protocol does not require it
        #    (KAN-670); both brokers that have it append ``(Order, reason)``.
        broker_rej_before = len(getattr(self._broker, "rejections", []))
        fills = self._broker.on_bar(bars)
        state.blotter.extend((ts, fill) for fill in fills)
        broker_rejections = list(getattr(self._broker, "rejections", [])[broker_rej_before:])

        # 2. Reveal the now-complete bar to the strategy (never the future).
        for symbol, bar in bars.items():
            state.history[symbol].append(bar)
            state.last_close[symbol] = bar.close

        # 3. Portfolio monitor: update/latch the kill switch on the marked book.
        #    The latch transitions are the halt *episodes* (ADR-0031): the guardrails
        #    own the state machine, the engine owns the timestamps. Under the default
        #    permanent latch there is exactly one False->True transition, so
        #    halt_ts/halted are what V3 recorded.
        was_halted = state.halted
        halted = self._guardrails.halted(self._broker.portfolio, state.last_close)
        halted_now = halted and not was_halted
        resumed_now = was_halted and not halted
        if halted_now:
            if state.halt_ts is None:
                state.halt_ts = ts
            reason = getattr(self._guardrails, "halt_reason", None)
            state.halt_episodes.append(HaltEpisode(halt_ts=ts, reason=reason or "halted"))
        elif resumed_now and state.halt_episodes:
            state.halt_episodes[-1] = replace(state.halt_episodes[-1], resume_ts=ts)
        state.halted = halted

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
            # A broker may refuse an order at *submit* time — the duplicate-order
            # guard (ADR-0036) and the venue's own veto (ADR-0041) both record a
            # rejection here rather than raising. Diffing per order rather than per
            # bar is what keeps the two lists below honest: on a bar with one
            # refusal and two acceptances, only the refused order is missing from
            # ``submitted``, and only its reason is reported.
            submit_rej_before = len(getattr(self._broker, "rejections", []))
            self._broker.submit(checked)
            refused = list(getattr(self._broker, "rejections", [])[submit_rej_before:])
            if refused:
                # Reporting only. ``_finalize`` merges the broker's whole list into
                # BacktestResult.rejections exactly once, so appending here as well
                # would count every refusal twice.
                broker_rejections.extend(refused)
                continue
            # ``checked`` — never ``order`` — because the clamped quantity is what
            # the broker was actually handed.
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
            halted=halted,
            equity=equity,
            exposure=exposure,
            resumed_now=resumed_now,
        )

    def _finalize(
        self,
        symbols: list[str],
        state: _RunState,
        *,
        absent: list[AbsentSymbol] | None = None,
    ) -> BacktestResult:
        """Assemble a :class:`BacktestResult` from the accumulated run state.

        The broker's own rejections (underfunded buys, oversells) are merged in
        here, after the guardrail rejections gathered per bar — exactly the order
        the pre-refactor ``run`` produced, so results stay byte-identical.

        ``absent`` carries the symbols that yielded no bars (ADR-0032); it defaults
        to empty so the paper driver, which resolves its own universe from a live
        feed, is unaffected.
        """
        rejections = list(state.rejections)
        rejections.extend(getattr(self._broker, "rejections", []))
        # halt_ts/halt_reason describe the FIRST halt, so read the reason off the
        # first episode: the guardrails' own attribute holds the *latest* reason,
        # which would pair a later cause with the first timestamp once recovery lets
        # the switch trip twice (ADR-0031). Identical for a single halt.
        halt_reason = getattr(self._guardrails, "halt_reason", None)
        if state.halt_episodes:
            halt_reason = state.halt_episodes[0].reason
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
            halt_reason=halt_reason,
            halt_episodes=list(state.halt_episodes),
            absent=list(absent) if absent else [],
        )


# The paper driver's default: how many recent completed bars to request each poll.
# Large enough to replay a full run when the feed reveals everything at once, and
# to cover any strategy's history window; the engine accumulates history across
# polls, so this only bounds how far back a single poll looks.
DEFAULT_PAPER_LOOKBACK = 512

# How long a *live* paper session tolerates a feed that reveals nothing new before
# it stops, and the floor in polls under that duration (ADR-0049).
#
# ``PaperSession.run`` counts consecutive quiet *polls*, and a count cannot mean the
# same thing at two cadences: the historical default of 2 was ten minutes at
# ``--interval 5m`` and two days at ``1d``, so the same constant that stopped an
# intraday session on a brief data gap also killed a daily session over every
# weekend. The policy is therefore a duration, converted at the session's poll
# interval by :func:`silence_tolerance_polls`.
#
# The numbers are chosen for an asymmetric trade. Stopping late costs a handful of
# extra polls against a shut venue; stopping early costs the whole day's
# measurement, which is the only thing a live session exists to produce (ADR-0038).
# So they are generous toward the cheap error. The floor exists because at 30m and
# coarser the duration converts to fewer polls than the old default, which would
# make this change a regression at exactly the cadence the weekend bug lives at.
LIVE_SILENCE_TOLERANCE = timedelta(minutes=60)
MIN_LIVE_EMPTY_POLLS = 4


def silence_tolerance_polls(
    poll_interval: timedelta,
    *,
    tolerance: timedelta = LIVE_SILENCE_TOLERANCE,
    minimum: int = MIN_LIVE_EMPTY_POLLS,
) -> int:
    """How many consecutive quiet polls ``tolerance`` of silence is, at this cadence.

    Rounded *up*, so the session never tolerates less silence than asked for, and
    floored at ``minimum``. At the standard intervals: ``1m -> 60``, ``5m -> 12``,
    ``30m -> 4``, ``1h -> 4``, ``1d -> 4`` (four days, which clears a normal weekend
    and a three-day one).

    A free function rather than a method or a new default on
    :meth:`PaperSession.run`: the choice belongs where the live/replay distinction
    is made — the CLI — and changing the loop's own default would silently retune
    every existing caller. Nothing on the backtest path calls this;
    :meth:`Engine.run` has no empty-poll concept at all.
    """
    if poll_interval <= timedelta(0):  # defensive: never divide by a non-positive step
        return minimum
    polls = -(-tolerance // poll_interval)  # ceiling division on timedeltas
    return max(minimum, int(polls))


def prime_history(state: _RunState, feed: Feed) -> None:
    """Load ``feed``'s bars into ``state`` as *history only* — never as trades (ADR-0042).

    This is the warmup half of a live paper session. It does exactly the part of
    :meth:`Engine._step` that is pure bookkeeping — appending each bar to
    ``state.history`` and marking ``state.last_close`` — and **nothing** else: the
    strategy, the sizer, the guardrails and the broker are not invoked, and no
    :class:`EquityPoint` is recorded.

    Every one of those omissions is load-bearing:

    * **No strategy call.** Strategies are stateful and transition-driven —
      ``SmaCrossover`` and ``Momentum`` keep a per-symbol ``_long`` latch and emit
      only when it flips. Running one over history while discarding its orders would
      leave it believing it is long against a flat account, so the live session would
      see no transition and sit out the day in silence. Priming as data leaves the
      strategy pristine, and its first call lands on a genuinely live bar where it
      transitions from flat exactly once.
    * **No broker call.** The bars are already closed; an order sized from a
      historical open fills at today's price, which is the ±1,100 bps noise that
      swamped the fill-divergence sample (ADR-0038).
    * **No equity point.** The account held nothing during the warmup, so a curve
      over those timestamps is fabricated, and every metric derived from the curve —
      return, Sharpe, drawdown, exposure — would be computed over a book that did
      not exist.

    It is a free function, not a method, so the boundary can be tested directly and
    so nothing about it can be mistaken for a second execution path: ``_step``
    remains the only code that trades, in both modes (ADR-0002).
    """
    for _ts, bars in feed:
        for symbol, bar in bars.items():
            state.history[symbol].append(bar)
            state.last_close[symbol] = bar.close


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

    ``max_empty_polls`` is a count, so what it *means* depends on the poll cadence —
    the default of 2 is ten minutes at ``--interval 5m`` and two days at ``1d``. A
    real live session should pass a value derived from its interval rather than
    inherit that default; :func:`silence_tolerance_polls` computes it, and
    ``trading paper --live`` passes it (ADR-0049).

    **Warmup vs. live (ADR-0042).** A completed-bar feed hands back a window of
    *history* the moment a session opens — up to ``lookback`` bars that closed
    before anyone pressed start. With ``warmup=True`` (the default, and the only
    safe setting for a live session) those bars are loaded as history via
    :func:`prime_history` and nothing is traded on them; the strategy's first call
    is on the first bar to complete *after* the session opened. With
    ``warmup=False`` every bar the feed reveals is stepped and traded, which is
    what a bounded offline replay (``trading paper --once``) is for.
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
        poll_interval: timedelta | None = None,
        frequency: Frequency | None = None,
        warmup: bool = True,
    ) -> None:
        self._engine = engine
        self._strategy = strategy
        self._symbols = list(symbols)
        self._feed = feed
        self._clock = clock
        self._lookback = lookback
        # Cadence: an explicit ``poll_interval`` wins; otherwise the frequency's
        # bar length; otherwise the daily default — byte-compatible with V5, where
        # the daily default was ``timedelta(days=1)``.
        if poll_interval is None:
            poll_interval = frequency.delta if frequency is not None else timedelta(days=1)
        self._poll_interval = poll_interval
        self._state = _RunState(starting_cash=engine._broker.portfolio.cash)
        self._seen: set[datetime] = set()
        # The session log: one BarOutcome per completed bar processed, in order.
        self.session_log: list[BarOutcome] = []
        # Warmup bookkeeping (ADR-0042). ``_warmup_pending`` is the *boundary*: it
        # stays True until the first poll that reveals bars, so a failed opening
        # fetch (ADR-0035 returns an empty cross-section rather than raising) cannot
        # hand the backfill to the live path one poll later.
        self._warmup_pending = warmup
        self._warmup_span: tuple[datetime, datetime] | None = None
        self._warmup_bars = 0

    @property
    def state(self) -> _RunState:
        return self._state

    @property
    def warmup_bars(self) -> int:
        """How many bar timestamps were primed as history rather than traded.

        Zero before the warmup poll happens, and zero for the whole of a
        ``warmup=False`` replay.
        """
        return self._warmup_bars

    @property
    def warmup_span(self) -> tuple[datetime, datetime] | None:
        """``(first, last)`` timestamp of the primed window, or ``None`` if empty.

        The operator-facing evidence that a session started from real history: a
        live run that reports no span warmed up on nothing, and its first trades are
        being made by a strategy that cannot see its own lookback yet.
        """
        return self._warmup_span

    @property
    def warmup_complete(self) -> bool:
        """True once the warmup boundary has been crossed (or was never armed).

        From this point on every bar the feed reveals is live and tradeable — a bar
        arriving mid-session is never warmup.
        """
        return not self._warmup_pending

    def _next_due(self) -> datetime:
        """The next poll instant: the first ``poll_interval`` boundary after now.

        Boundaries are anchored to the start of the current UTC day and stepped by
        ``poll_interval``; the result is the first one *strictly after* ``now`` —
        the moment the bar now forming becomes complete. For the daily default
        (``poll_interval == timedelta(days=1)``) this is the start of the next day,
        exactly as V5 computed it. For a sub-daily interval it is the next
        intra-session boundary.
        """
        now = self._clock.now().astimezone(UTC)
        anchor = datetime(now.year, now.month, now.day, tzinfo=UTC)
        interval = self._poll_interval
        if interval <= timedelta(0):  # defensive: never loop on a non-positive step
            return now
        steps = (now - anchor) // interval + 1
        return anchor + steps * interval

    def finalize(self) -> BacktestResult:
        """Assemble a :class:`BacktestResult` from the bars processed *so far*.

        :meth:`run` returns this at every exit, but a ``--live`` session has no
        natural exit -- it runs until interrupted -- so the caller needs to be able
        to build the result itself after a ``KeyboardInterrupt``. Without that, the
        equity CSV and ``result.json`` are unreachable in live mode, since the only
        way out of the loop skips everything after it (ADR-0033).

        Safe to call at any point, including before the first bar.
        """
        return self._engine._finalize(self._symbols, self._state)

    def _absorb_warmup(self, fresh: Feed) -> None:
        """Take ``fresh`` as the session's opening history and close the boundary.

        Called at most once per session, on the first poll that reveals bars. The
        timestamps go into ``_seen`` so a later cumulative poll — ``RecentWindowFeed``
        re-returns its whole window every time — cannot resurrect them as live bars.
        """
        prime_history(self._state, fresh)
        self._seen.update(ts for ts, _ in fresh)
        self._warmup_bars = len(fresh)
        self._warmup_span = (fresh[0][0], fresh[-1][0])
        self._warmup_pending = False

    def run(
        self,
        *,
        max_new_bars: int | None = None,
        max_empty_polls: int = 2,
        max_polls: int = 100_000,
        reporter: object = None,
        on_warmup: object = None,
    ) -> BacktestResult:
        """Poll → process new completed bars → sleep, until a stop condition.

        ``reporter``, if callable, is invoked ``reporter(outcome)`` for each newly
        processed bar (the CLI uses it to print status and persist state). Returns
        the final :class:`BacktestResult`, assembled from the shared run state so it
        is identical in shape to a backtest's.

        With ``warmup=True`` the first poll that reveals any bars is the *warmup*:
        those bars are primed as history and none of them is traded, reported, or
        marked to a curve (ADR-0042). Every poll after it is live. ``on_warmup``, if
        callable, is invoked with no arguments the moment that happens — the session
        then sleeps until the next bar boundary, so without it a live run would say
        nothing at all for a whole interval after starting.
        """
        call_reporter = reporter if callable(reporter) else None
        call_on_warmup = on_warmup if callable(on_warmup) else None
        empty_polls = 0

        for _ in range(max_polls):
            feed = self._feed.poll(self._symbols, self._lookback)
            fresh = [(ts, bars) for ts, bars in feed if ts not in self._seen]

            if self._warmup_pending:
                if not fresh:
                    # Nothing revealed yet — the session has not really opened, so
                    # the warmup boundary stays where it is and this counts as the
                    # quiet poll it is.
                    empty_polls += 1
                    if empty_polls >= max_empty_polls:
                        break
                    self._clock.sleep_until(self._next_due())
                    continue
                self._absorb_warmup(fresh)
                if call_on_warmup is not None:
                    call_on_warmup()
                # A poll that primed hundreds of bars is the opposite of quiet;
                # counting it as empty would leave a default session one dull poll
                # from stopping before it ever traded.
                empty_polls = 0
                self._clock.sleep_until(self._next_due())
                continue

            for ts, bars in fresh:
                outcome = self._engine._step(self._strategy, ts, bars, self._state)
                self._seen.add(ts)
                self.session_log.append(outcome)
                if call_reporter is not None:
                    call_reporter(outcome)
                if max_new_bars is not None and len(self.session_log) >= max_new_bars:
                    return self.finalize()

            if fresh:
                empty_polls = 0
            else:
                empty_polls += 1
                if empty_polls >= max_empty_polls:
                    break

            self._clock.sleep_until(self._next_due())

        return self.finalize()
