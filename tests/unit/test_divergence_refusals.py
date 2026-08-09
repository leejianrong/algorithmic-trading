"""A refusal at submit is not an order sitting at the venue (ADR-0050).

ADR-0044 fixed this shape of miss one layer in: ``Engine._step`` diffed the
broker's rejection list around ``on_bar`` only, so a refusal recorded at *submit*
never reached that bar's record. :class:`~trading.divergence.ShadowBroker` had the
identical miss one layer out — it tracked every order it forwarded, refused or
not, and diffed rejections around the live ``on_bar`` alone.

The consequence is narrow and worth stating precisely: a refused order never fills
on either side, so it is never a paired fill and **the measured slippage does not
move**. What was wrong is the row. A duplicate the broker refused (ADR-0036) and an
order the venue vetoed (ADR-0041) both surfaced as ``pending`` — "still working at
the venue" — for orders the venue never received. That is the same rendering a
genuinely parked order gets, and the duplicate guard fires *precisely* when orders
are parked and unfilled, i.e. exactly when an operator is reading the pending count
to decide whether execution is healthy (``docs/monday-divergence-run.md``).

Everything here is offline: :class:`FakeAlpacaClient` scripts both refusals and
:class:`FakeClock` supplies the time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from trading.brokers.alpaca import AlpacaBroker
from trading.clock import FakeClock
from trading.config import RiskConfig
from trading.data.alpaca_client import FakeAlpacaClient
from trading.data.fake import FakeAdapter
from trading.divergence import (
    OUTCOME_FILLED,
    OUTCOME_PENDING,
    ShadowBroker,
    divergence_rows,
    render_report,
)
from trading.engine import Engine
from trading.interfaces import Broker
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Bar, Fill, Order, Portfolio, Side

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trading.divergence import FillDivergence

START = datetime(2026, 1, 5, tzinfo=UTC)

# The venue's own words for the refusal ADR-0041 met live, verbatim.
WASH_TRADE = (
    "potential wash trade detected. use complex orders (opposite side market/stop order exists)"
)


def _bars(symbol: str, opens: list[float]) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=START + timedelta(days=i),
            open=price,
            high=price * 1.03,
            low=price * 0.99,
            close=price * 1.02,
            volume=1_000,
        )
        for i, price in enumerate(opens)
    ]


def _slice(*bars: Bar) -> dict[str, Bar]:
    return {bar.symbol: bar for bar in bars}


def _clock(step_seconds: int = 60) -> FakeClock:
    base = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    return FakeClock(base, [base + timedelta(seconds=step_seconds * i) for i in range(1, 500)])


def _parked_then_duplicate() -> tuple[ShadowBroker, AlpacaBroker, list[Bar]]:
    """The ADR-0036 scenario, end to end: one order parks, the next is refused.

    Bar 1 places a ``BUY AAA`` the shut venue parks at ``accepted``. The account
    still reads flat, so bar 2's target-weight ask is the same order again — and
    the duplicate guard refuses it rather than stacking a second order at the
    venue. One order exists; two were asked for.
    """
    bars = _bars("AAA", [100.0, 101.0])
    client = FakeAlpacaClient({"AAA": bars}, cash=10_000.0, auto_fill=False)
    live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
    shadow = ShadowBroker(live, _clock())

    shadow.submit(Order("AAA", Side.BUY, qty=1))
    shadow.on_bar(_slice(bars[0]))
    shadow.submit(Order("AAA", Side.BUY, qty=1))  # refused: order 1 is still working
    shadow.on_bar(_slice(bars[1]))
    return shadow, live, bars


def _pending_rows(records: Sequence[FillDivergence]) -> list[dict[str, str]]:
    return [row for row in divergence_rows(records) if row["live_outcome"] == OUTCOME_PENDING]


class TestADuplicateRefusalIsNotPending:
    """ADR-0036: the broker refused it, so it never reached the venue."""

    def test_the_refused_duplicate_is_not_a_row_at_all(self) -> None:
        shadow, live, _ = _parked_then_duplicate()

        # One order was asked for twice and placed once, so there is one order to
        # compare -- not two.
        assert live.pending_order_ids == ("1",)
        assert len(shadow.divergences) == 1

    def test_only_the_order_the_venue_really_holds_reads_pending(self) -> None:
        shadow, _, _ = _parked_then_duplicate()

        rows = _pending_rows(shadow.divergences)
        assert len(rows) == 1, "a refused duplicate must not inflate the pending count"

    def test_the_summary_counts_one_order_not_two(self) -> None:
        shadow, _, _ = _parked_then_duplicate()

        summary = shadow.summary
        assert summary.orders == 1
        assert summary.submit_refusals == 1


class TestAVenueRefusalAtSubmitIsNotPending:
    """ADR-0041: the venue said no, so there is nothing working there either."""

    def _refused_sell(self) -> tuple[ShadowBroker, AlpacaBroker, list[Bar]]:
        bars = _bars("AAA", [100.0])
        client = FakeAlpacaClient({"AAA": bars}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        client.set_submit_refusal("AAA", WASH_TRADE, side=Side.SELL)
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock())

        shadow.submit(Order("AAA", Side.SELL, qty=1))
        shadow.on_bar(_slice(bars[0]))
        return shadow, live, bars

    def test_a_vetoed_order_produces_no_divergence_row(self) -> None:
        shadow, live, _ = self._refused_sell()

        assert live.pending_order_ids == ()
        assert shadow.divergences == []

    def test_it_is_not_reported_as_pending(self) -> None:
        shadow, _, _ = self._refused_sell()

        assert _pending_rows(shadow.divergences) == []


class TestTheRefusalIsReportedNotDropped:
    """Refusing to mislabel it must not become losing it."""

    def test_it_lands_on_the_shadow_with_the_venue_s_own_words(self) -> None:
        shadow, _, _ = self._wash_trade()

        assert len(shadow.submit_refusals) == 1
        _ts, order, reason = shadow.submit_refusals[0]
        assert order.symbol == "AAA"
        assert order.side is Side.SELL
        assert "wash trade" in reason

    def test_it_is_stamped_with_the_bar_it_happened_on(self) -> None:
        shadow, _, bars = self._wash_trade()

        # The first submit precedes any bar; a later one carries the last bar seen.
        shadow.submit(Order("AAA", Side.SELL, qty=1))
        assert shadow.submit_refusals[0][0] is None
        assert shadow.submit_refusals[1][0] == bars[0].ts

    def test_the_printed_report_names_it(self) -> None:
        shadow, _, _ = self._wash_trade()

        text = render_report(shadow.summary, shadow.divergences)
        assert "Refused at submit" in text
        assert "1" in text

    def test_the_report_stays_quiet_when_nothing_was_refused(self) -> None:
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        shadow = ShadowBroker(AlpacaBroker(client, clock=_clock()), _clock())
        shadow.submit(Order("AAA", Side.BUY, qty=1))
        shadow.on_bar(_slice(*_bars("AAA", [100.0])))

        assert "Refused at submit" not in render_report(shadow.summary, shadow.divergences)

    def test_the_broker_s_own_rejection_list_still_carries_it(self) -> None:
        """The end-of-run tally is untouched: this is a reporting fix, not a tally fix."""
        shadow, live, _ = self._wash_trade()

        assert len(live.rejections) == 1
        assert shadow.rejections == live.rejections  # pass-through, as ADR-0038 requires

    def _wash_trade(self) -> tuple[ShadowBroker, AlpacaBroker, list[Bar]]:
        bars = _bars("AAA", [100.0])
        client = FakeAlpacaClient({"AAA": bars}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        client.set_submit_refusal("AAA", WASH_TRADE, side=Side.SELL)
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock())
        shadow.submit(Order("AAA", Side.SELL, qty=1))
        shadow.on_bar(_slice(bars[0]))
        return shadow, live, bars


class TestAParkedOrderStillReportsAsPending:
    """The row that is *correct* must survive the fix that removes the wrong ones."""

    def test_the_parked_order_keeps_its_pending_row(self) -> None:
        shadow, _, _ = _parked_then_duplicate()

        record = shadow.divergences[0]
        assert record.live.outcome == OUTCOME_PENDING
        assert record.shadow.outcome == OUTCOME_FILLED
        assert record.outcome_diverged
        assert record.reference_price == pytest.approx(100.0)
        assert record.order.qty == pytest.approx(1.0)

    def test_the_two_cases_are_now_distinguishable(self) -> None:
        """The whole point: one signal for 'the venue is holding it', one for 'refused'."""
        shadow, _, _ = _parked_then_duplicate()

        summary = shadow.summary
        assert summary.orders == 1  # working at the venue
        assert summary.submit_refusals == 1  # never got there


class TestTheRejectionTallyIsUnchanged:
    """Nothing here may double-count a refusal into the run's totals."""

    def _run(self, wrap: bool) -> object:
        bars = _bars("AAA", [100.0, 101.0, 99.0, 103.0])
        client = FakeAlpacaClient({"AAA": bars}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        broker: Broker = ShadowBroker(live, _clock()) if wrap else live
        engine = Engine(FakeAdapter(bars), broker, Guardrails(RiskConfig.unlimited()))
        return engine.run(get_strategy("buy_and_hold"), ["AAA"], START, START + timedelta(days=10))

    def test_a_wrapped_run_rejects_exactly_what_an_unwrapped_one_does(self) -> None:
        assert self._run(wrap=True) == self._run(wrap=False)

    def test_the_refusals_are_counted_once(self) -> None:
        bars = _bars("AAA", [100.0, 101.0, 99.0, 103.0])
        client = FakeAlpacaClient({"AAA": bars}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        shadow = ShadowBroker(live, _clock())
        engine = Engine(FakeAdapter(bars), shadow, Guardrails(RiskConfig.unlimited()))

        result = engine.run(
            get_strategy("buy_and_hold"), ["AAA"], START, START + timedelta(days=10)
        )

        # buy_and_hold retries its unfilled entry every bar (ADR-0037), and the
        # duplicate guard refuses every retry -- so this fixture really does
        # exercise the path.
        assert len(shadow.submit_refusals) >= 1
        assert result.rejections == live.rejections
        assert len(result.rejections) == len(live.rejections)


class TestTheShadowStillCannotPerturbTheLivePath:
    """ADR-0038's structural guarantee, re-checked over the new bookkeeping."""

    def test_a_rejection_list_that_explodes_costs_a_report_not_an_order(self) -> None:
        """The refusal diff reads ``rejections`` *before* the live call.

        That read is the only new statement ahead of ``self._live.submit`` in the
        whole wrapper, so it is the one place the guarantee could have been lost.
        It sits inside ``try/except``, so a broker whose property raises disables
        the shadow and the order goes out anyway.
        """

        class _ExplodingRejections:
            def __init__(self) -> None:
                self.placed: list[Order] = []
                self._portfolio = Portfolio(cash=1_000.0)

            @property
            def portfolio(self) -> Portfolio:
                return self._portfolio

            @property
            def rejections(self) -> list[tuple[Order, str]]:
                raise RuntimeError("rejections exploded")

            def submit(self, order: Order) -> None:
                self.placed.append(order)

            def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
                _ = bars
                return []

        live = _ExplodingRejections()
        wrapped = ShadowBroker(live, _clock())

        wrapped.submit(Order("AAA", Side.BUY, qty=1))

        assert live.placed  # the order reached the venue
        assert not wrapped.enabled
        assert any("rejections exploded" in message for message in wrapped.errors)

    def test_a_refused_order_is_never_journaled(self) -> None:
        """ADR-0048: a row is written only once neither side can change it.

        A refused order has no row, so it must never reach the journal -- a file
        that under-reports is the invariant; one that writes a row it would want
        back is not.
        """

        class _RecordingJournal:
            def __init__(self) -> None:
                self.appended: list[FillDivergence] = []

            def append(self, records: Sequence[FillDivergence]) -> None:
                self.appended.extend(records)

        bars = _bars("AAA", [100.0])
        client = FakeAlpacaClient({"AAA": bars}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        client.set_submit_refusal("AAA", WASH_TRADE, side=Side.SELL)
        journal = _RecordingJournal()
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock(), journal=journal)

        shadow.submit(Order("AAA", Side.SELL, qty=1))
        shadow.on_bar(_slice(bars[0]))

        assert journal.appended == []
        assert shadow.enabled
