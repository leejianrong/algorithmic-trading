"""Paper-vs-simulated fill divergence: is the modelled 5 bps real? (ADR-0038)

Every number a backtest reports rests on one unverified assumption: that
:class:`~trading.broker.SimulatedBroker`'s fill model — next bar's open, moved
against you by :attr:`~trading.config.CostConfig.slippage_bps` (5 bps by default),
plus commission — is roughly what a venue would actually have done. No backtest can
check that; it *is* the model. Paper trading can, because the same order goes to a
real venue and comes back with a real price.

This module runs that check. :class:`ShadowBroker` wraps the live broker, forwards
every :class:`~trading.types.Order` to it untouched, and *alongside* replays the
same orders against a throwaway :class:`~trading.broker.SimulatedBroker` seeded
from the live book, over the same bars. Each order therefore gets two answers, and
the difference is the divergence: fill price, slippage in basis points, observation
latency, and — just as importantly — a rejection on one side against a fill on the
other.

**The counterfactual.** For an order submitted while processing bar *t*, the
reference price is the **open of the next bar the feed serves for that symbol**
(bar *t+1*), because that is precisely what ``SimulatedBroker`` fills at (ADR-0001,
ADR-0004). The modelled fill is that open moved adversely by ``slippage_bps``; the
realized fill is whatever the venue reported. Both are compared against the *same*
reference open, so ``realized - modelled`` is a like-for-like statement about the
cost model and not about which bar was picked.

**The price notion** (ADR-0021 — the trap here). The paper/live feed serves RAW
actual quotes and the venue fills in raw dollars, so both sides of the comparison
are raw and the arithmetic is meaningful. A backtest feed serves adjusted prices,
and there both sides are adjusted. What must never happen is a raw fill measured
against an adjusted open; the notion is a property of the feed the wrapper is
handed, so it is carried on the summary as a label and never mixed within a run.

**Latency** is *observation* latency: the injected :class:`~trading.clock.Clock`
instant at :meth:`ShadowBroker.submit` to the instant the settlement was seen inside
:meth:`ShadowBroker.on_bar`. It is an upper bound on the venue's own fill latency,
since a submit-then-poll broker (ADR-0020) can only *notice* a fill during a poll.
It is measured on the clock, never ``time.time()``, so it is deterministic under
:class:`~trading.clock.FakeClock` — and it is only meaningful under a real clock: an
offline ``--once`` replay drains every bar inside a single poll, so its latencies
are near zero by construction.

**An order the broker refused is not an order at the venue** (ADR-0050). A broker
declines at two moments — at settlement, inside ``on_bar``, and at *submit*, where
the duplicate-order guard (ADR-0036) and the venue's own veto (ADR-0041) live. Only
the first was ever watched here, so a refused order was tracked like any other,
never settled, and was reported as :data:`OUTCOME_PENDING` — the same rendering a
genuinely parked order gets, for an order the venue never received. It is now
detected by diffing the live rejection list around the live ``submit``, per order,
and kept out of the comparison entirely: no tracked row, no ``pending``, no journal
row to retract. It is counted on :attr:`ShadowBroker.submit_refusals` and printed,
because the end-of-run tally already carries every refusal and this report must not
be the one place they go quiet. Nothing about the measurement moves — a refused
order never filled on either side, so it was never a paired fill.

**The shadow can never perturb the live path.** Three structural properties, each
covered by a test:

1. The live call happens **first and unguarded** in both :meth:`ShadowBroker.submit`
   and :meth:`ShadowBroker.on_bar`. The one statement that precedes it — reading
   the live rejection count so the refusal above can be told from an older one —
   is itself inside ``try/except``, and the live call is reached on every path.
2. Every line of shadow work sits inside ``try/except Exception``. A failure
   records a message on :attr:`ShadowBroker.errors`, permanently disables the
   shadow, and returns the live result unchanged. A bug here costs a report, never
   an order.
3. The counterfactual broker is built on a **copy** of the live portfolio, holds no
   client, and is thrown away every bar. It has nothing to submit an order with and
   nothing of the live book to mutate.

Because the wrapper is a plain :class:`~trading.interfaces.Broker` decorator, the
engine is untouched and there is no ``if paper:`` anywhere: ADR-0002's one execution
path stays one path. Wrapping a ``SimulatedBroker`` in a backtest is legal and
reports zero divergence, which is the mechanism's own null test.

**The rows are written as they settle** (ADR-0048). The comparison is the whole
point of a live session and it is not reconstructible from anything else the run
leaves behind, so :class:`DivergenceJournal` appends each row to
``fill_divergence.csv`` the moment both sides have answered, and the end-of-run
:func:`write_divergence_csv` replaces that file atomically with the canonical one.
A session that dies without finalizing — ``SIGKILL``, power loss, a suspended
laptop, an unhandled exception — therefore keeps every settled row instead of all
of them. A row is journaled only from :meth:`ShadowBroker._harvest`, i.e. only once
*neither side can change it again*: an order still parked at the venue (ADR-0036)
and a fill the venue is about to amend into a partial (ADR-0033) are both still
open at that point, so the file never contains a row it will later contradict.
Journal I/O is shadow work like any other — it runs after the live call, inside the
same ``try/except``, and a full disk or an unwritable path disables the shadow
rather than costing an order.
"""

from __future__ import annotations

import csv
import os
import statistics
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import IO, TYPE_CHECKING, Protocol, cast

from trading.broker import SimulatedBroker
from trading.config import CostConfig
from trading.types import Fill, Order, Portfolio, Side

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from trading.clock import Clock
    from trading.interfaces import Broker
    from trading.types import Bar

# How an order ended, on either side of the comparison.
OUTCOME_FILLED = "filled"
OUTCOME_PARTIAL = "partial"  # live only: a terminal-unfilled order that got some (ADR-0033)
OUTCOME_REJECTED = "rejected"
OUTCOME_PENDING = "pending"  # never settled before the session ended

# Paired fills needed before the realized-slippage average says anything about the
# model. Same spirit as ADR-0029's trades-per-parameter warning: a handful of fills
# is an anecdote, and the report says so rather than quoting a confident mean.
MIN_PAIRED_FILLS = 30

# Price notions a comparison can be conducted in (ADR-0021). Labels only — the
# wrapper never converts between them, it records which one the feed was serving.
NOTION_RAW = "raw"
NOTION_ADJUSTED = "adjusted"


@dataclass(frozen=True, slots=True)
class Settlement:
    """What one side (live venue or counterfactual model) did with one order.

    ``ts`` is the bar the settlement was observed on and ``observed_at`` the clock
    instant, which is what makes latency measurable without a wall clock. Price and
    quantity are ``None`` for anything that did not trade.
    """

    outcome: str
    ts: datetime | None = None
    observed_at: datetime | None = None
    qty: float | None = None
    price: float | None = None
    commission: float | None = None
    reason: str | None = None


# The "no answer yet" settlement, used for a side that never resolved.
PENDING = Settlement(OUTCOME_PENDING)


@dataclass(frozen=True, slots=True)
class FillDivergence:
    """One order, both answers, and the gap between them.

    ``reference_price`` is the open of the first bar the feed served for the symbol
    after submission — the price ``SimulatedBroker`` prices against, and the common
    denominator for both slippage figures. It is ``None`` only when the run ended
    before any such bar arrived.
    """

    order: Order
    submitted_at: datetime
    submitted_ts: datetime | None
    reference_price: float | None
    live: Settlement
    shadow: Settlement

    @property
    def symbol(self) -> str:
        return self.order.symbol

    @property
    def side(self) -> Side:
        return self.order.side

    def _adverse_bps(self, price: float | None) -> float | None:
        """Slippage in bps, signed so that **positive is worse for us**.

        A buy filled above the reference and a sell filled below it both cost
        money, so both come out positive; a price improvement is negative.
        """
        reference = self.reference_price
        if reference is None or reference <= 0.0 or price is None:
            return None
        raw = (price / reference - 1.0) * 10_000.0
        return raw if self.order.side is Side.BUY else -raw

    @property
    def realized_slippage_bps(self) -> float | None:
        """What the venue actually charged us, in bps against the model's reference."""
        return self._adverse_bps(self.live.price)

    @property
    def modelled_slippage_bps(self) -> float | None:
        """What the cost model assumed — ``CostConfig.slippage_bps`` by construction."""
        return self._adverse_bps(self.shadow.price)

    @property
    def slippage_error_bps(self) -> float | None:
        """Realized minus modelled. Positive means the backtest was optimistic."""
        realized = self.realized_slippage_bps
        modelled = self.modelled_slippage_bps
        if realized is None or modelled is None:
            return None
        return realized - modelled

    @property
    def price_difference(self) -> float | None:
        """Live fill price minus modelled fill price, in dollars."""
        if self.live.price is None or self.shadow.price is None:
            return None
        return self.live.price - self.shadow.price

    @property
    def qty_divergence(self) -> float | None:
        """Live filled quantity minus modelled — non-zero on a partial fill."""
        if self.live.qty is None or self.shadow.qty is None:
            return None
        return self.live.qty - self.shadow.qty

    @property
    def latency(self) -> timedelta | None:
        """Submit → live settlement observed, on the injected clock."""
        if self.live.observed_at is None:
            return None
        return self.live.observed_at - self.submitted_at

    @property
    def modelled_latency(self) -> timedelta | None:
        """Submit → modelled settlement, which is always the very next bar."""
        if self.shadow.observed_at is None:
            return None
        return self.shadow.observed_at - self.submitted_at

    @property
    def outcome_diverged(self) -> bool:
        """Whether the two sides disagree about *what happened*, not just the price.

        A venue rejection against a modelled fill (or an order still parked at the
        venue while the model filled it at the next open, ADR-0036) is the single
        most consequential kind of divergence, and it is reported as a row rather
        than dropped for having no price to compare.
        """
        return self.live.outcome != self.shadow.outcome

    @property
    def comparable(self) -> bool:
        """Both sides filled and a reference open exists — the row the stats use."""
        return (
            self.live.outcome in (OUTCOME_FILLED, OUTCOME_PARTIAL)
            and self.shadow.outcome == OUTCOME_FILLED
            and self.reference_price is not None
        )


@dataclass(frozen=True, slots=True)
class DivergenceSummary:
    """Aggregate answer, plus an explicit statement of what it cannot support."""

    price_notion: str
    modelled_slippage_bps: float
    modelled_commission_per_share: float
    orders: int
    comparable: int
    outcome_divergences: int
    live_only_fills: int
    model_only_fills: int
    mean_realized_bps: float | None
    median_realized_bps: float | None
    max_realized_bps: float | None
    stdev_realized_bps: float | None
    mean_error_bps: float | None
    mean_latency: timedelta | None
    max_latency: timedelta | None
    unmatched_live_fills: int = 0
    unmatched_live_rejections: int = 0
    submit_refusals: int = 0
    errors: tuple[str, ...] = ()
    min_samples: int = MIN_PAIRED_FILLS

    @property
    def conclusive(self) -> bool:
        """Whether there are enough paired fills to say anything about the model."""
        return self.comparable >= self.min_samples

    @property
    def implied_slippage_bps(self) -> float | None:
        """The slippage the observed fills imply — what to set the model to.

        Only an estimate, and only worth acting on when :attr:`conclusive`.
        """
        return self.mean_realized_bps


def summarize(
    records: Sequence[FillDivergence],
    *,
    costs: CostConfig | None = None,
    price_notion: str = NOTION_RAW,
    errors: Sequence[str] = (),
    unmatched_live_fills: int = 0,
    unmatched_live_rejections: int = 0,
    submit_refusals: int = 0,
) -> DivergenceSummary:
    """Aggregate divergence rows into a :class:`DivergenceSummary`."""
    config = costs or CostConfig()
    comparable = [r for r in records if r.comparable]
    realized = [bps for r in comparable if (bps := r.realized_slippage_bps) is not None]
    error_bps = [bps for r in comparable if (bps := r.slippage_error_bps) is not None]
    latencies = [lat for r in records if (lat := r.latency) is not None]

    return DivergenceSummary(
        price_notion=price_notion,
        modelled_slippage_bps=config.slippage_bps,
        modelled_commission_per_share=config.commission_per_share,
        orders=len(records),
        comparable=len(comparable),
        outcome_divergences=sum(1 for r in records if r.outcome_diverged),
        live_only_fills=sum(
            1
            for r in records
            if r.live.outcome in (OUTCOME_FILLED, OUTCOME_PARTIAL)
            and r.shadow.outcome != OUTCOME_FILLED
        ),
        model_only_fills=sum(
            1
            for r in records
            if r.shadow.outcome == OUTCOME_FILLED
            and r.live.outcome not in (OUTCOME_FILLED, OUTCOME_PARTIAL)
        ),
        mean_realized_bps=statistics.fmean(realized) if realized else None,
        median_realized_bps=statistics.median(realized) if realized else None,
        max_realized_bps=max(realized) if realized else None,
        stdev_realized_bps=statistics.stdev(realized) if len(realized) > 1 else None,
        mean_error_bps=statistics.fmean(error_bps) if error_bps else None,
        mean_latency=(
            timedelta(seconds=statistics.fmean(lat.total_seconds() for lat in latencies))
            if latencies
            else None
        ),
        max_latency=max(latencies) if latencies else None,
        unmatched_live_fills=unmatched_live_fills,
        unmatched_live_rejections=unmatched_live_rejections,
        submit_refusals=submit_refusals,
        errors=tuple(errors),
    )


class DivergenceSink(Protocol):
    """Where settled rows go as they close (ADR-0048).

    Narrow on purpose: :class:`ShadowBroker` hands over rows and learns nothing
    about files. :class:`DivergenceJournal` is the implementation the CLI uses; a
    test injects one that raises, which is the only way to prove that adding file
    I/O to the shadow path still cannot perturb the live one.
    """

    def append(self, records: Sequence[FillDivergence]) -> None:
        """Persist ``records``. May raise; the caller treats that as shadow failure."""
        ...


@dataclass(slots=True)
class _Tracked:
    """One in-flight order and whatever each side has said about it so far."""

    order: Order
    submitted_at: datetime
    submitted_ts: datetime | None
    reference_price: float | None = None
    live: Settlement | None = None
    shadow: Settlement | None = None


class ShadowBroker:
    """A :class:`~trading.interfaces.Broker` decorator that measures the fill model.

    Delegates ``portfolio`` / ``submit`` / ``on_bar`` / ``rejections`` to the live
    broker verbatim, so the engine cannot tell it is there, and runs a counterfactual
    :class:`~trading.broker.SimulatedBroker` on the side. See the module docstring
    for the counterfactual definition, the price-notion rule, and the three
    structural reasons the shadow cannot perturb the live path.

    ``shadow_factory`` builds the counterfactual broker from a snapshot of the live
    portfolio; it defaults to ``SimulatedBroker(snapshot, costs)`` and exists so a
    test can inject a broker that misbehaves (and prove the guard) without
    monkeypatching.

    ``journal``, when given, receives each row the moment it settles (ADR-0048), so
    a session killed without finalizing keeps the measurement it has already made.
    """

    def __init__(
        self,
        live: Broker,
        clock: Clock,
        *,
        costs: CostConfig | None = None,
        price_notion: str = NOTION_RAW,
        shadow_factory: Callable[[Portfolio], Broker] | None = None,
        journal: DivergenceSink | None = None,
    ) -> None:
        self._live = live
        self._clock = clock
        self._costs = costs or CostConfig()
        self._price_notion = price_notion
        self._shadow_factory = shadow_factory
        self._journal = journal
        # How much of ``_closed`` the journal has already been handed. Advanced only
        # after a successful append, so a writer that raises re-offers the same rows
        # rather than losing them (the shadow is disabled by then, so it never does).
        self._journaled = 0
        self._tracked: list[_Tracked] = []
        self._closed: list[FillDivergence] = []
        self._last_bar_ts: datetime | None = None
        self._enabled = True
        self.errors: list[str] = []
        # Settlements the venue reported that no submission could be attributed to.
        # Surfaced, never silently dropped: an unattributable fill means the
        # attribution rule below is wrong, which is worth knowing.
        self.unmatched_live_fills: list[tuple[datetime | None, Fill]] = []
        self.unmatched_live_rejections: list[tuple[datetime | None, Order, str]] = []
        # Orders the live broker refused at submit, so they never reached the venue
        # and have no counterfactual to compare (ADR-0050). Kept apart from
        # ``unmatched_live_rejections``, which means "our attribution rule is
        # wrong": these are attributed exactly, to an order deliberately not
        # tracked. Reported as a count, never as a row.
        self.submit_refusals: list[tuple[datetime | None, Order, str]] = []

    # -- Broker seam (pure delegation on the live path) --

    @property
    def portfolio(self) -> Portfolio:
        """The live broker's portfolio — the shadow never owns the book."""
        return self._live.portfolio

    @property
    def rejections(self) -> list[tuple[Order, str]]:
        """The live broker's rejection list, unchanged.

        :class:`~trading.engine.Engine` reads this through ``getattr`` and merges it
        into ``BacktestResult.rejections`` (ADR-0036); passing the live list straight
        through is what keeps a wrapped run identical to an unwrapped one.
        """
        live_rejections = getattr(self._live, "rejections", None)
        if isinstance(live_rejections, list):
            return cast("list[tuple[Order, str]]", live_rejections)
        return []

    def submit(self, order: Order) -> None:
        """Place ``order`` on the live broker, then record it for comparison.

        **Unless the broker refused it** (ADR-0050). A broker declines at two
        moments and only the settlement one used to be watched: the duplicate-order
        guard (ADR-0036) and the venue's own veto (ADR-0041) both record on
        ``rejections`` from inside ``submit``, before the order exists anywhere.
        Such an order is not tracked at all — tracking it would leave a row nothing
        can ever settle, which reads as :data:`OUTCOME_PENDING`, i.e. as an order
        working at a venue that never received it. It lands on
        :attr:`submit_refusals` instead, so it is reported rather than dropped.

        The refusal is detected by diffing the live broker's rejection list around
        the live call, per order — the same shape ``Engine._step`` uses (ADR-0044),
        and per order rather than per bar for the same reason: the venue refuses a
        *specific* order, so a bar with one refusal and two acceptances must say so.

        The pre-call read is the only shadow statement that has ever run ahead of
        the live submit, so it is guarded like every other: a broker whose
        ``rejections`` raises disables the shadow, and the live call is still
        reached on every path. ADR-0038's ordering guarantee is intact.
        """
        before: int | None = None
        if self._enabled:
            try:
                before = len(self.rejections)
            except Exception as exc:
                self._disable("submit", exc)

        self._live.submit(order)  # LIVE FIRST, UNGUARDED: nothing here can stop it.

        if not self._enabled or before is None:
            return
        try:
            refused = self.rejections[before:]
            if refused:
                self.submit_refusals.extend(
                    (self._last_bar_ts, refused_order, reason) for refused_order, reason in refused
                )
                return
            self._tracked.append(
                _Tracked(
                    order=order,
                    submitted_at=self._clock.now(),
                    submitted_ts=self._last_bar_ts,
                )
            )
        except Exception as exc:  # a broken shadow must never break a real order
            self._disable("submit", exc)

    def on_bar(self, bars: dict[str, Bar]) -> list[Fill]:
        """Run the live broker, then replay the same orders through the model."""
        snapshot: Portfolio | None = None
        if self._enabled:
            try:
                snapshot = _copy_portfolio(self._live.portfolio)
            except Exception as exc:
                self._disable("snapshot", exc)

        before = len(self.rejections)
        live_fills = self._live.on_bar(bars)  # THE LIVE PATH. Everything else is extra.
        new_rejections = list(self.rejections[before:])

        if not self._enabled or snapshot is None:
            return live_fills
        try:
            self._observe(bars, live_fills, new_rejections, snapshot)
        except Exception as exc:
            self._disable("on_bar", exc)
        return live_fills

    # -- the comparison --

    @property
    def enabled(self) -> bool:
        """``False`` once the shadow has failed and switched itself off."""
        return self._enabled

    @property
    def divergences(self) -> list[FillDivergence]:
        """Every tracked order: settled ones in settlement order, then the open ones.

        Not submission order — an order the venue took three bars to answer closes
        after one submitted later. Each row carries ``submitted_at`` and
        ``submitted_ts``, so a caller that wants submission order can sort on them.

        Safe to read at any point: an order only one side has answered is emitted
        with :data:`PENDING` on the other, which is itself a divergence worth
        reporting (an order parked at the venue while the model filled it, ADR-0036).

        Only orders the live broker *accepted* are here. One it refused at submit
        never reached the venue, so it can never settle and a row for it would be a
        permanent, indistinguishable ``pending`` (ADR-0050); those are counted on
        :attr:`submit_refusals` instead.
        """
        return [*self._closed, *(self._close(t) for t in self._tracked)]

    @property
    def summary(self) -> DivergenceSummary:
        """The aggregate answer for the orders tracked so far."""
        return summarize(
            self.divergences,
            costs=self._costs,
            price_notion=self._price_notion,
            errors=self.errors,
            unmatched_live_fills=len(self.unmatched_live_fills),
            unmatched_live_rejections=len(self.unmatched_live_rejections),
            submit_refusals=len(self.submit_refusals),
        )

    # -- internals --

    def _disable(self, where: str, exc: Exception) -> None:
        """Record a shadow failure and switch the shadow off for the rest of the run.

        Off, not retried: a shadow that raised once will very likely raise every bar,
        and a live session must not pay that cost repeatedly. The live path is
        already complete by the time this runs.
        """
        self._enabled = False
        self.errors.append(f"{where}: {type(exc).__name__}: {exc}")

    def _observe(
        self,
        bars: dict[str, Bar],
        live_fills: list[Fill],
        live_rejections: list[tuple[Order, str]],
        snapshot: Portfolio,
    ) -> None:
        now = self._clock.now()
        ts = max((bar.ts for bar in bars.values()), default=self._last_bar_ts)

        # 1. The reference open: the price the model prices against. Captured for
        #    every tracked order the moment its symbol first appears, independently
        #    of what either side did, so a rejected order still has a reference.
        for tracked in self._tracked:
            bar = bars.get(tracked.order.symbol)
            if tracked.reference_price is None and bar is not None:
                tracked.reference_price = bar.open

        self._run_shadow(bars, snapshot, ts, now)
        self._attribute_live(live_fills, live_rejections, ts, now)
        self._harvest()
        self._last_bar_ts = ts
        # Last, and only on rows ``_harvest`` has closed: a closed row is final, so
        # the file on disk never contradicts itself (ADR-0048). Bookkeeping above is
        # already consistent if this raises, so the disabled shadow still reports
        # every row it measured through ``divergences``.
        self._flush_journal()

    def _run_shadow(
        self,
        bars: dict[str, Bar],
        snapshot: Portfolio,
        ts: datetime | None,
        now: datetime,
    ) -> None:
        """Replay every unsettled order on a throwaway broker over the same bars.

        The broker is rebuilt each bar from a copy of the *pre-bar* live book, so
        each order is judged against the book that really existed when it would have
        executed — an early divergence cannot poison every later comparison, and the
        shadow can never touch the live portfolio because it never holds it.
        """
        outstanding = [t for t in self._tracked if t.shadow is None]
        if not outstanding:
            return
        shadow = self._make_shadow(snapshot)
        for tracked in outstanding:
            shadow.submit(tracked.order)
        fills = shadow.on_bar(bars)
        rejections = cast("list[tuple[Order, str]]", getattr(shadow, "rejections", []))

        # SimulatedBroker walks its queue in submission order and appends to `fills`
        # and `rejections` in that order, recording the very Order object it was
        # given. Replaying the same walk therefore attributes each outcome exactly,
        # with no name-matching heuristic (pinned by a test).
        fill_index = 0
        rejection_index = 0
        for tracked in outstanding:
            if tracked.order.symbol not in bars:
                continue  # unpriceable this bar; the model keeps it queued too.
            pending_rejection = (
                rejections[rejection_index] if rejection_index < len(rejections) else None
            )
            if pending_rejection is not None and pending_rejection[0] is tracked.order:
                tracked.shadow = Settlement(
                    OUTCOME_REJECTED,
                    ts=ts,
                    observed_at=now,
                    reason=pending_rejection[1],
                )
                rejection_index += 1
                continue
            if fill_index < len(fills):
                fill = fills[fill_index]
                fill_index += 1
                tracked.shadow = Settlement(
                    OUTCOME_FILLED,
                    ts=ts,
                    observed_at=now,
                    qty=fill.qty,
                    price=fill.price,
                    commission=fill.commission,
                )

    def _make_shadow(self, snapshot: Portfolio) -> Broker:
        if self._shadow_factory is not None:
            return self._shadow_factory(snapshot)
        return SimulatedBroker(snapshot, self._costs)

    def _attribute_live(
        self,
        live_fills: list[Fill],
        live_rejections: list[tuple[Order, str]],
        ts: datetime | None,
        now: datetime,
    ) -> None:
        """Attach the venue's answers to the orders that asked for them.

        A :class:`~trading.types.Fill` carries no order id, so fills are attributed
        **FIFO within (symbol, side)** — the oldest unsettled order for that symbol
        and direction gets the fill. Rejections *do* carry the ``Order`` (ADR-0036),
        so they are attributed by identity, exactly.

        Fills are processed before rejections so that ADR-0033's partial-fill case —
        an order the venue cancelled after filling part of it emits both a ``Fill``
        and a rejection — lands on one row as :data:`OUTCOME_PARTIAL` rather than two
        half-rows.
        """
        for fill in live_fills:
            match = next(
                (
                    t
                    for t in self._tracked
                    if t.live is None
                    and t.order.symbol == fill.symbol
                    and t.order.side is fill.side
                ),
                None,
            )
            if match is None:
                self.unmatched_live_fills.append((ts, fill))
                continue
            match.live = Settlement(
                OUTCOME_FILLED,
                ts=ts,
                observed_at=now,
                qty=fill.qty,
                price=fill.price,
                commission=fill.commission,
            )

        for order, reason in live_rejections:
            unsettled = next(
                (t for t in self._tracked if t.live is None and t.order is order), None
            )
            if unsettled is not None:
                unsettled.live = Settlement(OUTCOME_REJECTED, ts=ts, observed_at=now, reason=reason)
                continue
            partial = next(
                (
                    t
                    for t in self._tracked
                    if t.order is order
                    and t.live is not None
                    and t.live.outcome == OUTCOME_FILLED
                    and t.live.ts == ts
                ),
                None,
            )
            if partial is not None and partial.live is not None:
                partial.live = replace(partial.live, outcome=OUTCOME_PARTIAL, reason=reason)
                continue
            self.unmatched_live_rejections.append((ts, order, reason))

    def _harvest(self) -> None:
        """Close out every order both sides have now answered."""
        still_open: list[_Tracked] = []
        for tracked in self._tracked:
            if tracked.live is not None and tracked.shadow is not None:
                self._closed.append(self._close(tracked))
            else:
                still_open.append(tracked)
        self._tracked = still_open

    def _flush_journal(self) -> None:
        """Hand every newly closed row to the journal, in settlement order.

        Only closed rows: an order the venue has not answered (ADR-0036's parked
        order) and a fill that is about to be amended into a partial (ADR-0033) are
        both still in ``_tracked``, so neither can reach the file as a row that a
        later bar contradicts. Rows that are still open when the session ends are
        written by the final :func:`write_divergence_csv` instead.
        """
        if self._journal is None or self._journaled == len(self._closed):
            return
        fresh = self._closed[self._journaled :]
        self._journal.append(fresh)
        self._journaled = len(self._closed)

    @staticmethod
    def _close(tracked: _Tracked) -> FillDivergence:
        return FillDivergence(
            order=tracked.order,
            submitted_at=tracked.submitted_at,
            submitted_ts=tracked.submitted_ts,
            reference_price=tracked.reference_price,
            live=tracked.live if tracked.live is not None else PENDING,
            shadow=tracked.shadow if tracked.shadow is not None else PENDING,
        )


def _copy_portfolio(portfolio: Portfolio) -> Portfolio:
    """A private copy of the live book for the counterfactual to spend.

    :class:`~trading.types.Position` is frozen and ``apply_fill`` only rebinds dict
    entries, so copying the mapping is enough to make the two books independent.
    """
    return Portfolio(cash=portfolio.cash, positions=dict(portfolio.positions))


# --- reporting ----------------------------------------------------------------

CSV_COLUMNS = [
    "submitted_ts",
    "submitted_at",
    "symbol",
    "side",
    "order_qty",
    "reference_price",
    "live_outcome",
    "live_ts",
    "live_qty",
    "live_price",
    "live_commission",
    "live_reason",
    "model_outcome",
    "model_qty",
    "model_price",
    "model_commission",
    "model_reason",
    "realized_slippage_bps",
    "modelled_slippage_bps",
    "slippage_error_bps",
    "price_difference",
    "qty_divergence",
    "latency_seconds",
    "outcome_diverged",
]


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _num(value: float | None) -> str:
    return "" if value is None else repr(value)


def divergence_rows(records: Sequence[FillDivergence]) -> list[dict[str, str]]:
    """Render divergence records as flat, CSV-ready string rows."""
    rows: list[dict[str, str]] = []
    for record in records:
        latency = record.latency
        rows.append(
            {
                "submitted_ts": _iso(record.submitted_ts),
                "submitted_at": _iso(record.submitted_at),
                "symbol": record.symbol,
                "side": record.side.value,
                "order_qty": _num(record.order.qty),
                "reference_price": _num(record.reference_price),
                "live_outcome": record.live.outcome,
                "live_ts": _iso(record.live.ts),
                "live_qty": _num(record.live.qty),
                "live_price": _num(record.live.price),
                "live_commission": _num(record.live.commission),
                "live_reason": record.live.reason or "",
                "model_outcome": record.shadow.outcome,
                "model_qty": _num(record.shadow.qty),
                "model_price": _num(record.shadow.price),
                "model_commission": _num(record.shadow.commission),
                "model_reason": record.shadow.reason or "",
                "realized_slippage_bps": _num(record.realized_slippage_bps),
                "modelled_slippage_bps": _num(record.modelled_slippage_bps),
                "slippage_error_bps": _num(record.slippage_error_bps),
                "price_difference": _num(record.price_difference),
                "qty_divergence": _num(record.qty_divergence),
                "latency_seconds": _num(latency.total_seconds() if latency else None),
                "outcome_diverged": "true" if record.outcome_diverged else "false",
            }
        )
    return rows


class DivergenceJournal:
    """Append settled divergence rows to ``path`` as they close (ADR-0048).

    The file this writes is the same ``fill_divergence.csv`` the end of the run
    replaces: same header, same columns, same rendering, rows in the same
    settlement order :attr:`ShadowBroker.divergences` reports them in. So a crashed
    session's file is a **prefix** of the file it would have finished with, not a
    different artifact needing different tooling — and it is readable while the
    session is still running.

    Each :meth:`append` opens, writes, flushes, ``fsync``s and closes. No handle is
    held between bars, so there is nothing to leak on an exception path and nothing
    to reopen after a crash; and the ``fsync`` is what separates surviving a killed
    *process* (a flush would do) from surviving a killed *machine*. The cost is one
    open/fsync per bar that settled something — on the order of 90 for a session
    the Monday runbook describes.

    Not atomic per row, and it does not need to be: each row is written with a
    single small ``write`` to a regular file, which a dying process cannot tear in
    half, and the final :func:`write_divergence_csv` replaces the whole file anyway.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        # The header lands immediately, so a session that settles nothing still
        # leaves a well-formed (empty) CSV rather than a missing file.
        with path.open("w", newline="") as handle:
            csv.DictWriter(handle, fieldnames=CSV_COLUMNS).writeheader()
            _sync(handle)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def rows(self) -> int:
        """How many rows have been persisted so far."""
        return self._rows

    def append(self, records: Sequence[FillDivergence]) -> None:
        """Append ``records`` and make them durable before returning."""
        if not records:
            return
        with self._path.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=CSV_COLUMNS).writerows(divergence_rows(records))
            _sync(handle)
        self._rows += len(records)


def _sync(handle: IO[str]) -> None:
    """Flush Python's buffer *and* the OS's, so the bytes survive the machine.

    ``flush`` alone is enough to survive a killed process — the bytes are already
    the kernel's. ``fsync`` is what carries them past a lost machine, which is the
    case ADR-0048 exists for that no signal handler can cover.
    """
    handle.flush()
    os.fsync(handle.fileno())


def write_divergence_csv(records: Sequence[FillDivergence], path: Path) -> None:
    """Write one row per tracked order — including the ones that never filled.

    Written to a sibling temp file and moved into place with :func:`os.replace`,
    which is atomic within a filesystem on POSIX (ADR-0048). Two reasons, both
    about the incremental journal this may be replacing: a crash part-way through
    the final write must not truncate rows that were already safely on disk, and a
    reader watching the file while the session finishes must never see a partial
    one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(divergence_rows(records))
            _sync(handle)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _bps(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f} bps"


def _seconds(value: timedelta | None) -> str:
    return "n/a" if value is None else f"{value.total_seconds():.1f}s"


def _describe_side(settlement: Settlement) -> str:
    if settlement.price is not None and settlement.qty is not None:
        return f"{settlement.outcome} {settlement.qty:g} @ {settlement.price:.4f}"
    if settlement.reason:
        return f"{settlement.outcome} ({settlement.reason})"
    return settlement.outcome


def render_report(
    summary: DivergenceSummary,
    records: Sequence[FillDivergence],
    *,
    max_rows: int = 10,
) -> str:
    """A human-readable divergence block, honest about what it cannot conclude.

    Rendered here rather than in :mod:`trading.report` so the divergence feature
    owns its own presentation and the shared run summary stays untouched.
    """
    stdev = summary.stdev_realized_bps
    lines = [
        "Fill divergence — live paper vs simulated (ADR-0038)",
        f"  Price notion:      {summary.price_notion} (both sides; never mixed — ADR-0021)",
        f"  Cost model:        {summary.modelled_slippage_bps:.2f} bps slippage, "
        f"${summary.modelled_commission_per_share:.4f}/share commission",
        f"  Orders tracked:    {summary.orders} (accepted by the live broker)",
        *(
            [
                f"  Refused at submit: {summary.submit_refusals} — never reached the venue, "
                "so not tracked and not pending (ADR-0036/0041; see the run's rejections)"
            ]
            if summary.submit_refusals
            else []
        ),
        f"  Comparable fills:  {summary.comparable} (both sides filled, reference open known)",
        f"  Outcome mismatch:  {summary.outcome_divergences} "
        f"(live-only fills {summary.live_only_fills}, model-only {summary.model_only_fills})",
        "",
        "  Realized slippage vs the model's reference (next bar's open),",
        "  signed so positive is worse for us:",
        f"    mean   {_bps(summary.mean_realized_bps)}",
        f"    median {_bps(summary.median_realized_bps)}",
        f"    worst  {_bps(summary.max_realized_bps)}",
        f"    stdev  {'n/a' if stdev is None else f'{stdev:.2f} bps'}",
        f"    modelled                    {summary.modelled_slippage_bps:.2f} bps",
        f"    error (realized - modelled) {_bps(summary.mean_error_bps)} mean",
        "",
        "  Observation latency (submit → settlement seen, on the injected clock;",
        "  an upper bound — a polling broker only notices a fill when it polls):",
        f"    mean {_seconds(summary.mean_latency)}    max {_seconds(summary.max_latency)}",
        "    model: the next bar, by construction",
    ]

    diverged = [r for r in records if r.outcome_diverged]
    if diverged:
        lines.append("")
        lines.append("  Outcome divergences (a fill on one side, not the other):")
        lines.extend(
            f"    {record.symbol:<6} {record.side.value:<4} {record.order.qty:g} — "
            f"live {_describe_side(record.live)} | model {_describe_side(record.shadow)}"
            for record in diverged[:max_rows]
        )
        if len(diverged) > max_rows:
            lines.append(f"    ... and {len(diverged) - max_rows} more (see the CSV)")

    if summary.unmatched_live_fills or summary.unmatched_live_rejections:
        lines.append("")
        lines.append(
            f"  WARNING: {summary.unmatched_live_fills} venue fill(s) and "
            f"{summary.unmatched_live_rejections} rejection(s) could not be attributed "
            "to a tracked order; the comparison is incomplete."
        )
    if summary.errors:
        lines.append("")
        lines.append("  WARNING: the shadow was disabled mid-run and measured nothing after:")
        lines.extend(f"    {message}" for message in summary.errors)

    lines.append("")
    lines.append(_verdict(summary))
    return "\n".join(lines)


def _verdict(summary: DivergenceSummary) -> str:
    """State plainly whether the sample can support a claim about the cost model."""
    if summary.comparable == 0:
        return (
            "  VERDICT: no comparable fills — this run says nothing about the "
            f"{summary.modelled_slippage_bps:.2f} bps model. Run it live, with orders."
        )
    if not summary.conclusive:
        return (
            f"  VERDICT: {summary.comparable} paired fill(s) is below the "
            f"{summary.min_samples} this report needs before quoting an average as "
            f"evidence (ADR-0029's spirit). The {summary.modelled_slippage_bps:.2f} bps "
            "model is neither confirmed nor refuted; these rows are observations."
        )
    error = summary.mean_error_bps or 0.0
    implied = summary.implied_slippage_bps or 0.0
    direction = "optimistic" if error > 0 else "conservative"
    return (
        f"  VERDICT: over {summary.comparable} paired fills the venue charged "
        f"{implied:.2f} bps on average against the model's "
        f"{summary.modelled_slippage_bps:.2f} bps, so the backtest cost model is "
        f"{direction} by {abs(error):.2f} bps. This is one account, one venue, one "
        "strategy's order flow — not a market-wide constant."
    )
