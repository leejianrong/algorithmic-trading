"""Fast, no-infra acceptance tests for V5 paper mode (dev-playbook layer 1).

The headline criterion (SLICES V5): fed a scripted sequence of *newly completed*
bars via a fake clock + fake adapter, paper mode places the SAME orders — and
reaches the same positions, equity, fills, clamps, and halt — that a ``backtest``
over those identical bars produces. Because both modes drive the *same*
``Engine._step`` (ADR-0002), parity is by construction; these tests make it
airtight and also prove the completed-bar gate and the guardrail halt carry over.

Everything here uses ``FakeClock`` and ``FakeAdapter`` — no real waiting, no
network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.broker import SimulatedBroker
from trading.clock import FakeClock, ImmediateClock
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.recent_window import RecentWindowFeed, interval_is_complete
from trading.engine import BacktestResult, Engine, PaperSession
from trading.frequency import DAILY, Frequency
from trading.interfaces import Strategy, StrategyContext
from trading.risk import Guardrails
from trading.types import Bar, Order, Portfolio, Side, TargetWeight

_ZERO_COST = CostConfig(commission_per_share=0.0, slippage_bps=0.0)


def _ts(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, minute, tzinfo=UTC)


def _bar(symbol: str, day: int, o: float, c: float) -> Bar:
    return Bar(symbol, _ts(day), o, max(o, c), min(o, c), c, 1_000)


class _ScriptedWeights:
    """Emit a scripted target weight per bar for one symbol — deterministic.

    Two instances fed the identical bar sequence make the identical decisions, so
    it is the ideal driver for a backtest-vs-paper parity test. A ``None`` entry
    means "no intent this bar".
    """

    def __init__(self, symbol: str, weights: list[float | None]) -> None:
        self._symbol = symbol
        self._weights = weights
        self._i = 0

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._symbol not in bars:
            return []
        i = self._i
        self._i += 1
        if i < len(self._weights) and self._weights[i] is not None:
            weight = self._weights[i]
            assert weight is not None
            return [TargetWeight(self._symbol, weight)]
        return []


class _ScriptedCrash:
    """Deploy 50% on bar 1, try to add after the crash (blocked), then exit."""

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol
        self._bar = 0

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._symbol not in bars:
            return []
        self._bar += 1
        if self._bar == 1:
            return [TargetWeight(self._symbol, 0.5)]
        if self._bar == 4:
            return [Order(self._symbol, Side.BUY, 1.0)]  # post-crash: halt-blocked
        if self._bar == 5:
            held = context.portfolio.position(self._symbol).qty
            if held > 0:
                return [Order(self._symbol, Side.SELL, held)]  # exit — allowed
        return []


def _assert_parity(backtest: BacktestResult, paper: BacktestResult) -> None:
    """The two runs must agree on the curve, fills, positions, cash, and guards."""
    bt_curve = [(p.ts, p.equity, p.exposure) for p in backtest.equity_curve]
    paper_curve = [(p.ts, p.equity, p.exposure) for p in paper.equity_curve]
    assert paper_curve == pytest.approx(bt_curve)

    assert [(ts, f) for ts, f in paper.fills] == [(ts, f) for ts, f in backtest.fills]
    assert paper.final_portfolio.cash == pytest.approx(backtest.final_portfolio.cash)
    assert set(paper.final_portfolio.positions) == set(backtest.final_portfolio.positions)
    for symbol, pos in backtest.final_portfolio.positions.items():
        assert paper.final_portfolio.position(symbol).qty == pytest.approx(pos.qty)

    assert len(paper.clamps) == len(backtest.clamps)
    assert len(paper.rejections) == len(backtest.rejections)
    assert paper.halted == backtest.halted
    assert paper.halt_ts == backtest.halt_ts


def _paper_over(
    bars: list[Bar],
    symbols: list[str],
    strategy: Strategy,
    risk: RiskConfig,
    *,
    clock: FakeClock | ImmediateClock,
    starting_cash: float = 1_000.0,
    **run_kwargs: int,
) -> tuple[BacktestResult, PaperSession]:
    broker = SimulatedBroker(Portfolio(cash=starting_cash), _ZERO_COST)
    engine = Engine(FakeAdapter(bars), broker, Guardrails(risk))
    feed = RecentWindowFeed(FakeAdapter(bars), clock, lambda b, now: True)
    session = PaperSession(engine, strategy, symbols, feed, clock, lookback=1_000)
    result = session.run(max_empty_polls=1, **run_kwargs)
    return result, session


def _backtest_over(
    bars: list[Bar],
    symbols: list[str],
    strategy: Strategy,
    risk: RiskConfig,
    start: datetime,
    end: datetime,
    *,
    starting_cash: float = 1_000.0,
) -> BacktestResult:
    broker = SimulatedBroker(Portfolio(cash=starting_cash), _ZERO_COST)
    engine = Engine(FakeAdapter(bars), broker, Guardrails(risk))
    return engine.run(strategy, symbols, start, end)


class TestParity:
    def test_paper_matches_backtest_on_the_same_bars(self) -> None:
        # A buy, a hold, an over-cap buy (clamped), an exit, and a re-entry — so the
        # parity check spans fills, a clamp, a full sell, and a fresh position.
        bars = [
            _bar("AAA", 1, o=100, c=100),
            _bar("AAA", 2, o=100, c=110),
            _bar("AAA", 3, o=110, c=90),
            _bar("AAA", 4, o=90, c=95),
            _bar("AAA", 5, o=95, c=100),
            _bar("AAA", 6, o=100, c=100),
        ]
        weights: list[float | None] = [0.2, None, 0.5, 0.0, 0.1, None]
        risk = RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0, max_drawdown_pct=1.0)

        backtest = _backtest_over(
            bars, ["AAA"], _ScriptedWeights("AAA", weights), risk, _ts(1), _ts(6)
        )
        # Fake clock parked past the last bar → every bar reads as complete at once.
        paper, session = _paper_over(
            bars, ["AAA"], _ScriptedWeights("AAA", weights), risk, clock=FakeClock(_ts(7))
        )

        _assert_parity(backtest, paper)
        assert backtest.clamps, "test should exercise a clamp"
        assert len(session.session_log) == 6

    def test_paper_reaches_same_positions_multi_symbol(self) -> None:
        bars = [_bar(s, d, o=100, c=100 + d) for s in ("AAA", "BBB") for d in range(1, 5)]

        class _EqualWeightEachBar:
            def on_bar(
                self, ts: datetime, slice_: dict[str, Bar], context: StrategyContext
            ) -> list[Order | TargetWeight]:
                return [TargetWeight(s, 0.2) for s in sorted(slice_)]

        risk = RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0, max_drawdown_pct=1.0)
        backtest = _backtest_over(bars, ["AAA", "BBB"], _EqualWeightEachBar(), risk, _ts(1), _ts(4))
        paper, _ = _paper_over(
            bars, ["AAA", "BBB"], _EqualWeightEachBar(), risk, clock=FakeClock(_ts(6))
        )
        _assert_parity(backtest, paper)


class TestCompletedBarsOnly:
    def test_paper_loop_never_processes_the_forming_bar(self) -> None:
        # Bars through D=5; the clock sits mid-session on D=5 and never advances
        # (ImmediateClock.sleep_until is a no-op), so D=5 stays forming forever.
        bars = [_bar("AAA", d, o=100, c=100) for d in range(1, 6)]
        broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig.unlimited()))
        clock = ImmediateClock(start=_ts(5, hour=15))
        feed = RecentWindowFeed(FakeAdapter(bars), clock)  # default completeness policy
        session = PaperSession(engine, _ScriptedWeights("AAA", [0.1]), ["AAA"], feed, clock)

        result = session.run(max_empty_polls=1)

        processed = [o.ts for o in session.session_log]
        assert _ts(5) not in processed, "the forming bar must never be processed"
        assert processed == [_ts(1), _ts(2), _ts(3), _ts(4)]
        assert all(p.ts != _ts(5) for p in result.equity_curve)

    def test_bar_processed_once_the_clock_crosses_into_next_day(self) -> None:
        # The same forming bar IS processed once the clock advances past its day.
        bars = [_bar("AAA", d, o=100, c=100) for d in range(1, 6)]
        broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig.unlimited()))
        clock = FakeClock(_ts(6))  # a day past the last bar → all complete
        feed = RecentWindowFeed(FakeAdapter(bars), clock)
        session = PaperSession(engine, _ScriptedWeights("AAA", [0.1]), ["AAA"], feed, clock)

        session.run(max_empty_polls=1)

        assert _ts(5) in [o.ts for o in session.session_log]


class TestIdempotentReprocessing:
    def test_a_repolled_timestamp_is_never_processed_twice(self) -> None:
        # A feed that returns the same completed bars on every poll must not cause
        # any bar to be stepped more than once.
        bars = [_bar("AAA", d, o=100, c=100) for d in range(1, 4)]
        broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig.unlimited()))
        clock = FakeClock(_ts(10))
        feed = RecentWindowFeed(FakeAdapter(bars), clock)
        session = PaperSession(engine, _ScriptedWeights("AAA", [0.1]), ["AAA"], feed, clock)

        session.run(max_empty_polls=3)  # several extra polls, all re-seeing the bars

        processed = [o.ts for o in session.session_log]
        assert processed == [_ts(1), _ts(2), _ts(3)]  # each exactly once


class TestGuardrailParity:
    def test_scripted_drawdown_halts_paper_exactly_as_backtest(self) -> None:
        bars = [
            _bar("AAA", 1, o=100, c=100),
            _bar("AAA", 2, o=100, c=100),  # 50% buy fills → 5 shares
            _bar("AAA", 3, o=100, c=100),  # peak equity $1,000
            _bar("AAA", 4, o=100, c=50),  # crash → 25% drawdown, halt latches
            _bar("AAA", 5, o=50, c=50),  # exit submitted
            _bar("AAA", 6, o=50, c=50),  # exit fills
        ]
        risk = RiskConfig(max_position_pct=1.0, max_gross_exposure=1.0, max_drawdown_pct=0.20)

        backtest = _backtest_over(bars, ["AAA"], _ScriptedCrash("AAA"), risk, _ts(1), _ts(6))
        paper, _ = _paper_over(bars, ["AAA"], _ScriptedCrash("AAA"), risk, clock=FakeClock(_ts(7)))

        _assert_parity(backtest, paper)
        assert paper.halted is True
        assert paper.halt_ts == _ts(4)
        # The post-crash buy was halt-blocked in paper, just as in backtest.
        blocked = [(o, r) for o, r in paper.rejections if o.side is Side.BUY and "drawdown" in r]
        assert blocked, f"expected a halt-blocked buy in paper, got {paper.rejections}"
        # The exit still filled and flattened the book.
        assert any(f.side is Side.SELL for _, f in paper.fills)
        assert paper.final_portfolio.position("AAA").qty == pytest.approx(0.0)


class TestSleepAndPolling:
    def test_paper_sleeps_between_polls_and_stops_when_no_new_bars(self) -> None:
        bars = [_bar("AAA", d, o=100, c=100) for d in range(1, 4)]
        broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig.unlimited()))
        clock = FakeClock(_ts(10))
        feed = RecentWindowFeed(FakeAdapter(bars), clock)
        session = PaperSession(
            engine,
            _ScriptedWeights("AAA", [0.1]),
            ["AAA"],
            feed,
            clock,
            poll_interval=timedelta(days=1),
        )

        session.run(max_empty_polls=2)

        # It waited on the wall clock (recorded, never really slept) before stopping.
        assert clock.sleep_calls, "paper should sleep_until the next due time between polls"

    def test_max_new_bars_bounds_the_session(self) -> None:
        bars = [_bar("AAA", d, o=100, c=100) for d in range(1, 11)]
        broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig.unlimited()))
        clock = FakeClock(_ts(20))
        feed = RecentWindowFeed(FakeAdapter(bars), clock)
        session = PaperSession(engine, _ScriptedWeights("AAA", [0.1]), ["AAA"], feed, clock)

        session.run(max_new_bars=3)

        assert len(session.session_log) == 3


def _session_with_clock(
    clock: FakeClock,
    *,
    poll_interval: timedelta | None = None,
    frequency: Frequency | None = None,
) -> PaperSession:
    broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
    engine = Engine(FakeAdapter([]), broker, Guardrails(RiskConfig.unlimited()))
    feed = RecentWindowFeed(FakeAdapter([]), clock)
    return PaperSession(
        engine,
        _ScriptedWeights("AAA", []),
        ["AAA"],
        feed,
        clock,
        poll_interval=poll_interval,
        frequency=frequency,
    )


class TestCadence:
    """ADR-0022: _next_due generalizes to interval boundaries; daily is unchanged."""

    def test_daily_default_is_start_of_next_day(self) -> None:
        # Byte-compatible with V5: mid-session on D=5 → next due is D=6 00:00 UTC.
        clock = FakeClock(_ts(5, hour=15))
        session = _session_with_clock(clock)  # default poll_interval → 1 day
        assert session._next_due() == _ts(6)

    def test_daily_at_midnight_advances_a_full_day(self) -> None:
        # Strictly-after: standing exactly on a boundary rolls to the next one.
        clock = FakeClock(_ts(5))
        session = _session_with_clock(clock)
        assert session._next_due() == _ts(6)

    def test_frequency_sets_the_poll_interval(self) -> None:
        # An hourly frequency (no explicit poll_interval) → next hour boundary.
        clock = FakeClock(_ts(5, hour=13, minute=45))
        session = _session_with_clock(clock, frequency=Frequency.parse("1h"))
        assert session._next_due() == _ts(5, hour=14)

    def test_sub_daily_boundary_is_strictly_after_now(self) -> None:
        clock = FakeClock(_ts(5, hour=14, minute=30))  # exactly on a 30m boundary
        session = _session_with_clock(clock, poll_interval=timedelta(minutes=30))
        assert session._next_due() == _ts(5, hour=15)  # the next one, not this one

    def test_explicit_poll_interval_beats_frequency(self) -> None:
        clock = FakeClock(_ts(5, hour=13, minute=10))
        session = _session_with_clock(clock, poll_interval=timedelta(minutes=5), frequency=DAILY)
        assert session._next_due() == _ts(5, hour=13, minute=15)


class TestIntradayParity:
    """Backtest and paper stay identical on intraday bars under the interval gate."""

    def test_paper_matches_backtest_on_intraday_bars(self) -> None:
        # Four 1-hour bars on one day; START timestamps 13:30..16:30.
        def _hbar(hour: int, o: float, c: float) -> Bar:
            ts = datetime(2024, 1, 2, hour, 30, tzinfo=UTC)
            return Bar("AAA", ts, o, max(o, c), min(o, c), c, 1_000)

        bars = [_hbar(13, 100, 101), _hbar(14, 101, 102), _hbar(15, 102, 100), _hbar(16, 100, 103)]
        weights: list[float | None] = [0.2, None, 0.1, None]
        risk = RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0, max_drawdown_pct=1.0)
        interval = timedelta(hours=1)

        bt_broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        bt_engine = Engine(FakeAdapter(bars), bt_broker, Guardrails(risk))
        backtest = bt_engine.run(
            _ScriptedWeights("AAA", weights),
            ["AAA"],
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
        )

        # Clock parked after the last bar's close → all four read complete at once.
        clock = FakeClock(datetime(2024, 1, 2, 18, tzinfo=UTC))
        broker = SimulatedBroker(Portfolio(cash=1_000.0), _ZERO_COST)
        engine = Engine(FakeAdapter(bars), broker, Guardrails(risk))
        feed = RecentWindowFeed(FakeAdapter(bars), clock, interval_is_complete(interval))
        session = PaperSession(
            engine,
            _ScriptedWeights("AAA", weights),
            ["AAA"],
            feed,
            clock,
            frequency=Frequency.parse("1h"),
            lookback=1_000,
        )
        paper = session.run(max_empty_polls=1)

        _assert_parity(backtest, paper)
        assert len(session.session_log) == 4


class TestFinalizeAfterInterrupt:
    """A ``--live`` session's only exit is an interrupt (ADR-0033).

    ``PaperSession.run`` returns the result at each of its stop conditions, but a
    live session never reaches one -- it polls until someone hits Ctrl-C. So the
    result has to be buildable from *outside* the loop, or the equity CSV and
    ``result.json`` are unreachable in the one mode that matters most.
    """

    def _bars(self) -> list[Bar]:
        return [_bar("AAA", 2, 100.0, 100.0), _bar("AAA", 3, 100.0, 110.0)]

    def test_finalize_matches_what_run_returned(self) -> None:
        ran, session = _paper_over(
            self._bars(),
            ["AAA"],
            _ScriptedWeights("AAA", [1.0, None]),
            RiskConfig.unlimited(),
            clock=ImmediateClock(_ts(4)),
        )

        again = session.finalize()

        assert [p.equity for p in again.equity_curve] == [p.equity for p in ran.equity_curve]
        assert again.fills == ran.fills

    def test_finalize_covers_bars_processed_so_far(self) -> None:
        _, session = _paper_over(
            self._bars(),
            ["AAA"],
            _ScriptedWeights("AAA", [1.0, None]),
            RiskConfig.unlimited(),
            clock=ImmediateClock(_ts(4)),
            max_new_bars=1,
        )

        partial = session.finalize()

        # One bar processed -> one equity point: not zero, and not both bars.
        assert len(session.session_log) == 1
        assert len(partial.equity_curve) == 1

    def test_finalize_is_safe_before_any_bar(self) -> None:
        session = _session_with_clock(FakeClock(_ts(2)))
        result = session.finalize()
        assert result.equity_curve == []
        assert result.fills == []
