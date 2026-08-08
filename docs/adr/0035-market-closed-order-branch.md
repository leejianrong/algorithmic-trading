# ADR-0035: The market-closed order branch, and what a rejection is made of

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
- **Known and unfixed:** while orders sit parked, the portfolio reconciles from the
  account and therefore stays flat, so a target-weight strategy re-emits the same
  order every bar and `AlpacaBroker` submits a *duplicate* each time. Overnight at
  an intraday interval that stacks orders that all fill at the open, and the poll
  cost grows with the pending set. Suppressing a duplicate needs a policy decision
  about what a strategy means when it re-targets a weight it already asked for, so
  it gets its own slice rather than riding along with a verification ticket.
