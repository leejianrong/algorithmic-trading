# ADR-0014: Paper-mode loop — shared step, completed-bar gate, state persistence

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

ADR-0002 committed the project to *one execution path*: backtest and paper must
share the same engine, broker, sizing, and guardrails, differing only in the feed
and the clock. Slice V5 is where paper mode actually arrives, which forces a few
mechanism-level choices ADR-0002 left open:

1. How do backtest and paper share the per-bar logic *without* one re-implementing
   it (the exact drift ADR-0002 exists to prevent)?
2. How does the paper loop avoid acting on a still-forming daily bar?
3. A poll-based loop can see the same bar on consecutive polls — how is a bar
   processed exactly once?
4. What running state does a paper session persist, and in what shape?

## Decision

**Shared per-bar step.** The body of the old `Engine.run` loop is extracted
verbatim into a private `Engine._step(strategy, ts, bars, state)` that mutates a
`_RunState` accumulator and returns a per-bar `BarOutcome`. `Engine.run`
(backtest) builds the feed and calls `_step` for each timestamp; `PaperSession`
calls the *same* `_step` for each newly completed bar. There is one copy of the
execute → reveal → monitor → size → check → submit → mark sequence, so the
no-look-ahead invariant (ADR-0001) and the guardrail semantics (ADR-0013) hold
identically in both modes. `_finalize` assembles the `BacktestResult` for both.

**Completed-bar gate.** Paper mode reads bars through
`RecentWindowFeed`, which drops any bar the injectable completeness policy deems
still forming (default: a daily bar dated `D` is complete once the clock's UTC
date is strictly past `D`). The forming bar for the current session is therefore
never handed to `_step`. The gate lives in the feed, not the engine, so the engine
stays feed-agnostic.

**Idempotent reprocessing.** `PaperSession` keeps a `set` of processed
timestamps; each poll filters the feed to timestamps not yet seen and steps only
those. Re-polling a completed bar (which a recent-window feed does every poll)
never reprocesses it. The loop is bounded for tests and offline demos by
`max_new_bars` and a consecutive-empty-poll limit, with `max_polls` as a hard
safety.

**State persistence.** Per completed bar, the session appends a human-readable
line to a session-log file and overwrites a small JSON state file in the result
directory holding the latest `ts`, `equity`, `exposure`, `cash`, `halted`, and
per-symbol `{qty, avg_price}`. A JSON snapshot (overwrite, not append) makes the
current book trivially recoverable; the log preserves the per-bar narrative; the
equity curve is written as CSV at the end, exactly as backtest does.

## Alternatives considered

| Option | Why not |
|--------|---------|
| A separate paper loop that re-runs sizing/checks | Re-implements the order path — the drift ADR-0002 forbids. |
| Copy the loop body into `PaperSession` | Two copies diverge the first time one is edited. |
| Gate the forming bar inside the engine | Couples the engine to a wall-clock notion of "today"; the feed already owns completeness. |
| Track "last processed ts" only | A feed that reveals an out-of-order or backfilled bar would be missed; a `set` reprocesses nothing yet catches any newly completed timestamp. |
| Append full state each bar | Recovering "current" state means parsing the whole log; an overwritten JSON snapshot is O(1) to read. |

## Consequences

- Buys: parity is structural — the headline V5 test feeds identical bars to both
  modes and asserts identical curve, fills, positions, clamps, and halt.
- Costs: `_step`/`_finalize`/`_RunState` are now load-bearing internal seams; the
  `BarOutcome` record is a new (internal) contract the CLI reporter depends on.
- Forecloses nothing: a real broker (ADR-0004) or a live intraday feed slots in as
  a new `Broker`/feed behind the same step. Intraday completeness is a new policy
  passed to `RecentWindowFeed`, not an engine change.
- Now true: `WallClock` + `RecentWindowFeed` drive real paper trading; the same
  code under `FakeClock` + `FakeAdapter` runs offline and deterministically in the
  fast test layer with no waiting.
