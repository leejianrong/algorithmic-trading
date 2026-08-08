"""The benchmark can no longer sit silently in cash (KAN-672, ADR-0037 amended).

The bug: ``cli._run_benchmark`` runs ``buy_and_hold`` under
``RiskConfig.unlimited()``, so nothing clamps its entry. ``buy_and_hold`` sized
its one allocation from bar *t*'s close, but the fill lands at bar *t+1*'s open
plus slippage (ADR-0001/0004) — so an overnight gap up of more than the ~20 bps
of headroom in ``INVESTED_WEIGHT`` overshot the cash and the broker rejected it.
The strategy had already latched ``_invested``, so it never tried again and the
benchmark reported ``+0.00%`` for the whole run. An insufficient-cash rejection is
recorded, not raised, so nothing upstream could see it.

These tests own both halves of the fix: the strategy keeps its entry intent alive
until the position exists, and a benchmark that fails to invest anyway says so
instead of printing idle cash as a market return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.broker import SimulatedBroker
from trading.cli import _warn_if_benchmark_never_invested
from trading.config import RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.synthetic import SyntheticAdapter
from trading.engine import BacktestResult, Engine, EquityPoint
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.strategies.buy_and_hold import BuyAndHold
from trading.types import Bar, Order, Portfolio, Position, Side, TargetWeight

START = datetime(2024, 1, 1, tzinfo=UTC)


def _bar(symbol: str, day: int, open_: float, close: float) -> Bar:
    """One bar with an explicit open/close; high/low bracket them, volume fixed."""
    return Bar(
        symbol=symbol,
        ts=START + timedelta(days=day),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1_000_000,
    )


def _run(bars: list[Bar], *, cash: float = 1_000.0) -> BacktestResult:
    """Run ``buy_and_hold`` over ``bars`` exactly as ``cli._run_benchmark`` does."""
    adapter = FakeAdapter(bars)
    broker = SimulatedBroker(Portfolio(cash=cash))
    engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
    symbols = sorted({bar.symbol for bar in bars})
    return engine.run(BuyAndHold(), symbols, START, START + timedelta(days=len(bars) + 10))


class _StubContext:
    """The minimal :class:`~trading.interfaces.StrategyContext` the strategy uses."""

    def __init__(self, portfolio: Portfolio) -> None:
        self.portfolio = portfolio

    def history(self, symbol: str, lookback: int) -> list[Bar]:
        return []


class TestTheReproductionFromTheTicket:
    """Synthetic ``SPY`` seed 7 over 2018 — the run that printed ``+0.00%``."""

    SEED = 7
    FROM = datetime(2018, 1, 1, tzinfo=UTC)
    TO = datetime(2018, 12, 31, tzinfo=UTC)

    @classmethod
    def _benchmark_run(cls) -> BacktestResult:
        adapter = SyntheticAdapter(seed=cls.SEED)
        broker = SimulatedBroker(Portfolio(cash=1000.0))
        engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
        return engine.run(get_strategy("buy_and_hold"), ["SPY"], cls.FROM, cls.TO)

    def test_the_benchmark_actually_invests(self) -> None:
        """What the benchmark is *supposed* to do: buy once and hold.

        This was ``xfail(strict=True)`` in ``test_report.py`` while the bug stood;
        the marker is gone because the fix landed, which is the whole point of
        having made it strict.
        """
        result = self._benchmark_run()
        assert result.fills != []
        assert max(point.exposure for point in result.equity_curve) > 0.9

    def test_the_first_entry_is_still_rejected_and_the_second_one_clears(self) -> None:
        """The gap that caused the bug is unchanged — only the response to it is."""
        result = self._benchmark_run()
        reasons = [reason for _order, reason in result.rejections]
        assert any("insufficient cash" in reason for reason in reasons), reasons
        # One rejected attempt, then a fill. It holds from then on: buy-and-hold
        # never sells, so exactly one fill is the whole trading record.
        assert len(result.fills) == 1

    def test_it_holds_from_the_entry_to_the_end(self) -> None:
        exposures = [point.exposure for point in self._benchmark_run().equity_curve]
        entered = next(i for i, x in enumerate(exposures) if x > 0.0)
        assert all(x > 0.9 for x in exposures[entered:])


class TestTheEntryIsRetriedNotLatched:
    """The defect was a one-shot entry that could not recover. Now it recovers."""

    def test_a_gap_up_that_rejects_the_entry_is_retried_on_the_next_bar(self) -> None:
        # Bar 1 closes at 100 -> qty 9.98. Bar 2 opens 6% higher, so the fill would
        # cost ~1058 against 1000 cash: rejected. Bar 2 closes back at 100 and bar 3
        # opens flat, so the retry clears.
        result = _run(
            [
                _bar("AAA", 1, 100.0, 100.0),
                _bar("AAA", 2, 106.0, 100.0),
                _bar("AAA", 3, 100.0, 100.0),
                _bar("AAA", 4, 100.0, 100.0),
            ]
        )
        assert [reason for _o, reason in result.rejections] == [
            "insufficient cash: need 1058.41, have 1000.00"
        ]
        assert [fill.symbol for _ts, fill in result.fills] == ["AAA"]
        assert result.equity_curve[-1].exposure > 0.9

    def test_it_keeps_retrying_across_several_failed_bars(self) -> None:
        """A run of gap-ups delays the entry; it does not cancel it."""
        bars = [_bar("AAA", 1, 100.0, 100.0)]
        bars += [_bar("AAA", day, 106.0, 100.0) for day in range(2, 6)]
        bars.append(_bar("AAA", 6, 100.0, 100.0))
        result = _run(bars)
        assert len(result.rejections) == 4
        assert len(result.fills) == 1
        assert result.equity_curve[-1].exposure > 0.9

    def test_once_established_it_never_trades_again(self) -> None:
        """Buy-and-hold, not constant-mix: a drifting price is never rebalanced.

        Each bar opens exactly where the last one closed, so the entry clears on
        the first attempt and every later bar is pure drift.
        """
        bars = [_bar("AAA", 1, 100.0, 100.0)]
        bars += [
            _bar("AAA", day, 100.0 + (day - 2) * 10.0, 100.0 + (day - 1) * 10.0)
            for day in range(2, 12)
        ]
        result = _run(bars)
        assert len(result.fills) == 1
        assert result.rejections == []

    def test_a_book_that_already_holds_something_is_left_alone(self) -> None:
        """The old ``context.portfolio.positions`` guard, preserved exactly."""
        strategy = BuyAndHold()
        portfolio = Portfolio(cash=500.0, positions={"ZZZ": Position("ZZZ", 1.0, 100.0)})
        bars = {"AAA": _bar("AAA", 1, 100.0, 100.0)}
        assert strategy.on_bar(START, bars, _StubContext(portfolio)) == []
        # And it stays stood down, rather than allocating on a later bar.
        assert strategy.on_bar(START + timedelta(days=1), bars, _StubContext(portfolio)) == []

    def test_the_first_bar_still_emits_the_target_weights_it_always_did(self) -> None:
        """The healthy path is untouched: same intents, same type, same weights."""
        strategy = BuyAndHold()
        bars = {"BBB": _bar("BBB", 1, 50.0, 50.0), "AAA": _bar("AAA", 1, 100.0, 100.0)}
        intents = strategy.on_bar(START, bars, _StubContext(Portfolio(cash=1_000.0)))
        assert intents == [TargetWeight("AAA", 0.499), TargetWeight("BBB", 0.499)]


class TestAPartlyEstablishedAllocation:
    """One leg fills, another is starved — the case a naive retry loops on forever."""

    @staticmethod
    def _starved_bars() -> list[Bar]:
        # Two legs at 0.499 each of 1000 = 499 apiece. AAA's open gaps +6%, so it
        # eats 529 of the cash and BBB's 499 no longer fits: BBB is rejected.
        return [
            _bar("AAA", 1, 100.0, 100.0),
            _bar("BBB", 1, 100.0, 100.0),
            _bar("AAA", 2, 106.0, 106.0),
            _bar("BBB", 2, 100.0, 100.0),
            _bar("AAA", 3, 106.0, 106.0),
            _bar("BBB", 3, 100.0, 100.0),
            _bar("AAA", 4, 106.0, 106.0),
            _bar("BBB", 4, 100.0, 100.0),
        ]

    def test_the_starved_leg_is_funded_from_the_cash_that_is_actually_left(self) -> None:
        result = self._starved_bars()
        run = _run(result)
        assert sorted(run.final_portfolio.positions) == ["AAA", "BBB"]
        # Exactly one rejection: the first attempt at BBB. The retry is sized
        # against remaining cash, so it does not re-demand money AAA has spent.
        assert len(run.rejections) == 1
        assert run.rejections[0][0].symbol == "BBB"

    def test_the_retry_spends_the_remaining_cash_not_a_weight_of_equity(self) -> None:
        """The retry is an Order funded from cash, with the same headroom."""
        strategy = BuyAndHold()
        bars = {"AAA": _bar("AAA", 1, 100.0, 100.0), "BBB": _bar("BBB", 1, 100.0, 100.0)}
        context = _StubContext(Portfolio(cash=1_000.0))
        strategy.on_bar(START, bars, context)
        # AAA filled and ate 529; 471 of cash is left and BBB is still flat.
        context.portfolio = Portfolio(cash=471.0, positions={"AAA": Position("AAA", 4.99, 106.0)})
        retry = strategy.on_bar(START + timedelta(days=1), bars, context)
        # 0.998 * 471 / 1 leg / 100.0 per share.
        assert retry == [Order("BBB", Side.BUY, round(0.998 * 471.0 / 100.0, 6))]

    def test_with_no_cash_left_it_stops_submitting_instead_of_spinning(self) -> None:
        """An unfundable leg costs one rejection, not one per remaining bar."""
        strategy = BuyAndHold()
        bars = {"AAA": _bar("AAA", 1, 100.0, 100.0), "BBB": _bar("BBB", 1, 100.0, 100.0)}
        context = _StubContext(Portfolio(cash=1_000.0))
        strategy.on_bar(START, bars, context)
        context.portfolio = Portfolio(cash=0.0, positions={"AAA": Position("AAA", 9.98, 100.0)})
        assert strategy.on_bar(START + timedelta(days=1), bars, context) == []


class TestAFailedBenchmarkIsSurfaced:
    """A benchmark that fails to invest must never report a return as if it were real."""

    @staticmethod
    def _flat_benchmark() -> BacktestResult:
        curve = [EquityPoint(START + timedelta(days=i), 1_000.0, 0.0) for i in range(5)]
        result = BacktestResult(
            symbols=["SPY"],
            starting_cash=1_000.0,
            equity_curve=curve,
            final_portfolio=Portfolio(cash=1_000.0),
        )
        result.rejections = [
            (Order("SPY", Side.BUY, 0.287874), "insufficient cash: need 1001.54, have 1000.00")
        ]
        return result

    def test_the_cli_warns_on_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        _warn_if_benchmark_never_invested("SPY", self._flat_benchmark())
        err = capsys.readouterr().err
        assert "warning: benchmark SPY never took a position" in err
        assert "idle cash" in err
        assert "insufficient cash: need 1001.54, have 1000.00" in err

    def test_a_healthy_benchmark_gets_no_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        healthy = self._flat_benchmark()
        healthy.equity_curve = [
            EquityPoint(point.ts, point.equity, 0.998) for point in healthy.equity_curve
        ]
        _warn_if_benchmark_never_invested("SPY", healthy)
        assert capsys.readouterr().err == ""
