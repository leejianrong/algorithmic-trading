"""A live paper session warms up on history instead of trading it (ADR-0042).

The bug (KAN-697): ``RecentWindowFeed.poll`` asks for ``[datetime.min, now]`` and
keeps the last ``DEFAULT_PAPER_LOOKBACK`` completed bars, and ``PaperSession.run``
treats every timestamp it has not seen as *fresh* — so the **first** poll of a
``--live`` session drove ``Engine._step`` over the whole historical window and
submitted a real order for every bar of it. At ``--interval 5m`` that is 512 bars
across seven sessions: hundreds of orders priced off historical opens but filled
at today's price, swamping the fill-divergence sample (ADR-0038) the live session
exists to collect.

The two obvious fixes are both wrong, and the tests here pin *why*:

* **Skipping the backfill** starves the strategy. History accumulates only inside
  ``_step``, so a skipped backfill means ``sma_crossover`` cannot compute a 20-bar
  average until 20 live bars have passed.
* **Replaying the backfill with order submission suppressed** desynchronizes a
  stateful strategy from the account. ``sma_crossover`` and ``momentum`` keep a
  per-symbol ``_long`` latch and emit only on a *transition*; feed them history
  while swallowing the orders and they believe they are long while the book is
  flat, so the live session sits out the day in silence.

What actually happens: the warmup bars prime ``state.history`` and
``state.last_close`` as **data**, with the strategy, the sizer, the guardrails and
the broker never invoked, and no equity point recorded for a bar the account did
not live through. The strategy's first call is on a genuinely live bar, where it
transitions from flat exactly once.

Everything here is offline and deterministic (``FakeClock``, scripted feeds).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading.broker import SimulatedBroker
from trading.cli import app
from trading.clock import FakeClock
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.engine import Engine, Feed, PaperSession, build_feed
from trading.interfaces import StrategyContext
from trading.risk import Guardrails
from trading.strategies.sma_crossover import SmaCrossover
from trading.types import Bar, Fill, Order, Portfolio, Side, TargetWeight

_ZERO_COST = CostConfig(commission_per_share=0.0, slippage_bps=0.0)

runner = CliRunner()

# One fixed offline `paper --once` invocation, plus the SHA-256 its equity curve had
# on ``origin/main`` (commit ``dbb845f``) before a line of ADR-0042 was written.
_ONCE_ARGS = [
    "paper",
    "--strategy",
    "sma_crossover",
    "--symbols",
    "AAA,BBB",
    "--from",
    "2024-01-02",
    "--to",
    "2024-03-01",
    "--source",
    "synthetic",
    "--seed",
    "7",
]
_ONCE_GOLDEN = "50946899eca0d84d43a65dd096a3a58cd32a1ecad28dc3aff1334bee3f252eaf"


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _bar(symbol: str, day: int, price: float) -> Bar:
    return Bar(symbol, _ts(day), price, price, price, price, 1_000)


class _ScriptedFeed:
    """A ``CompletedBarFeed`` whose polls are scripted, so a test owns the boundary.

    Faithful to :class:`~trading.data.recent_window.RecentWindowFeed`, which is
    *cumulative*: every poll re-returns the whole completed window, and the session
    is what de-duplicates. Each entry is the full feed visible at that poll; the
    last entry repeats once the script runs out.
    """

    def __init__(self, polls: list[Feed]) -> None:
        self._polls = polls
        self.calls = 0

    def poll(self, symbols: list[str], lookback: int) -> Feed:
        index = min(self.calls, len(self._polls) - 1)
        self.calls += 1
        return self._polls[index]


class _RecordingBroker:
    """A ``Broker`` decorator that records every order that reaches the venue.

    The ticket's claim is about *submitted orders*, not about equity points, so the
    assertion has to be made at the broker seam — that is where a live session would
    have hit Alpaca. Everything is forwarded verbatim.
    """

    def __init__(self, inner: SimulatedBroker) -> None:
        self._inner = inner
        self.submitted: list[Order] = []

    @property
    def portfolio(self) -> Portfolio:
        return self._inner.portfolio

    @property
    def rejections(self) -> list[tuple[Order, str]]:
        return self._inner.rejections

    def submit(self, order: Order) -> None:
        self.submitted.append(order)
        self._inner.submit(order)

    def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
        return self._inner.on_bar(bars)


class _EveryBar:
    """Targets a fixed weight on *every* bar — the loudest possible order source."""

    def __init__(self, symbol: str, weight: float = 0.2) -> None:
        self._symbol = symbol
        self._weight = weight
        self.calls: list[datetime] = []
        self.history_lengths: list[int] = []

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        if self._symbol not in bars:
            return []
        self.calls.append(ts)
        self.history_lengths.append(len(context.history(self._symbol, 10_000)))
        return [TargetWeight(self._symbol, self._weight)]


def _session(
    polls: list[Feed],
    strategy: object,
    *,
    symbols: list[str] | None = None,
    warmup: bool = True,
    cash: float = 1_000.0,
) -> tuple[PaperSession, _RecordingBroker]:
    broker = _RecordingBroker(SimulatedBroker(Portfolio(cash=cash), _ZERO_COST))
    engine = Engine(FakeAdapter([]), broker, Guardrails(RiskConfig.unlimited()))
    clock = FakeClock(datetime(2024, 3, 1, tzinfo=UTC))
    session = PaperSession(
        engine,
        strategy,  # type: ignore[arg-type]
        symbols or ["AAA"],
        _ScriptedFeed(polls),
        clock,
        lookback=1_000,
        warmup=warmup,
    )
    return session, broker


def _feed_of(bars: list[Bar]) -> Feed:
    return build_feed({"AAA": [b for b in bars if b.symbol == "AAA"]})


class TestFirstPollIsWarmupNotTrading:
    """The headline: N historical bars on the first poll cost **zero** orders."""

    def test_no_orders_are_submitted_for_the_backfill(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        strategy = _EveryBar("AAA")
        session, broker = _session([_feed_of(history)], strategy)

        session.run(max_empty_polls=1)

        assert broker.submitted == [], (
            f"a live session's first poll submitted {len(broker.submitted)} order(s) "
            "for bars that had already closed before it started"
        )
        assert strategy.calls == [], "the strategy must not be run over the backfill"
        assert session.session_log == []

    def test_no_equity_points_are_fabricated_for_the_backfill(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        session, _ = _session([_feed_of(history)], _EveryBar("AAA"))

        result = session.run(max_empty_polls=1)

        # The account held nothing during the backfill; inventing a curve for it
        # would corrupt every metric computed from the curve.
        assert result.equity_curve == []
        assert result.fills == []

    def test_the_warmup_is_reported_not_silent(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        session, _ = _session([_feed_of(history)], _EveryBar("AAA"))

        session.run(max_empty_polls=1)

        assert session.warmup_bars == 30
        assert session.warmup_span == (_ts(1), _ts(30))
        assert session.warmup_complete is True


class TestWarmupStillFeedsTheStrategy:
    """Skipping the backfill would starve a strategy; priming it does not."""

    def test_first_live_bar_sees_the_whole_primed_history(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        live = _bar("AAA", 31, 131.0)
        strategy = _EveryBar("AAA")
        session, _ = _session([_feed_of(history), _feed_of([*history, live])], strategy)

        session.run(max_empty_polls=1)

        assert strategy.calls == [_ts(31)], "exactly one live bar should have been traded"
        # 30 primed + the live bar itself, i.e. the strategy is not starved.
        assert strategy.history_lengths == [31]

    def test_a_lookback_strategy_trades_on_the_very_first_live_bar(self) -> None:
        # A rising series: after 30 bars the 5-bar SMA is above the 20-bar SMA, so a
        # pristine SmaCrossover (which starts flat) crosses long on its first call.
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        live = _bar("AAA", 31, 131.0)
        strategy = SmaCrossover(fast=5, slow=20, weight=0.5)
        session, broker = _session([_feed_of(history), _feed_of([*history, live])], strategy)

        session.run(max_empty_polls=1)

        assert len(broker.submitted) == 1, f"expected one entry, got {broker.submitted}"
        order = broker.submitted[0]
        assert order.symbol == "AAA"
        assert order.side is Side.BUY
        assert order.qty > 0
        # *When* it was submitted is the whole point: under the bug this same single
        # order goes out on a historical bar around the 20th backfill step, priced off
        # a stale open. Pin it to the one live bar the session actually stepped.
        assert len(session.session_log) == 1
        assert session.session_log[0].ts == _ts(31)
        assert session.session_log[0].submitted == [order]

    def test_the_strategy_state_is_pristine_when_the_live_session_starts(self) -> None:
        """The trap: replaying with orders suppressed leaves ``_long`` already True.

        A strategy that latched ``long`` during a suppressed replay emits nothing on
        the live bar (no transition) and sits flat all day, silently. Priming history
        as *data* leaves the latch untouched, so the entry above really is the first
        crossing the strategy has ever seen.
        """
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        strategy = SmaCrossover(fast=5, slow=20, weight=0.5)
        session, _ = _session([_feed_of(history)], strategy)

        session.run(max_empty_polls=1)

        assert strategy._long == {}, "the strategy must not have been run over history"


class TestLaterPollsAreNotWarmup:
    """Warmup is the session's opening window only — never a mid-session bar."""

    def test_a_bar_arriving_mid_session_is_traded(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 4)]
        polls = [
            _feed_of(history),
            _feed_of([*history, _bar("AAA", 4, 104.0)]),
            _feed_of([*history, _bar("AAA", 4, 104.0), _bar("AAA", 5, 105.0)]),
        ]
        strategy = _EveryBar("AAA")
        session, broker = _session(polls, strategy)

        session.run(max_empty_polls=1)

        assert strategy.calls == [_ts(4), _ts(5)]
        assert len(broker.submitted) == 2
        assert session.warmup_bars == 3

    def test_an_empty_first_poll_does_not_burn_the_warmup(self) -> None:
        """A failed opening fetch must not turn the *next* poll into a live replay.

        ``RecentWindowFeed`` swallows a per-symbol fetch failure and returns an empty
        cross-section (ADR-0035), so "the first poll" is not a reliable boundary on
        its own: the warmup is the first poll that actually reveals bars.
        """
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        polls: list[Feed] = [[], [], _feed_of(history)]
        strategy = _EveryBar("AAA")
        session, broker = _session(polls, strategy)

        session.run(max_empty_polls=5)

        assert broker.submitted == []
        assert session.warmup_bars == 30

    def test_a_priming_poll_does_not_count_as_an_empty_poll(self) -> None:
        """A poll that primed 30 bars revealed plenty; it must not stop the session.

        ``max_empty_polls`` exists to end a session whose feed has gone quiet. If the
        warmup poll counted as quiet, a default live session would be one dull poll
        away from stopping before it ever traded.
        """
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        live = _bar("AAA", 31, 131.0)
        strategy = _EveryBar("AAA")
        # Warmup, then one genuinely quiet poll, then the first live bar.
        polls = [_feed_of(history), _feed_of(history), _feed_of([*history, live])]
        session, _ = _session(polls, strategy)

        session.run(max_empty_polls=2)

        assert strategy.calls == [_ts(31)], "the session stopped before its first live bar"


class TestReplayModeIsUnchanged:
    """``--once`` replays a range and trades it — that is its entire purpose."""

    def test_warmup_off_trades_every_bar_of_the_replay(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        strategy = _EveryBar("AAA")
        session, broker = _session([_feed_of(history)], strategy, warmup=False)

        result = session.run(max_empty_polls=1)

        assert len(strategy.calls) == 30
        assert len(result.equity_curve) == 30
        assert broker.submitted, "a replay must still trade"
        assert session.warmup_bars == 0
        assert session.warmup_span is None

    def test_warmup_off_leaves_the_session_log_complete(self) -> None:
        history = [_bar("AAA", d, 100.0 + d) for d in range(1, 31)]
        session, _ = _session([_feed_of(history)], _EveryBar("AAA"), warmup=False)

        session.run(max_empty_polls=1)

        assert [o.ts for o in session.session_log] == [_ts(d) for d in range(1, 31)]


class TestOnceIsByteIdentical:
    """``trading paper --once`` must be *exactly* what it was before ADR-0042.

    The replay is how the offline paper tests and every demo run work, so the safe
    new default must not reach it. The digest below was taken from ``origin/main``
    (commit ``dbb845f``) before a line of this slice was written; regenerate it only
    when a change to ``--once`` is genuinely intended, and say so in the commit.
    """

    def _run(self, tmp_path: Path, *extra: str) -> str:
        result = runner.invoke(app, [*_ONCE_ARGS, "--out", str(tmp_path), *extra])
        assert result.exit_code == 0, result.output
        return result.output

    def test_equity_curve_matches_the_pre_change_golden(self, tmp_path: Path) -> None:
        self._run(tmp_path)

        raw = (tmp_path / "equity_curve.csv").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == _ONCE_GOLDEN
        # Spot checks so a digest mismatch is diagnosable, not just red.
        lines = raw.decode().splitlines()
        assert len(lines) == 45  # header + 44 bars
        assert lines[1] == "2024-01-02T00:00:00+00:00,1000.000000,0.000000"
        assert lines[-1] == "2024-03-01T00:00:00+00:00,989.632386,0.000000"

    def test_the_replay_trades_and_never_reports_a_warmup(self, tmp_path: Path) -> None:
        output = self._run(tmp_path)

        assert "Processed 44 completed bar(s)." in output
        assert "Warmup:" not in output, "--once replays a range; it does not warm up"

    def test_lookback_is_a_floor_under_once_not_a_truncation(self, tmp_path: Path) -> None:
        """``--lookback`` must never silently shorten a replay.

        The window is also what each poll requests, so a small value would drop the
        oldest bars of the range — a replay that quietly covered less than asked for.
        """
        output = self._run(tmp_path, "--lookback", "5")

        assert "Processed 44 completed bar(s)." in output
        raw = (tmp_path / "equity_curve.csv").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == _ONCE_GOLDEN


class TestPrimedMarksCarryForward:
    """``last_close`` is primed too, so a symbol absent from a live bar still marks."""

    def test_a_symbol_missing_from_the_live_bar_keeps_its_primed_mark(self) -> None:
        aaa = [_bar("AAA", d, 100.0) for d in range(1, 4)]
        bbb = [_bar("BBB", d, 50.0) for d in range(1, 4)]
        warm = build_feed({"AAA": aaa, "BBB": bbb})
        live = build_feed({"AAA": [*aaa, _bar("AAA", 4, 110.0)], "BBB": bbb})
        strategy = _EveryBar("AAA", weight=0.0)
        session, _ = _session([warm, live], strategy, symbols=["AAA", "BBB"])

        session.run(max_empty_polls=1)

        assert session.state.last_close["BBB"] == pytest.approx(50.0)
        assert session.state.last_close["AAA"] == pytest.approx(110.0)
