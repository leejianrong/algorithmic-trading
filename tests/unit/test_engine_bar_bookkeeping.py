"""The bar a refusal happened on must say so (ADR-0044, KAN-679).

``Engine._step`` snapshotted ``broker.rejections`` around ``on_bar`` only, so
everything a broker rejects at *settlement* — a venue-ended order (ADR-0033), an
underfunded simulated buy — reached that bar's :class:`~trading.engine.BarOutcome`
while everything it refuses at *submit* time did not. Submit time is exactly when
the two newest refusals fire: the duplicate-order guard (ADR-0036) and the venue's
own veto (ADR-0041). Both reached ``BacktestResult.rejections`` through
``_finalize``, and therefore ``result.json`` and the summary — but the per-bar log
a live operator actually watches showed nothing at all.

That log is the only real-time signal a ``--live`` paper session has, and
``cli._format_bar`` has always rendered ``outcome.broker_rejections``. So the one
place a duplicate refusal should appear as it happens was the one place it could
not.

Everything here is offline: :class:`FakeAlpacaClient` + :class:`FakeClock`, no
network and no key.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from trading.broker import SimulatedBroker
from trading.brokers.alpaca import AlpacaBroker
from trading.clock import FakeClock, ImmediateClock
from trading.config import CostConfig, RiskConfig
from trading.data.alpaca_client import AlpacaOrder, FakeAlpacaClient
from trading.data.fake import FakeAdapter
from trading.engine import BarOutcome, Engine, Feed, PaperSession, build_feed
from trading.interfaces import StrategyContext
from trading.report import result_to_dict
from trading.risk import Guardrails
from trading.types import Bar, Order, Portfolio, Side, TargetWeight

CASH = 10_000.0
PRICE = 100.0


def _series(symbol: str, bars: int) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(days=i),
            open=PRICE,
            high=PRICE,
            low=PRICE,
            close=PRICE,
            volume=1_000,
        )
        for i in range(bars)
    ]


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 2, 21, 0, tzinfo=UTC))


def _reasons(document: dict[str, Any]) -> list[str]:
    """The ``reason`` of every rejection in a ``result_to_dict`` document."""
    rejections = cast("list[dict[str, Any]]", document["rejections"])
    return [str(entry["reason"]) for entry in rejections]


class _ScriptedOrders:
    """Emit a fixed list of raw orders on every bar.

    Raw :class:`~trading.types.Order` intents pass straight through
    :func:`trading.sizing.size`, so what the broker sees is exactly what is written
    here — no weight arithmetic between the script and the assertion.
    """

    def __init__(self, orders: list[Order]) -> None:
        self._orders = orders

    def on_bar(
        self,
        ts: datetime,
        bars: dict[str, Bar],
        context: StrategyContext,
    ) -> list[Order | TargetWeight]:
        return [order for order in self._orders if order.symbol in bars]


class _WholeFeed:
    """A completed-bar feed that reveals a fixed script at once, then repeats it.

    Matching :class:`~trading.data.recent_window.RecentWindowFeed`, which re-returns
    its whole window on every poll; ``PaperSession`` is what de-duplicates.
    """

    def __init__(self, feed: Feed) -> None:
        self._feed = feed

    def poll(self, symbols: list[str], lookback: int) -> Feed:
        return list(self._feed)


class _ParkingClient(FakeAlpacaClient):
    """A venue that accepts every order and parks it ``accepted``, never filling.

    The market-closed branch ADR-0036 drove live. The parked order leaves the
    account flat, the portfolio reconciles from that flat account (ADR-0020), and
    so the strategy's target reads unmet on every following bar — which is what
    made the broker resubmit before the duplicate guard existed.
    """

    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        super().__init__(bars, cash=CASH, auto_fill=False)
        self.submitted: list[AlpacaOrder] = []

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        placed = super().submit_order(symbol, qty, side)
        parked = self.set_order_status(placed.id, "accepted")
        self.submitted.append(parked)
        return parked


def _session(
    client: FakeAlpacaClient,
    strategy: _ScriptedOrders,
    series: dict[str, list[Bar]],
) -> tuple[PaperSession, AlpacaBroker]:
    """A paper session over ``series``, driving the real ``Engine._step``.

    ``poll_timeout=0`` takes the broker's timeout branch on the first poll, so a
    parked order stays pending with no clock to script. ``warmup=False`` because
    this is a replay: every scripted bar is meant to be traded.
    """
    broker = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(seconds=0))
    symbols = sorted(series)
    engine = Engine(FakeAdapter([bar for bars in series.values() for bar in bars]), broker)
    session = PaperSession(
        engine,
        strategy,
        symbols,
        _WholeFeed(build_feed(series)),
        ImmediateClock(),
        warmup=False,
    )
    return session, broker


class TestSubmitTimeRefusalReachesItsOwnBar:
    """A duplicate refusal is a per-bar event, and the per-bar record must carry it."""

    def _run(self, bars: int = 3) -> tuple[PaperSession, AlpacaBroker, _ParkingClient]:
        series = {"AAA": _series("AAA", bars)}
        client = _ParkingClient(series)
        strategy = _ScriptedOrders([Order("AAA", Side.BUY, qty=10)])
        session, broker = _session(client, strategy, series)
        session.run(max_new_bars=bars)
        return session, broker, client

    def test_the_refusal_appears_in_that_bars_outcome(self) -> None:
        session, _broker, client = self._run()

        # One order reached the venue; every later bar was refused by the guard.
        assert len(client.submitted) == 1
        first, *later = session.session_log
        assert first.broker_rejections == []
        for outcome in later:
            reasons = [reason for _order, reason in outcome.broker_rejections]
            assert reasons, f"bar {outcome.ts.date()} recorded no refusal"
            assert all("still working at the venue" in reason for reason in reasons)

    def test_the_refusal_names_the_order_it_refused(self) -> None:
        # (Order, reason), the shape SimulatedBroker and result.json already use —
        # never the venue id string the broker briefly recorded (ADR-0036).
        session, _broker, _client = self._run(bars=2)

        order, _reason = session.session_log[1].broker_rejections[0]
        assert isinstance(order, Order)
        assert (order.symbol, order.side, order.qty) == ("AAA", Side.BUY, 10)

    def test_a_refused_order_is_not_reported_as_submitted(self) -> None:
        session, _broker, _client = self._run(bars=2)

        assert [o.symbol for o in session.session_log[0].submitted] == ["AAA"]
        # It was refused, not submitted. Saying otherwise makes the per-bar log
        # disagree with the venue about what was placed.
        assert session.session_log[1].submitted == []

    def test_settlement_rejections_still_reach_their_bar(self) -> None:
        # The pre-existing on_bar snapshot must survive the change: a venue-ended
        # order (ADR-0033) is still reported on the bar it settled on.
        series = {"AAA": _series("AAA", 2)}
        client = FakeAlpacaClient(series, cash=CASH, auto_fill=False)
        session, _broker = _session(
            client, _ScriptedOrders([Order("AAA", Side.BUY, qty=10)]), series
        )

        # Bar 1 submits; before bar 2 polls it, the venue cancels it.
        session.run(max_new_bars=1)
        client.set_order_status("1", "canceled")
        session.run(max_new_bars=2)

        reasons = [reason for _o, reason in session.session_log[1].broker_rejections]
        assert any("canceled" in reason for reason in reasons)


class TestOneBarWithRefusalsAndAcceptances:
    """Per-order granularity: one refusal on a bar must not indict its siblings."""

    def _run(self) -> tuple[PaperSession, FakeAlpacaClient]:
        series = {name: _series(name, 1) for name in ("AAA", "BBB", "CCC")}
        client = FakeAlpacaClient(series, cash=CASH, auto_fill=False)
        # The venue refuses exactly one of the three (ADR-0041's live finding: it
        # answers a specific order, not the bar).
        client.set_submit_refusal("BBB", "potential wash trade detected. use complex orders")
        strategy = _ScriptedOrders(
            [
                Order("AAA", Side.BUY, qty=10),
                Order("BBB", Side.BUY, qty=10),
                Order("CCC", Side.BUY, qty=10),
            ]
        )
        session, _broker = _session(client, strategy, series)
        session.run(max_new_bars=1)
        return session, client

    def test_only_the_refused_order_is_missing_from_submitted(self) -> None:
        session, _client = self._run()

        outcome = session.session_log[0]
        assert [o.symbol for o in outcome.submitted] == ["AAA", "CCC"]

    def test_the_refusal_is_reported_once_and_names_its_symbol(self) -> None:
        session, _client = self._run()

        outcome = session.session_log[0]
        assert len(outcome.broker_rejections) == 1
        order, reason = outcome.broker_rejections[0]
        assert order.symbol == "BBB"
        assert "wash trade" in reason  # the venue's words, verbatim


class TestNoDoubleCounting:
    """``BarOutcome`` is reporting; the run's rejection total must not move."""

    def test_the_runs_rejection_total_is_the_brokers_own_list(self) -> None:
        series = {"AAA": _series("AAA", 4)}
        client = _ParkingClient(series)
        strategy = _ScriptedOrders([Order("AAA", Side.BUY, qty=10)])
        session, broker = _session(client, strategy, series)
        result = session.run(max_new_bars=4)

        # ``_finalize`` merges the broker's whole list once; the per-bar copies must
        # not be fed back into ``state.rejections`` on top of it.
        assert len(broker.rejections) == 3  # bars 2..4 refused
        assert len(result.rejections) == len(broker.rejections)
        assert result.rejections == broker.rejections
        # And the per-bar view accounts for exactly the same events, no more.
        per_bar = [r for outcome in session.session_log for r in outcome.broker_rejections]
        assert per_bar == broker.rejections


class TestTheOperatorActuallySeesIt:
    """The point of the fix: the live per-bar line lights up, with no CLI change.

    ``cli._format_bar`` already renders ``outcome.broker_rejections`` — it always
    did, which is what made the missing snapshot a reporting bug rather than a
    missing feature. This pins that the two halves meet.
    """

    def test_the_paper_status_line_shows_the_refusal(self) -> None:
        from trading.cli import _format_bar
        from trading.frequency import DAILY

        series = {"AAA": _series("AAA", 2)}
        client = _ParkingClient(series)
        strategy = _ScriptedOrders([Order("AAA", Side.BUY, qty=10)])
        session, _broker = _session(client, strategy, series)
        session.run(max_new_bars=2)

        line = _format_bar(session.session_log[1], DAILY)

        assert "REJECT BUY AAA" in line
        assert "still working at the venue" in line


class TestBacktestIsByteIdentical:
    """``Engine.run`` must not move. Pinned, not argued.

    :class:`~trading.broker.SimulatedBroker` queues on ``submit`` and rejects only
    inside ``on_bar``, so the submit-loop diff this change adds is empty on every
    backtest bar. The golden below is the SHA-256 over the canonical
    ``result_to_dict`` documents of **two** runs — one under the default guardrails
    (fills, a clamp, capped-out rejections) and one unconstrained with an order
    that cannot be funded, which is the only way to reach ``SimulatedBroker``'s own
    rejection path with the caps on. Computed on ``origin/main`` @ b6399f0, i.e.
    with the fix reverted.

    **Updated once, deliberately, by ADR-0057** (market selection): ``result_to_dict``
    gained one additive top-level key, ``market``, so these bytes moved while the run
    did not. That claim is not asserted by assertion — the second test below removes
    that single key and reproduces the *original* digest exactly, so the golden
    remains a pin on ``Engine.run`` rather than a hash somebody re-blessed.
    """

    GOLDEN = "9395b4f2e46134da00a66b53b3d8a4e2ba6a632b991f2d716be8a8e450fcc221"

    # The same two documents before ADR-0057's additive ``market`` key existed
    # (origin/main @ a157123). Reachable today by deleting that one key.
    GOLDEN_WITHOUT_MARKET = "c5a97cfc012baa1a7e56174a55e463ffdfccfb7b06928943974dd53045a02012"

    def _bars(self) -> list[Bar]:
        # Rising, so a repeated fixed-quantity buy eventually runs out of room.
        return [
            Bar(
                symbol="AAA",
                ts=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(days=i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=1_000,
            )
            for i in range(8)
        ]

    def _run(self, qty: float, *, guardrails: Guardrails | None) -> dict[str, Any]:
        bars = self._bars()
        broker = SimulatedBroker(Portfolio(cash=CASH), CostConfig())
        engine = Engine(FakeAdapter(bars), broker, guardrails)
        result = engine.run(
            _ScriptedOrders([Order("AAA", Side.BUY, qty=qty)]),
            ["AAA"],
            bars[0].ts,
            bars[-1].ts,
        )
        return result_to_dict(result, mode="backtest")

    def _documents(self) -> tuple[dict[str, Any], dict[str, Any]]:
        guarded = self._run(20, guardrails=None)  # default enforced caps
        unguarded = self._run(1_000, guardrails=Guardrails(RiskConfig.unlimited()))
        return guarded, unguarded

    def test_the_canonical_documents_match_the_pre_change_golden(self) -> None:
        raw = json.dumps(self._documents(), sort_keys=True).encode()
        assert hashlib.sha256(raw).hexdigest() == self.GOLDEN

    def test_the_only_change_since_that_golden_is_the_additive_market_key(self) -> None:
        """ADR-0057 added one key and moved nothing else — proved, not asserted.

        Deleting ``market`` from both documents must reproduce the digest taken
        before that key existed. If any *value* had drifted — a fill price, an
        exposure, a rejection reason — this would not come back to the old hash, so
        the golden above is still pinning the run and not merely re-blessed bytes.
        """
        documents = self._documents()
        for document in documents:
            assert document.pop("market") == "us_equity"

        raw = json.dumps(documents, sort_keys=True).encode()
        assert hashlib.sha256(raw).hexdigest() == self.GOLDEN_WITHOUT_MARKET

    def test_the_fixture_really_exercises_both_reject_paths(self) -> None:
        # A golden over a run that never rejects or clamps would prove nothing
        # about this change, so assert the fixture is not vacuous.
        guarded, unguarded = self._documents()

        assert guarded["fills"], "guarded run must fill something"
        assert guarded["clamps"], "guarded run must clamp"
        assert all("cap" in reason for reason in _reasons(guarded))
        # The broker's own path, unreachable while the caps veto first.
        assert _reasons(unguarded), "unguarded run must be rejected by the broker"
        assert all("insufficient cash" in reason for reason in _reasons(unguarded))


def test_bar_outcome_shape_is_unchanged() -> None:
    # This slice is bookkeeping accuracy only: no new field, no schema bump.
    assert [f for f in BarOutcome.__dataclass_fields__] == [
        "ts",
        "fills",
        "intents",
        "submitted",
        "clamps",
        "guardrail_rejections",
        "broker_rejections",
        "halted_now",
        "halted",
        "equity",
        "exposure",
        "resumed_now",
    ]


def test_price_fixture_is_flat_so_qty_and_value_agree() -> None:
    # Guards the arithmetic the other tests lean on: 10 shares at $100 is 10% of
    # $10,000, comfortably inside the default position and gross caps, so nothing
    # here is silently a clamp test.
    assert pytest.approx(0.10 * CASH) == 10 * PRICE
