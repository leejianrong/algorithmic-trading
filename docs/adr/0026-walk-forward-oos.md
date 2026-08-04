# ADR-0026: True in-sample -> out-of-sample walk-forward, one OOS run per fold

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

`run_sweep` (ADR-0016) runs a parameter grid and ranks it by Sharpe or total
return. Every number in that ranked table is **in-sample**: the same bars that
chose a combination also scored it, so the top row is by construction the most
overfit row in the table, and it gets more overfit the more grid points are added.
`--windows N` split the range into consecutive spans, but ran the *whole grid* on
each span independently, so it produced more in-sample numbers rather than an
out-of-sample one. ADR-0016 said so plainly and parked "anchored / rolling
in-sample -> out-of-sample selection" as a later slice; the module docstring
repeated the caveat.

That recombination is the central discipline of honest validation, and this bench
exists to favour honest numbers over flattering ones: before any of this earns
paper capital, at least one reported figure must come from data that no selection
step ever looked at. This ADR is that slice. The questions it settles are how the
range is cut into folds, how the in-sample (IS) window is shaped, how many
out-of-sample (OOS) runs a fold is allowed, what the result reports, and what
happens when the input cannot support a walk-forward at all.

## Decision

**Walk-forward is a second outer loop in `trading.sweep`, not an engine feature.**
`run_walk_forward` reuses `expand_grid`, `split_windows`, the `_RANK_KEYS` ranking
definitions, and the same "fresh `SimulatedBroker` + `Guardrails` per run" rule as
`run_sweep`, through shared internals (`_run_combo`, `_partition_grid`,
`_rank_key`). Every run it performs *is* an ordinary `Engine.run` backtest, so the
single execution path (ADR-0002) and every domain invariant hold unchanged.
`run_sweep`'s signature, behaviour, and output are untouched — the two loops sit
side by side so an in-sample table can never be mistaken for an out-of-sample one.

**Folds are `folds + 1` equal segments; fold *k* tests on segment *k + 1*.**
`split_folds(start, end, folds, mode=...)` returns frozen `FoldSpans`
(`index`, `is_start`, `is_end`, `oos_start`, `oos_end`) built by pure arithmetic on
the two datetimes — no clock, no calendar service, no RNG. The OOS spans march
forward without overlapping and the last one ends exactly at `end`.

**`mode="anchored"` is the default; `mode="rolling"` is explicit.** Anchored
(expanding) IS always starts at `start`, so each fold optimizes on every bar
available before its OOS span — the right default for a bench with short histories,
where throwing data away costs more than stale regimes do. Rolling IS is the single
segment immediately before the OOS span, a fixed-length window that slides and
forgets old regimes; it is the honest choice when you believe the market's character
changes. The mode is a named parameter, recorded on the result, and both are
deterministic.

**The fold boundary *day* belongs to in-sample.** `oos_start` is midnight of the
day after `is_end`, not `is_end` itself and not `is_end` plus a microsecond. A
sub-second offset is enough for an adapter that filters on exact timestamps, but
not for one that filters at day granularity (`SyntheticAdapter` truncates a request
to whole days), and the point of a boundary is that no single bar can be both an
optimization input and an out-of-sample observation. This is ADR-0001's
no-look-ahead rule applied to validation.

**Exactly one OOS run per fold, on the combination IS alone chose.** Per fold:
run every runnable combination over the IS span, rank by `rank_by` with a stable
argmax (ties keep grid-expansion order), then run *that one* combination once over
the OOS span. Nothing about an OOS result feeds back into any selection, ever.
This is the load-bearing rule of the whole feature — it is what makes the OOS
figures an estimate rather than a second optimization — and it is pinned by a test
that counts the adapter's requests per span, not one that merely reads the numbers,
plus a rigged fixture where the IS winner is deliberately the worse OOS combination.

**The result spells out which numbers are honest.** `WalkForwardFold` carries the
fold index, both spans, the winning `params`, `in_sample_metrics`,
`out_of_sample_metrics`, the `candidates` count, and each span's bar count.
`WalkForwardSummary` adds aggregates whose names cannot be misread:
`mean_in_sample_sharpe`, `mean_out_of_sample_sharpe`,
`median_out_of_sample_sharpe`, the matching total-return figures,
`sharpe_degradation` (mean IS Sharpe minus mean OOS Sharpe — how much of the edge
was fitting), `sharpe_retention` (the ratio, `None` when the IS mean is not
positive, because a ratio against a non-positive base is meaningless rather than
zero), and `folds_with_positive_out_of_sample_return`.

**Degenerate input is surfaced, never silent and never fatal.** A range or fold
count that cannot form even one IS/OOS pair, and a grid whose every combination the
strategy constructor rejects, land in `warnings`; per-fold failures land in
`unusable_folds` as `(index, reason)`; rejected combinations land in `skipped`,
exactly as in `SweepSummary`. A span too short to define a return still produces a
fold, with a warning naming the bar count, so a structurally-zero metric block
cannot read as a flat result. Only an unusable *argument* raises: an unknown
strategy (`KeyError`), an unknown mode or rank key (`ValueError`).

**This ADR amends ADR-0016.** Where ADR-0016 says the in-sample -> out-of-sample
recombination "is a later slice", that slice is this one; ADR-0016's per-window grid
sweep remains valid and unchanged as the in-sample exploration tool.

## Alternatives considered

| Option | Why not |
|--------|---------|
| K-fold cross-validation / shuffled splits | Trains on bars that come after the test bars — look-ahead by construction, which ADR-0001 forbids outright. Time series only split forward. |
| Report the best OOS combination per fold | That *is* re-selection on out-of-sample data; the number stops being out-of-sample the instant it chooses anything. It would quietly reintroduce exactly the bias this slice exists to remove. |
| A single global train/test split | One observation, no distribution: a lucky (or unlucky) test period says nothing about stability, and it wastes the rest of the history. Folds give several independent OOS observations for the same data. |
| Offer only rolling, or only anchored | The choice encodes a belief about regime persistence that belongs to the caller, not the library; both are a few lines over the same segment arithmetic. |
| Purged / embargoed CV (Lopez de Prado) | Heavier machinery aimed at overlapping labels; with daily bars, no labels, and a whole-day fold boundary there is no bar overlap to purge. Revisit if label horizons or intraday leakage ever matter. |
| Auto-refit and stitch the OOS folds into one continuous equity curve | The most useful next step, but it needs portfolio carry-over across folds (cash and positions surviving a refit), which is an engine-level concern. Per-fold metrics first; a stitched curve can be built on these folds later without changing them. |
| Fold the OOS test into `run_sweep`'s `windows` | Would change the meaning of an existing, tested output and blur the in-sample/out-of-sample line in one summary. A separate function with separate types keeps the distinction impossible to lose. |
| Raise on a grid or range that cannot walk forward | A validation tool that dies on one bad corner gets skipped; `run_sweep`'s `skipped` list already set the precedent of reporting instead of aborting. |
| An independent IS-length parameter for rolling mode | More knobs, more ways to draw incomparable folds. Fold count already controls IS length (`folds + 1` segments); add the knob only when a real run needs it. |

## Consequences

- One walk-forward now yields a number worth quoting: the mean/median OOS Sharpe,
  next to the IS figure it degraded from. `sharpe_degradation` makes the cost of
  parameter fitting a reported quantity instead of a suspicion.
- **This does not fix survivorship bias.** The universe is still whatever symbols
  were passed in, and `blue20` (ADR-0024) is a curation of today's mega-caps —
  companies that failed are absent from every fold, IS and OOS alike. Honest
  parameter selection over a dishonest universe is still dishonest.
- **Multiple-comparison bias still accrues across runs.** One walk-forward gives an
  unbiased OOS estimate of one grid. Re-run it with twenty grids and keep the best
  OOS number and that number is in-sample again — the selection just moved up a
  level, where no code can see it. Only discipline fixes this: few grids, decided in
  advance, every run logged.
- **Fold count is a bias/variance tradeoff, and the default (3) is a judgement, not
  a fact.** More folds mean shorter spans: noisier per-fold estimates, but more
  observations and a more recent-weighted picture. Fewer folds mean longer, steadier
  spans and fewer of them. Anchored mode additionally makes early folds train on
  much less data than late ones.
- Each fold's runs start fresh at `cash`, so OOS folds are separate experiments
  rather than one compounding account; the summary reports a distribution of fold
  outcomes, not a tradeable equity curve.
- A caveat for offline verification: `SyntheticAdapter` reseeds per call and
  generates from the requested start day, so two spans of equal length replay the
  *same* path. Synthetic data therefore cannot demonstrate real IS→OOS degradation
  (rolling mode shows ~0 by construction); the rigged fast test uses a hand-built
  `FakeAdapter` path instead, and meaningful degradation figures need real data.
- The OOS numbers inherit every assumption of one backtest — costs, slippage,
  next-open fills, adjusted prices (ADR-0008), enforced guardrails (ADR-0009). A
  walk-forward launders none of them; it only removes the parameter-selection bias.
