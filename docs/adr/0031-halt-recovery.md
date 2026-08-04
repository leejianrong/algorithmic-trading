# ADR-0031: Halt recovery — the kill switch may re-arm, opt-in

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

ADR-0013 §3 decided that the drawdown kill switch **"latches for the session"**:
once drawdown-from-peak (or a single-bar loss) reaches its threshold, `Guardrails`
sets `_halted = True` and never clears it, and one `Guardrails` instance belongs to
one run. For a live or paper session a human will look at, that is the right call —
you stop, you investigate, you restart deliberately.

For a long backtest it is quietly fatal, and the damage was measured on real
yfinance data over 2000-2020:

- The halt fired for **all six** strategies — a 20% drawdown is inevitable over
  twenty years — typically in **2001**, and then blocked every new entry for the
  remaining **19 years**.
- `cross_sectional` on a 20-name universe returned **-3.91%** with the latch versus
  **+1727%** with it neutralized, and logged **1853 rejected orders** against 142.
- Every strategy's numbers were dominated by this, so the whole comparison measured
  the kill switch rather than the strategies.

The only workaround available was `--max-drawdown 0.99`, which *disables* the
safety feature instead of making it usable — exactly the trade this bench exists to
avoid. It matters beyond backtests too: a live strategy should not be permanently
dead after one bad month.

## Decision

The kill switch gains **opt-in recovery**. Two independent mechanisms on
`RiskConfig`, both `None` by default, so every existing run, result, and test is
byte-identical and the ADR-0013 permanent latch remains what you get unless you ask
for something else:

| Field | CLI flag | Meaning |
|-------|----------|---------|
| `halt_recovery_drawdown_pct` | `--halt-recovery-drawdown` | Re-arm once drawdown from the peak is back to at most this fraction. |
| `halt_cooldown_bars` | `--halt-cooldown-bars` | Re-arm once the halt has been in force this many bars (counting the bar it fired on). |

Both flags are wired through `cli._build_risk` onto `backtest`, `paper`, and
`sweep`, following the `--target-vol` / `--max-sector-exposure` precedent.

**1. Recovery is measured against the live running peak.** The peak is monotone, so
while halted it can only move by equity *exceeding* it — which is a full recovery
(drawdown 0) that re-arms under any threshold. "The peak at halt time" and "the live
peak" therefore differ in no case that changes an outcome, and the live peak needs no
second stored reference.

**2. The combination rule is OR — whichever triggers first — not AND.** This ADR
originally specified AND as the conservative reading. Running it end to end on
sixteen years of synthetic data killed that: while halted, a long-or-flat strategy is
allowed to *exit* but not to enter, so it drains to cash and its equity — and with it
its drawdown — **freezes**. A drawdown condition that was not already satisfied when
the book went flat can then never be satisfied, and AND silently reinstated the
permanent latch this ADR exists to remove. In the measured AND build, halt episode #2
opened on 2009-02-25 and stayed in force for the final eleven years of the run with
exposure pinned at 0.0. Under OR the cooldown is the **liveness guarantee** and the
drawdown threshold is an **early re-arm** for a book that heals on its own. The
deadlock is now a named regression test.

**3. Anti-flapping is enforced, not hoped for.** Three guarantees:

1. **A non-empty hysteresis band.** `RiskConfig.__post_init__` rejects
   `halt_recovery_drawdown_pct >= max_drawdown_pct`. A config whose re-arm level
   coincides with its trip level cannot be constructed, so the "halt, re-arm, halt on
   the next bar" oscillation is refused at the boundary rather than discovered in a
   run.
2. **Re-arming resets the drawdown peak to the current equity.** Every halt after the
   first must be earned by a *fresh, full* `max_drawdown_pct` decline from the level
   trading resumed at — not one more sliver of the original crash. The number of
   episodes in a run is therefore bounded by the number of distinct
   `max_drawdown_pct` declines in the curve; a sawtooth whose legs stay inside the
   threshold produces exactly **one** episode. This peak is a *control* reference, not
   a reporting one: `metrics.compute` still derives the honest high-water mark and
   peak-to-trough drawdown from the equity curve and is untouched by the reset.
3. **A re-arm bar never re-halts.** The switch grants at least one bar of live trading
   before it can trip again, which also makes the halt/resume sequence a strict
   alternation — the property that lets the engine record clean episodes. With a
   cooldown of N and no recovery threshold, guarantees 2 and 3 tighten into a hard
   bars-based bound: halts cannot recur more often than every **N + 1 bars**, however
   hostile the equity path. A test drives a 41-bar curve engineered to flap every bar
   and asserts that bound.

**4. Exits stay allowed while halted, and entries stay blocked.** ADR-0013 §2 is
untouched and still tested; recovery only decides *when the block lifts*.

**5. Halt episodes are recorded, so a run shows how often it halted.** A single final
boolean cannot describe a run that halted six times. `BacktestResult` keeps
`halted` / `halt_ts` / `halt_reason` with their exact existing meanings ("a halt
occurred", and the **first** one's timestamp and reason) and gains
`halt_episodes: list[HaltEpisode]` — `(halt_ts, reason, resume_ts)`, `resume_ts` being
`None` for a halt still in force at the end — plus a `halt_episode_count` property.
The guardrails own the state machine and expose `halt_count` / `resume_count` /
`bars_halted`; the *engine* owns the timestamps, deriving episodes from the latch
transitions, so the `RiskGuardrails` seam signature does not change. `BarOutcome`
gains `resumed_now` and the paper log prints a `RESUME:` line to match `HALT:`.

`report.summarize` keeps its `Halt:` line byte-identical and adds a `Halt episodes:`
count plus one line per stretch **only when recovery actually did something** (a
re-arm happened, or there was more than one halt) — under the default latch the count
is always 1 and would add nothing.

**6. `RESULT_SCHEMA_VERSION` stays at 1.** `result.json`'s `halt` object gains
`episode_count` and `episodes`; every pre-existing key keeps its exact meaning and
value, so a v1 reader (including the current dashboard, which reads `halt` through
`.get`) is unaffected. The version signals *incompatible* shape changes, and the
dashboard validates it by **exact equality** (`dashboard.payload._check_schema`), so a
gratuitous bump would reject every `result.json` already on disk while gaining nothing.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Keep the permanent latch (ADR-0013 as written) | Measured: it dominated every long backtest, turned +1727% into -3.91%, and made the strategy comparison meaningless. It also means one bad month permanently kills a live strategy. |
| Recovery on by default | Would silently change every prior result and every existing test's numbers. The bench's discipline rests on old results not shifting under it; a safety default is also the wrong thing to relax without being asked. |
| `--max-drawdown 0.99` (the status-quo workaround) | Disables the guardrail instead of making it usable — no protection at all, and it hides the fact that the run ever drew down 20%. |
| Require **both** conditions (AND) | Measured deadlock: a halted long-or-flat book drains to cash, equity and drawdown freeze, the drawdown condition becomes unreachable, and the permanent latch returns. Eleven years of one 2005-2020 run were spent halted at zero exposure. |
| Re-arm at the same drawdown that trips the switch | Flaps: halt, re-arm, halt again on adjacent bars. Now impossible — the config refuses it. |
| Don't reset the peak on re-arm | Every later dip re-trips off the *old* peak, so one crash produces an episode per oscillation. The reset makes each new halt earn a fresh, full decline. |
| Auto-flatten on halt, restart the run after | Still forecloses the rest of the backtest and (per ADR-0009) auto-flatten was deliberately not the default. |
| Bake the timestamps into `Guardrails` (pass `ts` to `halted()`) | Widens the `RiskGuardrails` seam and every implementation/stub for information the engine already holds. The engine watches the latch transitions instead. |
| Bump `RESULT_SCHEMA_VERSION` to 2 | The change is purely additive; and because the dashboard compares versions for exact equality, a bump would reject every existing `result.json` for no benefit. |

## Consequences

- Buys: a long backtest can be *protected* and still finish honestly — on a 2005-2020
  synthetic `cross_sectional` run with `--max-drawdown 0.05`, the latch gave +16.97%
  and 1327 rejections while `--halt-recovery-drawdown 0.02 --halt-cooldown-bars 10`
  gave +200.44% with 264 rejections across six bounded, timestamped episodes. A live
  strategy is no longer permanently dead after one bad month. Runs now report *how
  many times* they halted rather than a bare boolean.
- **Costs, stated plainly: a re-arming switch will let a strategy resume into a
  continuing decline.** That is the price of not being permanently disabled. A
  cooldown re-arms on the calendar, not on evidence that anything improved, so in a
  2008-style grinding drawdown the switch will re-enter, halt, re-enter, and lose more
  than the latch would have. The bound is the design's promise, not the outcome: each
  additional episode costs at most another `max_drawdown_pct` from the resume level,
  and with a cooldown they cannot recur faster than every N+1 bars. Nothing here
  claims re-arming is *better* — it is a knob whose default is off, chosen per run,
  and paired with the honest metrics on the equity curve.
- The guardrails' peak is now a control variable that a re-arm rewrites, so it no
  longer equals the account's high-water mark when recovery is on. Reported drawdown
  is unaffected — it comes from `metrics.compute` over the equity curve — but anyone
  reading `Guardrails._peak` should know which it is.
- ADR-0013 §3's wording ("The halt latches for the session") is **amended** by this
  ADR: it latches for the session *by default*, and may be configured to re-arm. The
  ADR-0013 text now carries that pointer.
- Forecloses nothing: an equity-independent recovery signal (a benchmark that
  recovered, a volatility regime, a human ack in paper mode) remains additive on the
  same seam.
