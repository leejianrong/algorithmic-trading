# ADR-0038: Paper-vs-simulated fill divergence — measuring the cost model

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

Every number this bench reports rests on `CostConfig(slippage_bps=5.0)` and
`SimulatedBroker`'s fill rule: an order queued on bar *t* fills at bar *t+1*'s
**open**, moved adversely by 5 bps, plus commission (ADR-0004, Q14). That rule was
chosen to be "deliberately pessimistic" and has never been checked against a venue.
It cannot be: a backtest measured against its own cost model is a tautology.

Paper trading can check it, and that is the reason paper trading exists here. The
same `Order` goes to a real venue and comes back with a real price on a real
timeline, and the comparison is **survivorship-free by construction** — it is
measured on orders this bench actually sent, not on a curated basket of today's
winners (ADR-0027).

Four things had to be decided before writing any of it, because each has a wrong
answer that produces a confident, wrong number:

1. What the fair counterfactual *is*. A live fill compared against the wrong
   reference price yields a precise measurement of nothing.
2. Which price notion the comparison runs in. ADR-0021 splits the bench in half:
   backtests use adjusted prices, paper/live uses RAW quotes. A raw fill measured
   against an adjusted open is meaningless arithmetic that still prints a number.
3. How the shadow is prevented from touching the live path. A bug in a reporting
   feature must never cost a real (paper) order.
4. Whether adding this forks the execution path ADR-0002 forbids.

## Decision

### The counterfactual: the same order, the same bars, the *next* open

`divergence.ShadowBroker` is a `Broker` **decorator**. It holds the live broker,
forwards `portfolio` / `submit` / `on_bar` / `rejections` to it verbatim, and on the
side replays every order through a throwaway `SimulatedBroker`.

For an order submitted while processing bar *t*, the **reference price** is the open
of the first bar the feed subsequently serves for that symbol. That is exactly the
price `SimulatedBroker` fills at (ADR-0001's no-look-ahead rule made concrete), so
the modelled fill is `reference × (1 ± slippage_bps)` and the realized fill is
whatever the venue reported. **Both sides are divided by the same reference open.**
That is what makes `realized − modelled` a statement about the cost model rather
than about which bar was picked, and it is why the reference is captured once, on
the first bar the symbol appears, and never re-anchored if the venue settles three
bars later.

Two more properties fall out of using a real `SimulatedBroker` rather than
re-deriving the fill formula:

- **Rejections are modelled, not guessed.** The counterfactual answers "would the
  simulator have funded this?" with the simulator's own funding and oversell checks.
- **It is rebuilt every bar from a copy of the *pre-bar* live book.** Each order is
  judged against the book that really existed when it would have executed, so one
  early divergence cannot poison every later comparison — and the shadow's fills
  cannot double-spend cash the live fills already spent, which would manufacture
  fake "insufficient cash" divergences.

Slippage is signed so **positive is worse for us** on both sides: a buy above the
reference and a sell below it both cost money.

### The price notion: whatever the feed is serving, stated on the report

The comparison never converts between notions and never mixes them. Under
`--live`, `RecentWindowFeed` serves RAW quotes (ADR-0021) and the venue fills in raw
dollars, so both sides are raw. Under the offline `--once` replay the CLI
materializes the range through the adapter's default **adjusted** fetch, so that run
is labelled `adjusted` — both sides adjusted, still internally consistent, and
labelled honestly rather than described by the mode's name. The label is printed at
the top of every report.

### Latency: submit → observed settlement, on the injected `Clock`

`ShadowBroker` reads `clock.now()` at `submit` and again inside `on_bar`, never
`time.time()`, so `FakeClock` makes it deterministic. The number is **observation**
latency and the report says so: a submit-then-poll broker (ADR-0020) can only
*notice* a fill while polling, so this is an upper bound on the venue's own latency
and it is only meaningful under a wall clock. An offline replay drains every bar
inside one poll, so its latencies are near zero by construction.

### The shadow cannot perturb the live path — structurally, not by promise

Three properties, each with a test:

1. **The live call runs first and unguarded** in both `submit` and `on_bar`. Shadow
   code cannot execute before the real order is placed, so it cannot prevent it.
2. **All shadow work is inside `try/except Exception`.** A failure appends to
   `errors`, switches the shadow off for the rest of the run, and returns the live
   result unchanged. Off rather than retried: a shadow that raised once will raise
   every bar, and a live session must not pay that cost repeatedly. The failure is
   printed in the report, so a silently half-measured run is impossible.
3. **The counterfactual holds a copy**, has no client, and is discarded each bar. It
   has nothing to submit an order with and no live book to mutate.

The proof is a test that runs the same strategy twice — once through a plain
`SimulatedBroker`, once through a `ShadowBroker` whose injected shadow raises on
every call — and asserts the two `BacktestResult`s are **equal**. `BacktestResult`
is a dataclass, so that compares the whole run: curve, blotter, rejections, clamps,
halt state. The same test exists for a clock that raises.

### ADR-0002 is intact

There is no mode check anywhere. `Engine._step` is untouched, because a decorator
that satisfies the `Broker` protocol needs no engine support. Wrapping a
`SimulatedBroker` in a backtest is legal and is the mechanism's **null test**: the
same broker compared against its own model must diverge by exactly zero, and a test
asserts that. If the reference price were wrong, the null test would show the error.

### Honesty about sample size

`MIN_PAIRED_FILLS = 30`. Below it the verdict line says the model is "neither
confirmed nor refuted" and calls the rows observations, in the same spirit as
ADR-0029's trades-per-parameter warning. Above it the verdict quotes the implied
bps and names the model optimistic or conservative — and still says it is one
account, one venue, one strategy's order flow, not a market-wide constant.

Nothing is dropped. Every tracked order becomes a row, including a venue rejection
against a modelled fill, a modelled rejection against a venue fill, a partial fill
(ADR-0033) as one `partial` row rather than two half-rows, and an order still parked
at the venue when the session ended (ADR-0036) as `pending`. Fills the venue
reported that no submission could be attributed to are surfaced as a warning rather
than silently discarded.

### Attribution

A `Fill` carries no order id, so fills are attributed **FIFO within (symbol, side)**.
Rejections carry the `Order` itself (ADR-0036), so they are attributed by **identity**
— exactly. The same identity guarantee is what lets the shadow's outcomes be
attributed exactly: `SimulatedBroker` walks its queue in order and records the very
`Order` object it was handed, which a test pins, because if it ever copied the order
every modelled rejection would be mis-read as a fill.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Compare the live fill against the bar **close**, or the bar the order was decided on | Neither is what the model does. A close-based reference would fold a full session's drift into "slippage" and could easily invert the sign of the answer. |
| Re-derive the fill formula inside `divergence.py` instead of running a real `SimulatedBroker` | Then the report measures a *copy* of the cost model. The copy drifts the first time the real one changes, and the funding/oversell rejection semantics would have to be duplicated too. |
| Let the shadow's portfolio accumulate across bars instead of re-seeding from the live book | One early divergence (a modelled rejection where the venue filled) permanently forks the books, and every later row measures that fork rather than the fill model. |
| Compare a live paper run against a separately-run backtest of the same window | Different feeds, different price notions (raw vs adjusted, ADR-0021), different order sequences once the books diverge. It answers "did two runs differ", not "is 5 bps right". |
| A `Broker` protocol method or an `if paper:` branch in `Engine._step` | A mode check inside the shared step is exactly the fork ADR-0002 forbids, and it would make every broker implement reporting surface it does not need. |
| Let shadow exceptions propagate ("fail loudly") | Loud is right for the accounting path and wrong here: the live order is already placed, and killing the session to protect a report loses the survivorship-free evidence the session exists to gather (ADR-0035 made the same call for the paper feed). The failure is recorded and printed instead. |
| Fold the divergence block into `report.summarize` / `result.json` | The run summary describes the run; this describes the *model*. Keeping it in `divergence.py` also kept this slice off `report.py`, which a concurrent lane owned. Recorded below as a follow-up. |
| Attribute live fills by re-reading Alpaca order ids | Would push the venue's private bookkeeping through the `Broker` seam for a reporting feature. FIFO within (symbol, side) mis-orders only same-symbol, same-side duplicates, where both rows carry the same reference price anyway — so only latency can be swapped, never a price. |

## Consequences

- `trading paper --divergence` writes `fill_divergence.csv` (one row per tracked
  order) and prints the report block. **Off by default**, and a CLI test asserts the
  `equity_curve.csv` and `result.json` bytes are identical with and without the flag.
- The bench can finally answer, with evidence, whether 5 bps is right — but only
  after ~30 real paired fills. Until then it says so out loud.
- The mechanism is fully covered offline (`FakeAdapter` / `FakeAlpacaClient` /
  `FakeClock`); the live layer is double-gated on credentials **and** the SDK and
  skips cleanly in CI, so live runs are evidence, not the test suite.
- **Known limitation — cadence, not microstructure.** Under a daily paper session
  the venue fills at the next session open, which is the same event the model prices,
  so the comparison is tight. Run the session at a coarse interval against a venue
  that fills promptly and part of the measured "slippage" is the market moving
  between the bar open and the fill, not execution cost. The report cannot separate
  the two; it reports total realized cost against the model's reference, which is the
  number a backtest's P&L actually depends on.
- **Known limitation — one venue.** Alpaca paper fills are simulated by Alpaca
  against real quotes. They are far better evidence than our own model and are still
  not a live-capital execution record.
- Wanted, not built here (owned by another lane this cycle): a `divergence` block in
  `result.json` and a dashboard panel. Both are additive to
  `RESULT_SCHEMA_VERSION` 1, and `divergence.divergence_rows` already emits the flat
  shape they would serialize.
- Forecloses nothing: the wrapper takes any `Broker` as the live side and any
  `shadow_factory` as the counterfactual, so a different cost model (or a limit-order
  model) can be compared by swapping the factory.
