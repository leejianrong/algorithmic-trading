# ADR-0074: Walk-forward's own trial accounting, and `paper` gets `--bootstrap`/`--ledger`

- **Status:** accepted
- **Date:** 2026-09-01
- **Card:** KAN-677 ("paper has no `--bootstrap`, and walk-forward prints no
  deflation")
- **Builds on:** ADR-0039 (Sharpe significance: the bootstrap CI, the deflated
  Sharpe, `trial_count_note`'s LOWER BOUND caveat), ADR-0026 (true
  in-sample -> out-of-sample walk-forward, the one-OOS-run-per-fold discipline
  this ADR is careful never to violate), ADR-0059 (a derived statistic must be
  scored on the basis its population was computed on — the mixed-basis raise
  this ADR's `deflated_in_sample` reuses verbatim), ADR-0062 (the
  cross-invocation `TrialLedger`, whose `prior_trials` this ADR threads into
  two more entry points), ADR-0066 (`compute_regime_report`'s pattern of
  pooling a run's own discontiguous bars into one derived statistic — reused
  here to pool walk-forward folds)

## Context

Two gaps left by KAN-675 (ADR-0039's CLI wiring), both recorded in `CLAUDE.md`
rather than silently skipped:

1. `trading paper` had no `--bootstrap`, so a paper session could not report a
   confidence interval on its own Sharpe — arguably the run where it matters
   most, since paper results are survivorship-free (ADR-0027) while a curated
   backtest universe is not.
2. `sweep --folds` (true walk-forward, ADR-0026) printed no deflation of its
   own. It is the single most trial-heavy path in the bench — each fold tunes
   the *whole grid* in-sample before picking one winner — so if any run
   deserved trial accounting, it was that one. `--ledger`/`--hypothesis` were
   already accepted as CLI options but only honored on the plain-sweep path;
   passing them with `--folds` printed a "not yet wired" note and appended
   nothing.

## Decision

### Part 1 — `sweep --folds` gets its own deflation, never the OOS side

**What gets deflated is the in-sample optimization's own winner-selection
Sharpe, never the out-of-sample Sharpe.** ADR-0026's central discipline is that
each fold runs its OOS winner *exactly once*, and nothing about that result
ever feeds back into a selection — that is what makes the OOS number an
unbiased estimate. Treating `mean_out_of_sample_sharpe` as "best of N trials"
and deflating it would silently reintroduce exactly the peeking bug
walk-forward exists to prevent: OOS was never selected from a search, so there
is nothing to correct it for. What a search *did* produce, fold by fold, is
each fold's in-sample winner — chosen from `WalkForwardFold.candidates`
competing combinations — and that is what gets scored.

**The trial count is `(folds x grid size)`, not the grid size once and not the
fold count once.** Each fold reruns the whole grid in-sample, so a 3-fold
walk-forward over an 8-combo grid made 24 real trials, not 8. `WalkForwardFold`
gains `in_sample_candidate_sharpes` (every candidate's annualized IS Sharpe,
including the winner) and `WalkForwardSummary.in_sample_trial_count` sums it
across every **completed** fold — the same "count only what actually ran" rule
`SweepSummary.trial_count` already uses: a fold whose IS span raised
`EmptyUniverseError` never scored a candidate, and a fold whose IS succeeded
but whose OOS span then failed is excluded too (conservatively — its combos
were genuinely evaluated, but this counts a completed fold's search, not a
partial one, matching `unusable_folds`'s own "did not produce a result" line).

**The spread behind the correction (`sharpe_stdev` in
`expected_max_sharpe`) is the pooled in-sample candidate Sharpes across every
completed fold** — not the spread of just the fold *winners*. With the
default 3 folds, three winner Sharpes are far too few to estimate a
distribution's spread from, and using only them would silently discard the
very candidates that define how much luck a search this wide could produce.
Pooling every candidate a fold actually scored keeps the spread estimate
anchored in the real trial count, at the cost of an approximation named
explicitly in `WalkForwardSummary.deflated_in_sample`'s docstring: candidates
from an early, data-poor fold and a late, data-rich one are pooled as though
they were exchangeable.

**The observed Sharpe comes from the pooled per-bar returns of each fold's own
IS winner**, concatenated fold by fold and re-scored with
`metrics.return_moments` — the same discontiguous-bar-pooling pattern
`compute_regime_report` already uses for a run's own non-contiguous regime
slices (ADR-0066): a derived statistic for analysis, never a claim that this
was one tradeable curve. This is deliberately *not* the same number as
`mean_in_sample_sharpe` (which averages each fold's own annualized Sharpe):
`deflated_sharpe`'s `moments` argument needs one return series with a real
sample size, skew, and kurtosis, and re-deriving it from the pooled bars is
what supplies that — implicitly bar-count-weighted, where the mean-of-Sharpes
figure is not. The two are usually close and can differ when folds have very
different lengths; the docstring says so rather than leaving a reader to
notice a small unexplained gap.

**Basis mismatches raise**, mirroring `SweepSummary.deflated_winner` exactly
(ADR-0059/KAN-840): `deflated_in_sample`'s `periods_per_year` defaults to the
summary's own basis, and an explicit value that disagrees is refused with the
same "re-run at the basis you want" message — the trial Sharpes are fixed at
the basis they were annualized on, so scoring them against a different year's
null threshold has no correct answer to give.

**`--folds --bootstrap` brackets each fold's own out-of-sample Sharpe with a
confidence interval instead** (`sharpe_confidence_interval`,
`WalkForwardFold.out_of_sample_sharpe_interval`) — deliberately scoped to OOS,
the mirror image of the deflation's IS scope: OOS is the one curve per fold
that was genuinely *observed* rather than selected, so a confidence interval
on it answers the honest question "how much of this number is sampling
noise." Often `None` even with the flag: a fold's OOS span is a fraction of
the whole range and commonly falls short of `MIN_BOOTSTRAP_OBSERVATIONS`. The
IS deflation is **not** behind this flag — it is free arithmetic on numbers
the fold loop already produced (the same reasoning that keeps the plain
sweep's own deflation unconditional), so `sweep --folds` prints it on every
invocation now, exactly as a plain `sweep` has printed its deflation since
ADR-0039. `--bootstrap` on the plain-sweep path (no `--folds`) prints a note
and computes nothing: a plain sweep keeps each trial's `ReturnMoments`, not
its curve, so there is nothing to bootstrap.

**`--ledger`/`--hypothesis` now work on `--folds`.** One `TrialRecord` per
invocation, `command="sweep --folds"`, `trial_count=in_sample_trial_count`
(the full `folds x grid` search — not the grid once, so a later invocation's
`--ledger` sees the true size of what this run searched), `observed_sharpe`
set to `mean_out_of_sample_sharpe` — the run's own honest headline figure
(what the CLI prints as `OUT-OF-SAMPLE mean sharpe`), not the deflated IS
figure, which answers a different question. Nothing is appended when no fold
completes.

### Part 2 — `trading paper` gets `--bootstrap`/`--bootstrap-resamples`/`--bootstrap-seed` and `--ledger`/`--hypothesis`

No engine change was needed: `PaperSession.finalize()` already returns a
`BacktestResult` with the same `equity_curve` a backtest's does, so the exact
`backtest --bootstrap` pattern applies unchanged — `_assess_significance`
computed once, handed to both `summarize` and `write_result_json`, off by
default. `--ledger`/`--hypothesis` mirror `backtest`'s wiring exactly
(`trial_count=1`, since a paper session is always one trial, never a search).
Both flags are threaded to fire on *either* exit a paper session has: the
normal `--once` completion, and the `KeyboardInterrupt`/`SessionTerminated`
finalize path (ADR-0033/ADR-0043) — a session stopped mid-run by SIGTERM is
still one real trial, and that is the shape a live incubation run is actually
stopped in.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Deflate the mean OOS Sharpe | Reintroduces peeking: OOS was never selected from anything, so "best of N" has no meaning there. Exactly the mistake ADR-0026 exists to prevent, one abstraction layer up. |
| Trial count = grid size (once, not x folds) | Understates the real search by the fold count — a 3-fold walk-forward genuinely ran the grid three times, once in-sample per fold, and each of those runs had its own chance to overfit. |
| Trial count = fold count (one "trial" per fold) | Discards the very thing the correction is supposed to price: how wide the grid searched inside each fold. A 1-combo, 10-fold walk-forward and a 10-combo, 1-fold walk-forward are not equally exposed to selection bias. |
| Spread from fold-winner Sharpes only (one per fold) | Too few numbers (3 with the default fold count) to estimate a distribution's spread from, and discards the real trial population the search actually generated. |
| A single pooled `BacktestResult` stitching every fold's IS winner into one curve | Rejected in ADR-0026 itself ("Auto-refit and stitch the OOS folds into one continuous equity curve") as needing engine-level portfolio carry-over across folds — an engine change, out of scope here, and arguably the wrong number anyway (parameters differ fold to fold). Pooling *returns* for one derived statistic, as `compute_regime_report` already does, gets the needed input without claiming a tradeable curve exists. |
| Bootstrap the in-sample side instead of/alongside deflating it | A confidence interval answers "how much sampling noise is in this observed number" — meaningless for a number that was itself the output of a search across candidates, which is precisely what the deflation is for. The two statistics answer different questions about different things; assigning each to the side it is actually valid for (bootstrap -> OOS, deflation -> IS) is the resolution, not picking one. |
| Gate the IS deflation behind `--bootstrap` | Would be inconsistent with the plain sweep's own unconditional deflation (ADR-0039: "sweep needs no flag... prints under the ranking table always") — the same "free arithmetic, not a resampling procedure" argument applies unchanged to walk-forward's pooled IS statistic. |
| Give `paper` its own bespoke significance computation | `PaperSession.finalize()` already returns the same `BacktestResult` shape a backtest does; reusing `_assess_significance`/`summarize`/`write_result_json` verbatim is what keeps `metrics.py`/`report.py` from growing a second, paper-specific code path (ADR-0002's spirit, applied to reporting rather than the engine). |

## Consequences

- `sweep --folds`'s stdout changes on every invocation now (a new deflation
  block appears unconditionally, mirroring the plain sweep's own unconditional
  block since ADR-0039) — a deliberate, accepted change, not a bug. The
  **artifact** contract is what stays byte-identical: `_write_walk_forward_csv`
  gained no new columns, so the walk-forward CSV is byte-for-byte identical
  with or without `--bootstrap`/`--ledger`, and no ledger file is created
  unless `--ledger` is passed.
- `_run_combo` (private, used by all three sweep entry points) now returns a
  fourth element, the run's own equity curve — free, since `Engine.run`
  already produced it. `run_sweep`/`run_cost_sensitivity_sweep` discard it
  unchanged; only `run_walk_forward` uses it, for the IS-winner-returns pool
  and the OOS bootstrap curve. `SweepSummary`/`CostSensitivitySummary` and
  their CSVs are untouched.
- `WalkForwardFold` gains three additive fields, all defaulted so no existing
  hand-built fixture breaks: `in_sample_candidate_sharpes`,
  `in_sample_winner_returns`, `out_of_sample_sharpe_interval`.
  `WalkForwardSummary` gains `in_sample_trial_sharpes()`,
  `in_sample_trial_count`, `deflated_in_sample(...)`. `run_walk_forward` gains
  `bootstrap`/`bootstrap_resamples`/`bootstrap_seed`, all defaulted off.
- No engine change: `PaperSession`/`Engine.run`/`Engine._step` are untouched.
  `trading.engine` was not modified by this card at all.
- `RESULT_SCHEMA_VERSION` stays **1** — `paper`'s `result.json` already had a
  `significance` key (always `null` until now, per `write_result_json`'s
  existing signature); this card is the first thing that ever populates it.
  Walk-forward still writes no `result.json`; the deflation is summary-only,
  printed under the ranking table, matching how `deflated_winner()` already
  prints under the plain sweep's.
- Known gap left open, named rather than silently skipped: `--stability`
  (ADR-0065) and `--slippage-sweep` (ADR-0069) are still not wired into
  `--folds` — this card only closes the `--bootstrap`/`--ledger` gap KAN-677
  named. `docs/research-playbook.md`'s "not yet built" table needs the same
  re-check its own note already asks for.
