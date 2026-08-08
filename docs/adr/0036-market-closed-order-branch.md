# ADR-0036: The market-closed order branch, and what a rejection is made of

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

ADR-0033 ended with an honest gap: "Still unverified live: the market-closed
pending/timeout branch of the real broker. It is covered offline under
`FakeClock` ... but the first live run happened during market hours and took the
fill branch." That branch is the *normal* state of an overnight or weekend paper
session, so leaving it unexecuted left the same blind spot ADR-0018 created for
the whole Alpaca path.

Driving it needed the venue shut, which is a few hours a day and all weekend. Run
on Saturday 2026-08-08 (next open Mon 2026-08-10 09:30 ET) against the paper
account, a fractional `TimeInForce.DAY` market order behaved as ADR-0020 assumed:

```
submitted: AlpacaOrder(id='aac0be09-…', symbol='AAPL', qty=0.01, side=Side.BUY,
                       status='accepted', filled_qty=0.0, filled_avg_price=None)
```

`accepted` — not filled, not rejected. It stayed `accepted` across five polls over
ten seconds; Alpaca parks the order for the next open. `accepted` is not in
`TERMINAL_STATUSES`, so `_poll` waited out its timeout, returned `None`, and the id
stayed in `pending_order_ids` — exactly the designed behaviour, now witnessed.
Cancelling moved it to `canceled` in under a second, and the next `on_bar` settled
it on the first poll and evicted the id, which is ADR-0033's fix working for real.

Two things did *not* survive contact:

**1. A rejection carried the order id where everything downstream expects the
`Order`.** `AlpacaBroker.rejections` was typed `list[tuple[str, str]]` and appended
`(order_id, reason)`. `SimulatedBroker.rejections`, `BacktestResult.rejections`, and
`report.result_to_dict` all use `list[tuple[Order, str]]` and read `order.symbol` /
`.qty` / `.side`. `Engine._finalize` merges the broker's list into the result's
through `getattr(self._broker, "rejections", [])`, which types as `Any`, so
`mypy --strict` saw nothing; the fast tests only ever read `rejections[0][1]`, the
reason string, so they saw nothing either. The result:

```
AttributeError: 'str' object has no attribute 'symbol'
```

the first time any order ended `canceled` / `expired` / `replaced` in a live
session — thrown while writing `result.json`, the canonical artifact ADR-0023
defines and ADR-0033 had just fixed the Ctrl-C path to preserve. And this is not an
exotic path: an unfilled `DAY` order **expires** at the close, so it is the routine
end of every order the market-closed branch parks. ADR-0033 built the terminal
classification and shipped a crash on the wire it hung from it.

**2. There was no way to take a parked order back.** The `AlpacaClient` seam had
five calls and no cancel. A queued order that outlives a run fills at the next open
— hours later, unattended — so the existing live test's cleanup, which sold back
only what it could *see* in `list_positions`, left a buy queued whenever it ran with
the market closed. It cleaned up the branch it had been tested on.

## Decision

**A broker rejection is `(Order, reason)`.** `AlpacaBroker` keeps a
`dict[str, Order]` of what each pending id was asked to do, and records the
submitted `Order` alongside the reason, matching `SimulatedBroker` exactly. The
reason string is unchanged and still names the venue's id and the status
(`"order aac0be09-… ended canceled at the venue"`), so ADR-0033's honesty
requirement is intact and nothing is lost by dropping the id from the tuple slot.
One execution path (ADR-0002) means one shape for the field both brokers feed.

The regression is pinned three ways in the fast layer: the tuple carries an
`Order`, that `Order` survives `report.result_to_dict` into a real `rejections`
entry, and its type matches what `SimulatedBroker` appends. The last one is the
test that would have caught this before it shipped.

**`cancel_order(order_id) -> None` joins the seam** — the sixth call, and the
widening ADR-0017 explicitly anticipated ("adding a needed call (e.g. cancel-order)
is a deliberate widening of the seam plus a fake update"). Its semantics are what
the venue was observed to do, not what seemed reasonable:

- It is a *request*, not a result. It returns nothing; the order reaches `canceled`
  asynchronously and callers re-read it with `get_order` — which is what the
  broker's existing poll already does, so no new machinery is needed to consume it.
- Cancelling an already-terminal order **succeeds silently**. Alpaca answered a
  repeat cancel of a canceled order with 200 and no error, so cleanup can call it
  unconditionally. `FakeAlpacaClient` mirrors that rather than inventing an error.
- An unknown id raises `LookupError`, mapped from the venue's 404
  (`{"code":40410000,"message":"order not found for …"}`), keeping "we never heard
  of it" apart from "we could not ask" the same way `get_asset` does (ADR-0028).
- A partial fill on a cancelled order stays on the record, so ADR-0033 can still
  emit it as a `Fill`.

**Live tests skip on market state rather than asserting the wrong branch.** The new
`TestMarketClosedOrder` class calls `pytest.skip` when the venue is open, the same
way `TestOrderLifecycle` branches on it. A test that silently passes because the
branch it names could not run is worse than one that says it did not run.

**Cleanup cancels before it sells.** `_flatten` now cancels every working order in
the symbol first, then sells any excess, and a standalone test asserts no working
order is left behind.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Keep `(order_id, reason)` and teach `report.result_to_dict` to accept both shapes | Pushes a broker's private bookkeeping into the report and makes the union permanent. The field has one declared type; the broker that disagreed was the bug. |
| Give `AlpacaBroker` its own rejections collection, not merged into the result | ADR-0033 already rejected a third collection, and this would *hide* venue rejections from `result.json` — the opposite of the honesty the repo trades on. |
| Add `rejections` to the `Broker` protocol so mypy catches the mismatch | Right instinct, wrong slice: `Engine` reads it through `getattr` with a default precisely because it is optional surface, and widening the engine-facing protocol touches files this change does not own. Recorded as a follow-up. |
| Cancel by reaching into `client._trading.cancel_order_by_id` from the test | A private reach across the seam the repo exists to keep narrow, and it would leave production code unable to cancel at all. |
| Have `cancel_order` block until the order reports `canceled` | Hides a wall-clock wait inside a client call that has no `Clock` injected, and duplicates the poll the broker already owns. |
| Make `cancel_order` raise on an already-terminal order | Contradicts the venue, which returns 200. Cleanup would then need to race the status to decide whether it may call. |
| Wait for a real overnight `expired` order instead of cancelling | The same terminal-unfilled code path, but it needs a session held open across a close. Cancel reaches it in one second; `expired` is worth watching once, and is recorded below as still-unseen. |

## Consequences

- The market-closed branch is executed, not inferred: `accepted` is real, it is
  non-terminal, the poll timeout returns cleanly with the id still pending, and a
  cancel settles on the next poll without burning the timeout. Five new live tests
  cover it, double-gated on credentials **and** the SDK, and they skip when the
  market is open.
- A live session that has any order end unfilled now writes `result.json` instead
  of dying in it. Every strategy that ever runs overnight hits this.
- The seam is six calls wide. `FakeAlpacaClient` implements `cancel_order` too, so
  the fast layer keeps full coverage of it with no network.
- **Still unverified, and worth naming:** an order that *expires* overnight at the
  venue has not been watched end to end (only `canceled`, which shares the code
  path). The paper account was left flat — no positions, no working orders,
  $100,000.06 cash — and that was checked after the run, not assumed.
- ~~**Known and unfixed:**~~ **now fixed — see the amendment below.** While orders
  sit parked, the portfolio reconciles from the account and therefore stays flat, so
  a target-weight strategy re-emits the same order every bar and `AlpacaBroker`
  submitted a *duplicate* each time. Overnight at an intraday interval that stacks
  orders that all fill at the open, and the poll cost grows with the pending set.
  Suppressing a duplicate needed a policy decision about what a strategy means when
  it re-targets a weight it already asked for, so it got its own slice rather than
  riding along with a verification ticket.

## Amendment (2026-08-08, KAN-669): a duplicate order is refused, not sent

The open question above is now decided. **If `AlpacaBroker` is already working an
order in the same symbol and the same direction, `submit` refuses the new one** —
it records `(order, reason)` on `rejections`, naming the working order's venue id,
and never reaches the client.

Confirmed live on 2026-08-08 before the fix, and worse than the paragraph above
said: **the guardrails give no cross-bar protection at all.** `Guardrails` nets
same-bar committed exposure through `_committed_gross`, but that tally is reset at
the top of every bar, and `current_gross` is read off a portfolio that a parked
order leaves flat. So each bar re-authorises a fresh *full* gross allowance against
a book it believes is empty; `max_position_pct = 0.25` caps one order, not the
running total. Measured in the fast layer: five bars of an unmet 20% target queued
**100% of equity** at the venue, all of it to fill at the next open. The only
backstop was Alpaca's own buying-power check — a third party's risk limit standing
in for ours.

**The refusal is keyed on symbol *and* side.** Two consequences, both intended:

- **A working BUY can never block a SELL.** This bench is long-or-flat (ADR-0011),
  so a SELL is the only exit there is, and an unsellable position is far worse than
  a duplicate buy. It is the same asymmetry the kill switch already encodes — exits
  are allowed while halted, always (ADR-0013/0031). Because the side is part of the
  key, an exit is never even *compared* against a working entry: it is structural,
  not a special case that a later refactor could drop. Tested in both directions.
  **Read this as a statement about the guard, not about the outcome** — the live
  run found that Alpaca refuses an opposite-side market order while one is working
  ("potential wash trade detected"), so a parked entry does block the exit at the
  *venue*. See the second amendment below.
- **A duplicate SELL is suppressed too, and that is not blocking an exit.** The
  first SELL is already at the venue and will still fill; a second would try to sell
  the same position twice, which this bench forbids outright.

**Partial fills stay legitimate (ADR-0033).** The key is *working*, not *submitted*.
An order that partly filled and then **ended** — `canceled` / `expired`, the routine
end of a parked DAY order — is out of the pending set by the next bar, so a
follow-up order for the unfilled remainder is a fresh intent and goes through (its
partial fill still flows, unchanged). An order still reporting `partially_filled` is
in a *working* state: the rest of that same order is live at the venue, so topping up
the remainder would double it, and the guard suppresses it until the venue settles.
Both cases have their own test.

**This is the symptom-level guard, and deliberately only that.** The intent-level
fix — teaching the sizer to net in-flight quantity, so a target is computed against
"held plus working" rather than against a book that reads flat — is **deferred as
KAN-678**. That is the better answer to "what does a strategy mean when it re-targets
a weight it already asked for": it stops the duplicate being *generated* instead of
refusing it at the wire, and it repairs the guardrails' blind spot rather than
routing around it. But it changes the sizing layer, which the backtest shares, so it
needs its own slice and its own byte-identity proof. The two are **defence in depth,
not alternatives**: even with a netting sizer, a broker that will submit a duplicate
on request is a broker that will eventually be asked to.

Why the broker and not somewhere else:

| Option | Why not |
|--------|---------|
| Net in-flight quantity in `sizing.py` (KAN-678) | The right fix, and the reason this one is scoped as a *guard* rather than a solution. It touches the shared sizing path, so it cannot ride along with a broker-local change that must not alter backtest output. Deferred, not rejected. |
| Track cross-bar committed exposure in `Guardrails` | Would fix the exposure hole generally, but the guardrails are shared by both modes and have no notion of an order in flight — only `AlpacaBroker` knows what the venue is still working. Making risk state depend on a broker's private pending set inverts the dependency. |
| Cancel the parked order and resubmit the new one | Trades a duplicate for a race: cancellation is asynchronous (established above), so between the request and `canceled` the venue may fill the original and the "replacement" doubles the position anyway. It also churns the venue every bar. |
| Refuse on symbol alone, ignoring side | Would block an exit while a buy is parked. Non-negotiable: long-or-flat means a stuck position has no other way out. |
| Suppress silently, with no `rejections` entry | The bench trades on honest numbers. A refused order is a decision the run made; it belongs in `result.json` and the summary beside a venue rejection, not in a log line. |
| Dedupe on the whole `Order`, quantity included | A re-emitted target rarely repeats to the share, so nearly every duplicate would slip through — the bug would look fixed and not be. |

Consequences:

- Five bars of an unmet target queue one order, not five. Ten new fast tests pin it:
  the submission count reaching a never-filling venue, the refusal's shape and its
  survival through `result_to_dict`, both exit directions, both partial-fill cases,
  the release once an order settles, and symbol independence. The exposure statement
  is asserted end to end through the real `Engine` with default guardrails, so the
  cross-bar hole cannot reopen quietly.
- **Only `AlpacaBroker` changed.** `SimulatedBroker` fills within the bar, so it
  never has a working order to duplicate; the backtest path is untouched and its
  output byte-identical.
- **One refusal per refused order**, so a weekend session that re-asks on every
  intraday bar records one row per bar for as long as the order is parked. That is
  deliberately not deduplicated: each row is a real decision the run made, and a
  hundred of them is the report telling you the venue has been holding your order
  for a hundred bars. `SimulatedBroker` records a rejected order the same way.
- **Two cosmetic gaps in the *engine's* per-bar bookkeeping, named rather than
  fixed.** A refusal happens at submit time, in `_step` step 4, but the engine
  snapshots `broker.rejections` around `on_bar` (step 1) to attribute
  `BarOutcome.broker_rejections`, and it appends to `BarOutcome.submitted` on the
  assumption that `submit` accepted. So a refusal reaches
  `BacktestResult.rejections` — hence `result.json` and the summary, which is the
  visibility that matters — while that bar's `BarOutcome` neither lists it nor
  omits the order from `submitted`. Both live in the shared engine, which this
  broker-local slice does not own.
- A live test drives the same three-bars-one-order sequence against the paper
  account with the venue shut and asserts the exit is still accepted. It is
  double-gated on credentials **and** the SDK and skips when the market is open.
  ~~**Not yet executed:**~~ **now executed — see the second amendment below.** The
  worktree this landed from had no credentials, so unlike the rest of ADR-0036 the
  live half of this amendment shipped written but unwitnessed.

## Second amendment (2026-08-08): the live half, executed

Run against the paper account with the venue shut (Saturday, next open Mon 09:30
ET). **The guard itself holds exactly as designed.** Three bars of the same unmet
`BUY 0.01 AAPL` intent produced one order at the venue, parked at `accepted`, and
two refusals both naming its id; the venue's own order list confirms exactly one
new working order in the symbol, not three. The whole `TestMarketClosedOrder`
class from this ADR's first half still passes unchanged, so the guard did not
disturb the parked-order path it sits next to.

Two things the offline fake had wrong, both now pinned:

- **The venue accepts a duplicate.** Two identical BUYs submitted straight at the
  client both came back `accepted` with distinct ids, both working. This ADR
  *reasoned* that nothing else would stop the stack; it is now checked, and it has
  its own live test so the day Alpaca starts deduplicating we hear about it.
- **The venue refuses the opposite side, and the refusal used to kill the run.**
  `403 {"code":40310000,"message":"potential wash trade detected. use complex
  orders","reject_reason":"opposite side market/stop order exists"}`, raised as a
  raw SDK `APIError` that nothing caught. It is now classified and recorded rather
  than propagated, which is **ADR-0041** — including why the exit promise above is
  a statement about this bench's guard and not about the system.

The account was left flat and checked: no positions, no working orders,
$100,000.06 cash.
