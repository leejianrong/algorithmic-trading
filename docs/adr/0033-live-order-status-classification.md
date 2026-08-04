# ADR-0033: Classifying live order statuses, and finalizing an interrupted session

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

ADR-0020 designed the Alpaca paper broker as submit-then-poll: `on_bar` polls each
pending order "until it reports `filled` / `rejected` or a short, configurable poll
timeout elapses", and an order still working at timeout stays pending and is
retried on the next bar. That was written without the `alpaca-py` SDK installed
(ADR-0018), against an assumed order lifecycle.

Running it revealed the assumption was too narrow. Alpaca's `OrderStatus` has
**18** members, of which **five** are terminal — `filled`, `rejected`, `canceled`,
`expired`, `replaced` — and the poll recognised only the first two. The other
thirteen (`new`, `accepted`, `pending_new`, `partially_filled`, `done_for_day`,
`held`, `pending_cancel`, ...) are *working* states where waiting is correct.

That gap is not cosmetic. `canceled` / `expired` / `replaced` are states an order
reaches and never leaves, so under the original rule such an order was never
"settled": its id stayed in `_pending` and every subsequent `on_bar` re-polled it
for the **full poll timeout** (30s by default) before giving up again — for the
rest of the session. One canceled order therefore costs 30 seconds per bar
forever, and the order is never reported as having gone anywhere. An order expiring
at end of day is routine (a `TimeInForce.DAY` order that cannot fill), and a
cancellation from the Alpaca dashboard is a click away, so this is an ordinary path,
not an exotic one.

A second, unrelated gap surfaced in the same first live session. `--live` paper
mode "runs until interrupted" — Ctrl-C is not an error path, it is *the* exit. But
the interrupt propagated out of `session.run(...)` and past everything after it, so
the equity CSV, `result.json` (the dashboard's canonical artifact, ADR-0023), and
the printed summary were never written. Observed on a real session that processed
377 bars and left only `paper_session.log` and `paper_state.json` behind. The
`--once` replay mode terminates normally and writes all three, which is exactly why
nobody noticed: the mode that was tested is the mode that does not have the problem.

## Decision

**Classify the whole terminal set, not just the two happy-path statuses.**
`trading.data.alpaca_client` names all five terminal statuses and exposes two
frozensets:

- `TERMINAL_UNFILLED_STATUSES = {canceled, expired, replaced}` — the venue ended
  the order without completing it.
- `TERMINAL_STATUSES = {filled, rejected} | TERMINAL_UNFILLED_STATUSES` — what
  `_poll` stops on.

The literals are alpaca-py's `OrderStatus` **values**, and that correspondence is
asserted in the integration layer rather than trusted, so an SDK release that
renames one fails a test instead of silently reintroducing the leak.

**A terminal-unfilled order is dropped with a recorded reason, after emitting any
partial fill it did get.** Three sub-decisions:

- It is *reported*, not silently discarded: the reason lands on `broker.rejections`
  naming the status (`"order 17 ended canceled at the venue"`). Dropping an order
  the strategy asked for without saying so is the class of dishonesty this repo
  forbids.
- It is reported alongside outright `rejected` rather than in a separate bucket.
  Both mean "this order did not do what was asked", the recorded reason carries the
  distinction, and a third collection on the broker would need plumbing through
  `BacktestResult` for no decision anyone makes differently.
- A **partial** fill is still emitted. A partially-filled-then-canceled order moved
  real shares; the blotter must show them. (The portfolio is reconciled from the
  account regardless, so cash and positions were always right — it was the *fill
  record* that would have been missing.)

**Working statuses keep waiting, exactly as before.** `partially_filled` is
explicitly a working state: the rest may still fill, so it must not be treated as
terminal. The pending/timeout/retry behaviour ADR-0020 chose is unchanged.

**`AlpacaBroker.pending_order_ids` becomes public** (a read-only tuple), so tests
and an operator can see the retry set through a seam rather than a private reach.

**An interrupted live session finalizes.** `PaperSession.finalize()` is public and
builds a `BacktestResult` from the bars processed so far; `run()` returns it at
every exit, and the CLI catches `KeyboardInterrupt` around the loop, prints
`"Interrupted — finalizing with the bars processed so far."`, and then writes the
CSV, `result.json`, and summary on the normal path. Ctrl-C is a *stop*, not a
failure, so the exit code is 0 and nothing about the run is thrown away.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Leave the poll on `filled`/`rejected` and let a timeout clear the order | The timeout does not clear anything — it *keeps* the order pending by design (that is what makes deferred fills work). There is no timeout after which the leak resolves. |
| Add a max-retry count per order instead of classifying statuses | Treats the symptom. A canceled order is *knowably* dead on the first poll; retrying it N times is N times too many, and the count would also cut off legitimately slow fills. |
| Treat every non-`filled` terminal status as a rejection with no fill | Loses a real partial fill from the blotter, which is a silently wrong trade record. |
| Give terminal-unfilled orders their own collection on the broker | More surface, plumbing through `BacktestResult`, and no caller would branch on it; the recorded reason already carries the status. |
| Poll `done_for_day` as terminal too | It is not: a `done_for_day` order can still fill on a later session. It becomes `expired` when it truly ends, which *is* handled. |
| Let Ctrl-C stay fatal and tell operators to use `--once` for artifacts | `--once` is an offline replay; it cannot produce a *live* session's result. That is asking the operator to not use the feature. |
| Install a `SIGINT` handler that stops the loop gracefully | More machinery for the same outcome. `KeyboardInterrupt` already unwinds to exactly the right place; the loop needs no cooperation. |
| Write the artifacts incrementally every bar | `paper_state.json` already does that for live monitoring. Rewriting a whole equity CSV and `result.json` every bar is wasteful, and a partial `result.json` mid-write is worse than none. |

## Consequences

- A canceled, expired, or replaced order now settles on its first poll, is reported
  once, and never re-polled: no per-bar timeout tax, and the blotter keeps any
  partial fill. The fast layer covers all three statuses, the partial-fill case, and
  a working status that must still wait.
- `--live` sessions produce the same artifact set as `--once`, so the dashboard
  works on a live run. The offline regression test reproduces the interrupt with no
  network and no clock wait.
- Both fixes were found only by executing the code (ADR-0018's blind spot). The
  status classification is pinned against the real SDK in the integration layer;
  the interrupt fix is pinned in the fast layer, since nothing about it needs a
  broker.
- Still unverified live: the market-closed pending/timeout branch of the real
  broker. It is covered offline under `FakeClock`, and the live order test asserts
  it when the market happens to be closed, but the first live run happened during
  market hours and took the fill branch. An order that expires overnight has not
  been watched end to end against the venue.
