"""Fast, offline tests for the paper-vs-simulated fill divergence report (ADR-0038).

Everything here runs on :class:`FakeAdapter` / :class:`FakeAlpacaClient` /
:class:`FakeClock`: no network, no credentials, no wall clock, no RNG. The whole
divergence *mechanism* is provable offline — a live run is evidence about the
market, not a test of the code.

The three things these tests exist to hold down:

1. **The shadow cannot perturb the live path.** A shadow that raises, a clock that
   raises, and a shadow that spends the same cash all leave the live run bit-for-bit
   what it would have been unwrapped.
2. **The counterfactual is the right one.** The reference price is the next bar's
   open, the modelled fill is that open plus ``slippage_bps``, and wrapping a
   ``SimulatedBroker`` in its own shadow reports exactly zero divergence.
3. **Nothing is dropped.** A rejection on one side and a fill on the other is a row,
   not a hole; so is an order the venue never settled.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

import pytest

from trading.broker import SimulatedBroker
from trading.brokers.alpaca import AlpacaBroker
from trading.clock import FakeClock
from trading.config import CostConfig, RiskConfig
from trading.data.alpaca_client import STATUS_CANCELED, FakeAlpacaClient
from trading.data.fake import FakeAdapter
from trading.divergence import (
    MIN_PAIRED_FILLS,
    OUTCOME_FILLED,
    OUTCOME_PARTIAL,
    OUTCOME_PENDING,
    OUTCOME_REJECTED,
    FillDivergence,
    Settlement,
    ShadowBroker,
    divergence_rows,
    render_report,
    summarize,
    write_divergence_csv,
)
from trading.engine import Engine
from trading.interfaces import Broker
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Bar, Fill, Order, Portfolio, Side

if TYPE_CHECKING:
    from pathlib import Path

START = datetime(2026, 1, 5, tzinfo=UTC)


def _bars(symbol: str, opens: list[float]) -> list[Bar]:
    """A series whose open is what matters; close tracks open so marks are simple."""
    return [
        Bar(
            symbol=symbol,
            ts=START + timedelta(days=i),
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            volume=1_000,
        )
        for i, price in enumerate(opens)
    ]


def _slice(bars: list[Bar]) -> dict[str, Bar]:
    return {bar.symbol: bar for bar in bars}


def _clock(step_seconds: int = 60) -> FakeClock:
    """A clock that advances a fixed amount on every read, so latency is non-zero."""
    base = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    return FakeClock(base, [base + timedelta(seconds=step_seconds * i) for i in range(1, 500)])


class _BoomBroker:
    """A counterfactual broker that fails at everything. Injected to prove the guard."""

    rejections: ClassVar[list[tuple[Order, str]]] = []

    @property
    def portfolio(self) -> Portfolio:
        raise RuntimeError("shadow portfolio exploded")

    def submit(self, order: Order) -> None:
        raise RuntimeError(f"shadow submit exploded on {order.symbol}")

    def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
        raise RuntimeError(f"shadow on_bar exploded on {sorted(bars)}")


class _BoomClock:
    """A clock that raises on every read (the other way shadow bookkeeping can fail)."""

    def now(self) -> datetime:
        raise RuntimeError("clock exploded")

    def sleep_until(self, ts: datetime) -> None:
        raise RuntimeError("clock exploded")


class TestSeamAndDelegation:
    """The wrapper is a Broker and it is transparent."""

    def test_satisfies_the_broker_protocol(self) -> None:
        live = SimulatedBroker(Portfolio(cash=1_000.0))
        assert isinstance(ShadowBroker(live, _clock()), Broker)

    def test_portfolio_is_the_live_one_not_a_copy(self) -> None:
        live = SimulatedBroker(Portfolio(cash=1_000.0))
        shadow = ShadowBroker(live, _clock())
        # Identity, not equality: the engine marks equity off this object every bar,
        # and a copy would silently report a book nobody is trading.
        assert shadow.portfolio is live.portfolio

    def test_rejections_pass_through_the_live_list(self) -> None:
        live = SimulatedBroker(Portfolio(cash=10.0))
        shadow = ShadowBroker(live, _clock())
        shadow.submit(Order("AAA", Side.BUY, qty=5))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))
        # The engine merges this into BacktestResult.rejections through getattr
        # (ADR-0036); it must be the live broker's own list, unaltered.
        assert shadow.rejections == live.rejections
        assert len(live.rejections) == 1

    def test_rejections_is_empty_when_the_live_broker_has_none(self) -> None:
        class _Bare:
            def __init__(self) -> None:
                self._portfolio = Portfolio(cash=100.0)

            @property
            def portfolio(self) -> Portfolio:
                return self._portfolio

            def submit(self, order: Order) -> None: ...

            def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
                return []

        assert ShadowBroker(_Bare(), _clock()).rejections == []


class TestCounterfactualDefinition:
    """ADR-0038: the reference is the next bar's open, and the model is 5 bps on it."""

    def _one_fill(self, *, slippage_bps: float = 5.0) -> FillDivergence:
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0)
        client.set_price("AAA", 101.0)  # the venue fills a whole dollar above the open
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock(), costs=CostConfig(slippage_bps=slippage_bps))

        shadow.on_bar(_slice(_bars("AAA", [99.0])))  # bar t: nothing outstanding
        shadow.submit(Order("AAA", Side.BUY, qty=10))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))  # bar t+1: open 100.0
        records = shadow.divergences
        assert len(records) == 1
        return records[0]

    def test_reference_price_is_the_next_bars_open(self) -> None:
        record = self._one_fill()
        assert record.reference_price == pytest.approx(100.0)

    def test_model_fills_at_the_open_plus_slippage(self) -> None:
        record = self._one_fill()
        assert record.shadow.outcome == OUTCOME_FILLED
        assert record.shadow.price == pytest.approx(100.0 * 1.0005)
        assert record.modelled_slippage_bps == pytest.approx(5.0)

    def test_realized_slippage_is_measured_against_the_same_reference(self) -> None:
        record = self._one_fill()
        # 101 vs a 100 open is 100 bps of adverse move on a buy.
        assert record.realized_slippage_bps == pytest.approx(100.0)
        assert record.slippage_error_bps == pytest.approx(95.0)
        assert record.price_difference == pytest.approx(101.0 - 100.05)

    def test_slippage_is_signed_adversely_for_a_sell(self) -> None:
        # A sell filled *below* the reference open costs money, so it is positive.
        record = FillDivergence(
            order=Order("AAA", Side.SELL, qty=1),
            submitted_at=START,
            submitted_ts=START,
            reference_price=100.0,
            live=Settlement(OUTCOME_FILLED, price=99.0, qty=1.0),
            shadow=Settlement(OUTCOME_FILLED, price=99.95, qty=1.0),
        )
        assert record.realized_slippage_bps == pytest.approx(100.0)
        assert record.modelled_slippage_bps == pytest.approx(5.0)

    def test_a_price_improvement_reads_negative(self) -> None:
        record = FillDivergence(
            order=Order("AAA", Side.BUY, qty=1),
            submitted_at=START,
            submitted_ts=START,
            reference_price=100.0,
            live=Settlement(OUTCOME_FILLED, price=99.9, qty=1.0),
            shadow=Settlement(OUTCOME_FILLED, price=100.05, qty=1.0),
        )
        assert record.realized_slippage_bps == pytest.approx(-10.0)

    def test_the_model_is_never_charged_against_a_different_bar(self) -> None:
        """An order that waits two bars is still priced at the *first* open it saw."""
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))  # model fills here, venue does not
        client.fill_order("1", price=120.0)
        shadow.on_bar(_slice(_bars("AAA", [130.0])))  # venue settles two bars later

        record = shadow.divergences[0]
        assert record.reference_price == pytest.approx(100.0)  # NOT 130.0
        assert record.shadow.price == pytest.approx(100.05)
        assert record.live.price == pytest.approx(120.0)


class TestNullTest:
    """Wrapping a SimulatedBroker in its own shadow must report no divergence.

    This is the mechanism's calibration: if the comparison were measured against
    the wrong reference price, the *same* broker would appear to diverge from
    itself, and every number the report prints about a real venue would be wrong by
    that same offset.
    """

    def test_simulated_against_simulated_diverges_by_zero(self) -> None:
        live = SimulatedBroker(Portfolio(cash=100_000.0))
        shadow = ShadowBroker(live, _clock())
        engine = Engine(
            FakeAdapter(_bars("AAA", [100.0, 101.0, 99.0, 103.0, 104.0])),
            shadow,
            Guardrails(RiskConfig()),
        )
        engine.run(get_strategy("buy_and_hold"), ["AAA"], START, START + timedelta(days=10))

        records = shadow.divergences
        comparable = [r for r in records if r.comparable]
        assert comparable, "the null test needs at least one paired fill to compare"
        for record in comparable:
            assert record.slippage_error_bps == pytest.approx(0.0)
            assert record.price_difference == pytest.approx(0.0)
            assert record.qty_divergence == pytest.approx(0.0)
        assert shadow.summary.outcome_divergences == 0
        assert shadow.summary.mean_error_bps == pytest.approx(0.0)


class TestShadowCannotPerturbTheLivePath:
    """The guard the whole feature depends on: a shadow bug never costs an order."""

    def _run(self, broker: Broker) -> object:
        engine = Engine(
            FakeAdapter(_bars("AAA", [100.0, 101.0, 99.0, 103.0])),
            broker,
            Guardrails(RiskConfig.unlimited()),
        )
        return engine.run(get_strategy("buy_and_hold"), ["AAA"], START, START + timedelta(days=10))

    def test_an_exploding_shadow_leaves_the_run_identical(self) -> None:
        plain = self._run(SimulatedBroker(Portfolio(cash=10_000.0)))

        live = SimulatedBroker(Portfolio(cash=10_000.0))
        wrapped = ShadowBroker(live, _clock(), shadow_factory=lambda _: _BoomBroker())
        broken = self._run(wrapped)

        assert broken == plain  # BacktestResult is a dataclass: this is the whole run.
        assert not wrapped.enabled
        assert any("shadow" in message for message in wrapped.errors)

    def test_an_exploding_clock_leaves_the_run_identical(self) -> None:
        plain = self._run(SimulatedBroker(Portfolio(cash=10_000.0)))

        live = SimulatedBroker(Portfolio(cash=10_000.0))
        wrapped = ShadowBroker(live, _BoomClock())
        broken = self._run(wrapped)

        assert broken == plain
        assert not wrapped.enabled

    def test_the_order_still_reaches_the_venue_when_the_shadow_fails(self) -> None:
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        live = AlpacaBroker(client, clock=_clock())
        wrapped = ShadowBroker(live, _clock(), shadow_factory=lambda _: _BoomBroker())

        wrapped.submit(Order("AAA", Side.BUY, qty=3))
        fills = wrapped.on_bar(_slice(_bars("AAA", [100.0])))

        # The real order was placed and the real fill came back, shadow or no shadow.
        assert [f.qty for f in fills] == [pytest.approx(3.0)]
        assert wrapped.portfolio.position("AAA").qty == pytest.approx(3.0)
        assert not wrapped.enabled

    def test_the_shadow_never_holds_the_live_portfolio(self) -> None:
        seen: list[Portfolio] = []

        def factory(snapshot: Portfolio) -> Broker:
            seen.append(snapshot)
            return SimulatedBroker(snapshot)

        live = SimulatedBroker(Portfolio(cash=10_000.0))
        wrapped = ShadowBroker(live, _clock(), shadow_factory=factory)
        wrapped.submit(Order("AAA", Side.BUY, qty=10))
        wrapped.on_bar(_slice(_bars("AAA", [100.0])))

        assert seen, "the shadow was never built"
        assert all(snapshot is not live.portfolio for snapshot in seen)
        assert all(snapshot.positions is not live.portfolio.positions for snapshot in seen)

    def test_the_shadow_spending_cash_does_not_move_the_live_book(self) -> None:
        """Both books buy the same shares; only the live one may be charged once."""
        live = SimulatedBroker(Portfolio(cash=1_000.0))
        wrapped = ShadowBroker(live, _clock())
        wrapped.submit(Order("AAA", Side.BUY, qty=5))
        wrapped.on_bar(_slice(_bars("AAA", [100.0])))

        assert live.portfolio.cash == pytest.approx(1_000.0 - 5 * 100.05)
        assert live.portfolio.position("AAA").qty == pytest.approx(5.0)

    def test_disabled_shadow_stays_disabled_and_still_delegates(self) -> None:
        live = SimulatedBroker(Portfolio(cash=10_000.0))
        wrapped = ShadowBroker(live, _clock(), shadow_factory=lambda _: _BoomBroker())
        wrapped.submit(Order("AAA", Side.BUY, qty=1))
        wrapped.on_bar(_slice(_bars("AAA", [100.0])))
        assert not wrapped.enabled
        errors_after_first = list(wrapped.errors)

        wrapped.submit(Order("AAA", Side.BUY, qty=1))
        fills = wrapped.on_bar(_slice(_bars("AAA", [101.0])))

        assert [f.qty for f in fills] == [pytest.approx(1.0)]  # live path untouched
        assert wrapped.errors == errors_after_first  # not retried every bar


class TestOffIsByteIdentical:
    """A run without divergence tracking must be exactly what it was before."""

    def test_wrapped_and_unwrapped_runs_agree_completely(self) -> None:
        bars = _bars("AAA", [100.0, 103.0, 97.0, 101.0, 105.0, 99.0])

        def run(broker: Broker) -> object:
            engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig()))
            return engine.run(
                get_strategy("sma_crossover"), ["AAA"], START, START + timedelta(days=30)
            )

        plain = run(SimulatedBroker(Portfolio(cash=10_000.0)))
        wrapped = run(ShadowBroker(SimulatedBroker(Portfolio(cash=10_000.0)), _clock()))
        assert wrapped == plain


class TestRejectionsDiverge:
    """A fill on one side and a refusal on the other is the row that matters most."""

    def test_model_rejects_for_funding_while_the_venue_fills(self) -> None:
        # The venue has cash (Alpaca's paper account); the modelled book does not.
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=50.0)
        client.set_price("AAA", 100.0)
        live = AlpacaBroker(client, clock=_clock())
        # AlpacaBroker reconciles $50 of cash, so the shadow snapshot is underfunded.
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        record = shadow.divergences[0]
        assert record.live.outcome == OUTCOME_FILLED
        assert record.shadow.outcome == OUTCOME_REJECTED
        assert record.shadow.reason is not None
        assert "insufficient cash" in record.shadow.reason
        assert record.outcome_diverged
        summary = shadow.summary
        assert summary.live_only_fills == 1
        assert summary.outcome_divergences == 1
        assert summary.comparable == 0  # nothing to compare on price, and it says so

    def test_venue_cancels_while_the_model_fills(self) -> None:
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        client.set_order_status("1", STATUS_CANCELED)
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        record = shadow.divergences[0]
        assert record.live.outcome == OUTCOME_REJECTED
        assert record.live.reason is not None
        assert "canceled" in record.live.reason
        assert record.shadow.outcome == OUTCOME_FILLED
        assert shadow.summary.model_only_fills == 1

    def test_a_partial_fill_then_cancel_is_one_row(self) -> None:
        """ADR-0033: the venue emits both a Fill and a rejection for one order."""
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        client.set_order_status("1", STATUS_CANCELED, filled_qty=4.0, filled_avg_price=100.5)
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        records = shadow.divergences
        assert len(records) == 1
        record = records[0]
        assert record.live.outcome == OUTCOME_PARTIAL
        assert record.live.qty == pytest.approx(4.0)
        assert record.live.reason is not None and "canceled" in record.live.reason
        assert record.shadow.qty == pytest.approx(10.0)
        assert record.qty_divergence == pytest.approx(-6.0)
        # A partial fill still has a comparable price.
        assert record.comparable
        assert record.realized_slippage_bps == pytest.approx(50.0)

    def test_an_order_the_venue_never_settled_is_reported_as_pending(self) -> None:
        """ADR-0036: a DAY order placed while the market is shut just parks."""
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        record = shadow.divergences[0]
        assert record.live.outcome == OUTCOME_PENDING
        assert record.shadow.outcome == OUTCOME_FILLED
        assert record.outcome_diverged
        assert record.latency is None
        assert live.pending_order_ids == ("1",)

    def test_simulated_broker_records_the_order_object_it_was_given(self) -> None:
        """The identity the shadow's outcome attribution relies on.

        ``_run_shadow`` decides "was this order rejected or filled?" by checking
        ``rejections[i][0] is order``. If ``SimulatedBroker`` ever copied the order,
        every rejected order would be mis-attributed as a fill.
        """
        broker = SimulatedBroker(Portfolio(cash=1.0))
        order = Order("AAA", Side.BUY, qty=10)
        broker.submit(order)
        broker.on_bar(_slice(_bars("AAA", [100.0])))
        assert broker.rejections[0][0] is order


class TestAttribution:
    """Several orders on one bar land on the right rows."""

    def test_multiple_symbols_are_matched_by_symbol_and_side(self) -> None:
        bars = {**_slice(_bars("AAA", [100.0])), **_slice(_bars("BBB", [50.0]))}
        client = FakeAlpacaClient(cash=100_000.0)
        client.set_price("AAA", 100.0)
        client.set_price("BBB", 50.0)
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("BBB", Side.BUY, qty=2))
        shadow.submit(Order("AAA", Side.BUY, qty=3))
        shadow.on_bar(bars)

        by_symbol = {r.symbol: r for r in shadow.divergences}
        assert by_symbol["AAA"].live.qty == pytest.approx(3.0)
        assert by_symbol["AAA"].reference_price == pytest.approx(100.0)
        assert by_symbol["BBB"].live.qty == pytest.approx(2.0)
        assert by_symbol["BBB"].reference_price == pytest.approx(50.0)
        assert not shadow.unmatched_live_fills

    def test_an_order_with_no_bar_stays_open_on_both_sides(self) -> None:
        client = FakeAlpacaClient(cash=100_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("ZZZ", Side.BUY, qty=1))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))  # no ZZZ bar this timestamp

        record = shadow.divergences[0]
        assert record.reference_price is None
        assert record.live.outcome == OUTCOME_PENDING
        assert record.shadow.outcome == OUTCOME_PENDING

    def test_an_unattributable_venue_fill_is_surfaced_not_swallowed(self) -> None:
        class _GhostBroker:
            rejections: ClassVar[list[tuple[Order, str]]] = []

            def __init__(self) -> None:
                self._portfolio = Portfolio(cash=1_000.0)

            @property
            def portfolio(self) -> Portfolio:
                return self._portfolio

            def submit(self, order: Order) -> None: ...

            def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
                return [Fill("AAA", Side.BUY, qty=1.0, price=100.0)]

        shadow = ShadowBroker(_GhostBroker(), _clock())
        shadow.on_bar(_slice(_bars("AAA", [100.0])))
        assert len(shadow.unmatched_live_fills) == 1
        assert shadow.summary.unmatched_live_fills == 1


class TestLatency:
    """Latency comes off the injected clock — never time.time()."""

    def test_latency_is_submit_to_observed_settlement(self) -> None:
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        live = AlpacaBroker(client, clock=_clock())
        # A dedicated clock so the readings the wrapper takes are the ones we assert.
        base = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
        wrapper_clock = FakeClock(base, [base, base + timedelta(seconds=90)])
        shadow = ShadowBroker(live, wrapper_clock)

        shadow.submit(Order("AAA", Side.BUY, qty=1))  # reads `base`
        shadow.on_bar(_slice(_bars("AAA", [100.0])))  # reads base + 90s

        record = shadow.divergences[0]
        assert record.latency == timedelta(seconds=90)
        assert record.modelled_latency == timedelta(seconds=90)
        assert shadow.summary.max_latency == timedelta(seconds=90)


class TestSummaryHonesty:
    """The report must not claim more than the sample supports (ADR-0029's spirit)."""

    def _record(self, realized_bps: float) -> FillDivergence:
        reference = 100.0
        return FillDivergence(
            order=Order("AAA", Side.BUY, qty=1),
            submitted_at=START,
            submitted_ts=START,
            reference_price=reference,
            live=Settlement(
                OUTCOME_FILLED,
                ts=START,
                observed_at=START,
                qty=1.0,
                price=reference * (1 + realized_bps / 10_000.0),
            ),
            shadow=Settlement(
                OUTCOME_FILLED, ts=START, observed_at=START, qty=1.0, price=reference * 1.0005
            ),
        )

    def test_a_thin_sample_refuses_to_conclude(self) -> None:
        summary = summarize([self._record(20.0) for _ in range(5)])
        assert not summary.conclusive
        text = render_report(summary, [])
        assert "neither confirmed nor refuted" in text
        assert str(MIN_PAIRED_FILLS) in text

    def test_an_empty_run_says_it_measured_nothing(self) -> None:
        text = render_report(summarize([]), [])
        assert "no comparable fills" in text

    def test_a_sufficient_sample_names_the_model_as_optimistic(self) -> None:
        records = [self._record(20.0) for _ in range(MIN_PAIRED_FILLS)]
        summary = summarize(records)
        assert summary.conclusive
        assert summary.mean_realized_bps == pytest.approx(20.0)
        assert summary.mean_error_bps == pytest.approx(15.0)
        assert summary.implied_slippage_bps == pytest.approx(20.0)
        text = render_report(summary, records)
        assert "optimistic" in text
        assert "not a market-wide constant" in text

    def test_a_conservative_model_is_named_too(self) -> None:
        summary = summarize([self._record(1.0) for _ in range(MIN_PAIRED_FILLS)])
        assert "conservative" in render_report(summary, [])

    def test_the_price_notion_is_stated(self) -> None:
        text = render_report(summarize([], price_notion="raw"), [])
        assert "raw" in text
        assert "ADR-0021" in text

    def test_shadow_failures_are_reported_not_hidden(self) -> None:
        summary = summarize([], errors=["on_bar: RuntimeError: boom"])
        text = render_report(summary, [])
        assert "shadow was disabled" in text
        assert "boom" in text

    def test_outcome_divergences_are_listed(self) -> None:
        record = replace(self._record(10.0), live=Settlement(OUTCOME_PENDING))
        text = render_report(summarize([record]), [record])
        assert "Outcome divergences" in text
        assert "AAA" in text


class TestCsvOutput:
    def test_every_tracked_order_gets_a_row_including_the_unsettled(self) -> None:
        filled = FillDivergence(
            order=Order("AAA", Side.BUY, qty=2),
            submitted_at=START,
            submitted_ts=START,
            reference_price=100.0,
            live=Settlement(OUTCOME_FILLED, ts=START, observed_at=START, qty=2.0, price=100.2),
            shadow=Settlement(OUTCOME_FILLED, ts=START, observed_at=START, qty=2.0, price=100.05),
        )
        parked = FillDivergence(
            order=Order("BBB", Side.SELL, qty=1),
            submitted_at=START,
            submitted_ts=START,
            reference_price=None,
            live=Settlement(OUTCOME_PENDING),
            shadow=Settlement(OUTCOME_PENDING),
        )
        rows = divergence_rows([filled, parked])
        assert [row["symbol"] for row in rows] == ["AAA", "BBB"]
        assert rows[0]["outcome_diverged"] == "false"
        assert float(rows[0]["realized_slippage_bps"]) == pytest.approx(20.0)
        assert rows[1]["live_price"] == ""  # absent, not a misleading zero
        assert rows[1]["latency_seconds"] == ""

    def test_writes_a_header_even_with_no_records(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "fill_divergence.csv"
        write_divergence_csv([], path)
        assert path.read_text().splitlines()[0].startswith("submitted_ts,")
