# ADR-0042: A live paper session warms up on history instead of trading it

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

`RecentWindowFeed.poll` asks its adapter for `[datetime.min, now]` and keeps the
newest `lookback` completed bars per symbol (`DEFAULT_PAPER_LOOKBACK = 512`).
`PaperSession.run` then treated every timestamp it had not already seen as *fresh*:

```python
fresh = [(ts, bars) for ts, bars in feed if ts not in self._seen]
```

On the **first** poll of a session `self._seen` is empty, so all 512 bars were
fresh and every one went through `Engine._step` — which runs the strategy, sizes
its intents, checks the guardrails, and **submits live orders**. A `--live`
session's opening act was therefore to trade a week of history at today's venue.

Measured offline against the real live wiring (`RecentWindowFeed` +
`interval_is_complete` + `PaperSession` + a `FakeClock` whose `sleep_until`
advances exactly as a wall clock would), `--interval 5m`, session opening
2024-03-07 15:02 UTC, `sma_crossover`, 40 polls:

| | orders on bars that closed **before** the session opened | orders on genuinely live bars |
|---|---|---|
| before | **58** | 5 |
| after | **0** | 5 |

The first bar the old code stepped was stamped 2024-02-27 17:50 — eight days
before anyone pressed start.

This is not a cosmetic defect. Three things it breaks:

1. **The fill-divergence measurement (ADR-0038), which is the entire point of
   Monday's session.** A backfill order's reference price is a historical bar's
   open while the venue fills at today's price, so each one contributes a
   divergence in the ±1,100 bps range. At the observed ratio the sample is ~86%
   noise — and because the paired-fill count clears `MIN_PAIRED_FILLS = 30` on
   garbage, the report still prints a verdict instead of refusing to conclude.
2. **Real money-shaped risk.** These are real orders at a real venue, sized from
   stale prices, fired in a burst.
3. **The venue.** ~2,150 API calls in the opening seconds of a session.

### Two fixes that look right and are worse

**Skip the backfill.** History accumulates *only* inside `_step`
(`state.history[symbol].append(bar)`), and `_Context` is built from
`state.history`. Skip the backfill and the strategy has no history at all:
`sma_crossover` cannot compute a 20-bar average until 20 live bars have passed
(100 minutes at 5m), `momentum` needs 60 (5 hours), `cross_sectional` needs 121 —
longer than a session. The backfill is *how strategies warm up*; deleting it
trades one silent failure for another.

**Replay the backfill with order submission suppressed.** This is the trap.
Strategies here are stateful and **transition-driven**: `SmaCrossover` and
`Momentum` keep a per-symbol `_long: dict[str, bool]` and emit an intent only when
it flips. Run one over history while swallowing its orders and it finishes
believing it is long while the account is flat — so on the first live bar there is
no transition, nothing is emitted, and the session sits flat all day. Silently.
That is strictly worse than the bug being fixed, because the bug at least announces
itself.

## Decision

**Warmup bars are loaded as data. Nothing else touches them.**

`engine.prime_history(state, feed)` performs exactly the bookkeeping half of
`_step` — append each bar to `state.history`, mark `state.last_close` — and
nothing else:

- **no strategy call**, so the strategy's latch is pristine and its first
  invocation is on a genuinely live bar, where it transitions from flat exactly
  once (this is what makes the fix different from suppressing orders);
- **no sizer, no guardrails, no broker**, so no order exists to suppress in the
  first place and the guardrails' peak-equity state starts from the live book;
- **no `EquityPoint`**, because the account held nothing during the warmup and a
  fabricated curve would corrupt every metric computed from it — return, Sharpe,
  drawdown, exposure, turnover.

It is a module-level function rather than a method so the boundary is directly
testable and so nothing about it can be mistaken for a second execution path.

### The boundary: the first poll that reveals bars

`PaperSession` gains `warmup: bool = True`. While warmup is pending, the first
poll that returns **any** bars is absorbed as history and the boundary closes;
every poll after it is live. Concretely:

- It is **not** "the first poll". `RecentWindowFeed` swallows a per-symbol fetch
  failure and returns an empty cross-section rather than raising (ADR-0035), so a
  broken opening fetch would otherwise hand the whole backfill to the live path one
  poll later — the same bug with an extra step.
- It is **not** a wall-clock cutoff such as "bars stamped before `clock.now()` at
  startup". A bar mid-formation when the session opens has a timestamp *before*
  startup and completes *after* it; a timestamp cutoff would classify that
  genuinely live bar as warmup and silently skip it. Deferring to the feed's own
  completeness policy gets it right without duplicating that policy in a second
  place.
- A bar arriving mid-session is **never** warmup. The boundary closes once and
  stays closed for the life of the session.
- A poll that primed bars **resets** the empty-poll counter. It is the opposite of
  a quiet poll, and counting it as empty would leave a default session one dull
  poll away from stopping before it ever traded.

Residual, accepted: if every poll fails for a stretch while a new bar completes,
that bar is absorbed into the warmup instead of traded. That is one bar, it errs
toward *not* trading, and it only happens when the data source is already broken.

### `--once` opts out, explicitly

`trading paper --once` replays `[from, to]` and trades it — that *is* the mode, and
it is how every offline paper test and demo works. So the CLI passes
`warmup=live`: live sessions warm up, replays do not. The library default is the
safe one (`True`), so a `PaperSession` built without thinking about it cannot fire
a backfill at a venue; the replay path states its intent in one word at the one
place that means it.

Verified byte-identical, not argued: `paper --once` over a synthetic range produces
the same `equity_curve.csv`, `result.json`, `paper_session.log`, `paper_state.json`
and stdout before and after, and the equity CSV's SHA-256 is pinned as a golden in
`tests/unit/test_paper_warmup.py`. `Engine.run` is untouched, and a backtest's
artifacts diff clean against the same command run on `origin/main`.

### The warmup is reported, never silent

`PaperSession` exposes `warmup_bars`, `warmup_span` and `warmup_complete`, and the
CLI prints one line into both stdout and `paper_session.log` before the first live
bar:

```
Warmup: primed 512 completed bar(s) 2026-08-06 13:35..2026-08-08 19:55 as history;
no orders submitted for them (ADR-0042).
```

A session that warmed up on nothing says so instead, because a strategy starting
with empty history will sit flat for its whole lookback and the operator should
know that before the market does. A silent warmup is indistinguishable from a
session that quietly did nothing at all — the same reasoning as ADR-0032's absent
symbols.

### `--lookback` is now an operator lever

`trading paper --lookback N` sets how many recent completed bars each poll requests
and therefore how much history a live session warms up on (default 512). Under
`--once` it is a **floor**, never a truncation: the replay always covers the whole
requested range, so the flag cannot silently shorten a backtest-shaped run.

## Consequences

- **ADR-0002 holds.** `Engine._step` is unchanged and remains the only code that
  trades, in both modes. Warmup is feed-side interpretation of the opening window,
  not a second execution path: it calls neither the strategy, the sizer, the
  guardrails, nor the broker.
- **ADR-0001 holds.** Warmup bars are strictly older than every live bar, and the
  strategy still sees only past-and-present history.
- **A live session does not trade on its first interval.** It primes, sleeps to the
  next boundary, and trades the first bar to complete after it opened. That is the
  correct behaviour and it is now visible in the log rather than hidden behind a
  burst of backfill orders.
- **Metrics for a live session now cover the live session only.** Its equity curve
  starts at its first live bar, which is what an operator comparing paper against a
  backtest should be reading.
- **The existing paper tests now declare themselves.** Every `PaperSession` in
  `test_paper_session.py`, `test_recent_window.py` and
  `test_raw_adjusted_policy.py` passes `warmup=False`, because every one of them is
  a replay. That churn is the
  point: a replay can no longer be mistaken for a live session, or vice versa.
- **Still open.** The warmup is not surfaced in `result.json` or the dashboard —
  it reaches the operator through stdout and the session log only.
  `RESULT_SCHEMA_VERSION` stays **1**; adding a `warmup` block would be additive and
  is deliberately deferred rather than smuggled into this slice. Nothing yet checks
  that the primed history is actually *long enough* for the configured strategy's
  lookback: 512 bars covers every strategy in the registry today, but a strategy
  with a longer window would warm up short and the tool would not say so.
