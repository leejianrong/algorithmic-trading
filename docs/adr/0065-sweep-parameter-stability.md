# ADR-0065: A sweep reports a combo's score next to its grid-neighbour mean

- Status: Accepted
- Date: 2026-08-18
- Deciders: strategy developer (project owner)
- Tickets: KAN-620

## Context

`trading sweep` writes a flat, best-first CSV: one row per `(combo, window)`,
ranked by Sharpe or total return. That ranking answers "which combo scored
highest" and nothing else. It cannot answer the question that actually matters
for whether a winner is worth trading: does this combo sit on a wide plateau of
nearby-scoring parameters, or is it a single spike surrounded by mediocre or bad
neighbours — a "parameter cliff" a real search would not land on reliably?

The two existing honesty layers do not answer this either. `SweepSummary.
deflated_winner` (ADR-0039) discounts the winner for how many trials competed,
but says nothing about the *shape* of the score surface around it. `run_walk_
forward` (ADR-0026) validates out-of-sample, but tests exactly one winning combo
per fold — it cannot tell you whether that fold's winner is a robust choice or a
lucky one, only whether it happened to work on data selection never saw.

The card records an encouraging signal noticed by hand: for `sma_crossover` on
real data, `fast=10, slow=100` won 2 of 3 folds in *both* the anchored and
rolling walk-forward modes. That is a piece of evidence for stability the tool
could not itself produce or check — a winner that keeps winning across
different in-sample windows and different windowing disciplines is doing
something a single lucky spike would not reliably do twice. This ADR does not
reproduce that specific finding (it is a walk-forward, real-data observation);
it builds the complementary tool that inspects the *grid itself*, so a plateau
or a cliff around a candidate like that is visible directly from one plain
sweep, without needing several folds and two modes to notice it by coincidence.

## Decision

**For every combo that ran, look up its immediate neighbour in each swept grid
dimension, holding every other parameter fixed, and report the combo's own
score next to the mean of whichever neighbours also ran.** For a grid of `fast`
x `slow`, combo `(fast=10, slow=100)`'s neighbours are the adjacent `fast`
values at `slow=100` and the adjacent `slow` values at `fast=10` — never a
diagonal move, and never a parameter that was not actually swept (a grid axis
with only one value has no neighbour and contributes nothing). "Adjacent" means
*positional* in the grid's own list order (the same order `expand_grid` reads),
not numeric distance — `fast=[5, 20, 10]` treats 20 as adjacent to both 5 and
10 regardless of the values' numeric spacing, because that is the order the
grid was actually searched in.

`gap = score - neighbor_mean`. A large positive gap is the cliff this ADR
exists to surface: a combo that scored far above what its neighbours scored,
an unstable optimum a real parameter search would not reproduce. A negative
gap is the opposite — a combo sitting in a local dip relative to its
neighbours. `neighbor_mean` and `gap` are `None`, never a fabricated `0.0`,
when a combo has no neighbour with a recorded score at all: every swept axis
put it at a grid edge, or every adjacent combo was rejected by the strategy
constructor (`SweepSummary.skipped`) or fell in a window that produced no data.
A rejected or never-run neighbour is silently excluded from the mean rather
than treated as a zero score, for the same reason `SweepSummary.skipped` exists
— a combo that never ran must not silently drag down (or inflate) another
combo's stability figure.

**Pure functions, read-only reporting.** `sweep.combo_key` gives a parameter
combo a canonical, hashable, order-independent identity (sorted `(name,
value)` pairs), so two combos built independently — a sweep run's own `params`
dict and a neighbour `neighbor_stability` assembles by mutating a copy —
compare equal regardless of each dict's own insertion order. `sweep.
neighbor_stability(grid, scores)` is the pure computation: given a grid and a
`combo_key -> score` mapping, it returns one `NeighborStability` per scored
combo. `SweepSummary.combo_scores(by="sharpe")` collapses a combo's window
repeats (under `--windows`) to their mean before anything compares it to a
neighbour, since the parameter surface a neighbour lives on is the *combo*, not
the `(combo, window)` pair. `SweepSummary.stability(by="sharpe")` is the
convenience method a caller actually uses: it builds the deterministic
`combo_scores` in the grid's own `expand_grid` order, filtered to combos that
produced a score, and calls `neighbor_stability`. None of this touches
`run_sweep`'s existing return value, ranking, or metrics computation — it is
read-only reporting layered on top of a `SweepSummary` that already exists.

**Additive, not a new required field.** `SweepSummary` gains one new field,
`grid: dict[str, list[object]]`, defaulted to `{}` — the same idiom every
other field on `SweepSummary` already uses (`skipped`, `empty_windows`,
`periods_per_year`) — so a hand-built summary in an existing test stays valid
and `stability()` on one returns `[]` rather than guessing a grid that was
never recorded. `run_sweep` is the only writer of the field. `run_walk_forward`
/ `WalkForwardSummary` are **not** touched — see Known gaps.

**CLI: `trading sweep --stability`, opt-in, off by default.** The same shape as
`backtest --bootstrap` (ADR-0039): a flag that costs something extra to compute
(here, negligible — it is arithmetic on scores already in hand, the same
"free" property `deflated_winner` has) but changes nothing about the existing
report unless asked for. With the flag: `SweepSummary.stability(by=rank_by)` is
computed and written to a sibling CSV, `<out>_stability.csv` (`results/
sweep.csv` -> `results/sweep_stability.csv`) — no new path-taking flag, the
same idiom `paper --divergence` uses for `fill_divergence.csv` living beside a
session's other artifacts rather than getting its own `--out`-style option.
When the grid has exactly two `--param` axes, a plain-text 2D ASCII heatmap
(rows = the first axis, columns = the second, cells = the combo's score,
blank `.` for a combo that never ran) prints under the "Wrote sweep results"
line — the literal heatmap the card offered as one shape this could take. With
more than two axes there is no single 2D picture to draw, so only the CSV is
written. Passing `--stability` together with `--folds` prints a note and
writes nothing, the same pattern `--ledger` already uses for the same walk-
forward gap (KAN-677).

## Alternatives considered

| Option | Why not |
|--------|---------|
| A hard "cliff" threshold that flags/warns on a combo | The right threshold is strategy- and grid-dependent, and a bright-line flag invites over-trusting a specific number over the shape of the surface. Reporting `gap` as a plain column lets the reader judge; a later slice can add a threshold once there is evidence for where to put it. |
| Resample/bootstrap the neighbour comparison | The neighbours are *already-run, deterministic* backtests on the same data — there is no sampling variability to bootstrap here, unlike ADR-0039's Sharpe CI. Nothing would be estimated; it would just be slower. |
| Fold this into `run_walk_forward` now, not just `run_sweep` | A fold's IS grid is scored once per fold and never retained beyond its winner (`WalkForwardFold` carries only the winning combo's metrics), so a neighbour-mean would need a new field to keep every fold's whole candidate list — a real change to that summary's shape, not a read-only addition on top of what it already returns. Left as a known gap rather than done partially. |
| Numeric-distance-aware neighbours (e.g. nearest by value, not by grid position) | The grid the user actually searched is a *list*, and `expand_grid` already treats it positionally — a combo between two list entries that are numerically far apart was still searched as adjacent. Reproducing what was actually searched, not re-deriving a notion of "nearby" from the values, keeps this consistent with the rest of the module's determinism-from-the-grid's-own-order design. |
| A CSV-only deliverable, no heatmap | The card names a literal heatmap as a nice-to-have. It costs little once the CSV data exists (same `NeighborStability` rows, formatted differently) and directly answers "is this a plateau" faster than scanning a CSV by eye for a 2-axis grid, so it is included but gated to exactly two axes rather than attempted for higher dimensions where it would not be a faithful picture. |

## Consequences

- A sweep's CSV output is unchanged unless `--stability` is passed; a daily
  equity sweep without the flag is byte-identical to before this ADR (no
  existing test needed updating, and the fast test layer's pre-existing sweep
  goldens were not touched).
- The new report is genuinely free: it reads scores already computed by
  `run_sweep`, the same way `deflated_winner` reads moments already computed —
  no extra backtest runs, no extra data fetches.
- `run_walk_forward` still has no neighbour-stability view of its own. Its own
  known gaps (KAN-677: no deflation block) and this one now sit together as
  the walk-forward path's under-built reporting relative to the plain sweep.
- The `fast=10, slow=100` observation from the card is not reproduced or
  disproved by this change — it was a walk-forward, real-data finding, and
  this ADR ships a grid-shaped tool, not a re-run of that experiment. A
  `--folds` version of this stability view (once built) is the natural way to
  check it directly against real data.
- **Known gaps, left open:**
  - `run_walk_forward` / `WalkForwardSummary` carry no neighbour-stability
    view; `--stability --folds` is refused with a note, not silently ignored.
  - No threshold or flagging on `gap` — the column is printed, the judgement
    is the reader's, same as `deflated_winner`'s probability is printed rather
    than gated on a hard-coded pass/fail.
  - The heatmap is plain ASCII with one score per cell (`+0.53`-style,
    2 decimals) — no colour, no `--rank-by`-aware scaling, no more than two
    axes. A dashboard panel (the other "heatmap" reading of the card) is not
    built; `result.json` does not carry a sweep at all today, so this stays a
    CLI-only, CSV/stdout deliverable.

## Verification

- `tests/unit/test_sweep.py`: `combo_key` order-independence; `neighbor_
  stability` on hand-built grids covering a center combo (all 4 neighbours), a
  corner combo (2 neighbours), a missing/rejected neighbour excluded from the
  mean rather than zeroed, a combo with zero recorded neighbours (`None`, not
  `0.0`), a single-value axis contributing no neighbours, positional (not
  numeric) adjacency on an out-of-order grid list, and a large positive gap on
  a deliberately spiked combo. `SweepSummary.stability`/`combo_scores` exercised
  through real `run_sweep` calls on the synthetic adapter: every combo that ran
  gets exactly one stability row, results match calling `neighbor_stability`
  directly on `combo_scores()`, results are deterministic across two identical
  sweeps, computing stability does not change `ranked()` / `trial_count` /
  `skipped` / `deflated_winner()`, a constructor-rejected combo is excluded
  from its neighbour's mean, `--windows` repeats collapse to their mean before
  comparison, and ranking by `total_return` reads that metric instead of
  Sharpe. 18 new tests.
- `tests/unit/test_cli_sweep.py`: `--stability` off by default writes no
  sibling file and the word "stability" never appears in stdout; with the
  flag, the sibling CSV exists, is named from `--out`, has one row per unique
  combo with the expected columns, is ranked best-first, and matches
  `SweepSummary.stability()` computed directly (not merely "some CSV got
  written"); the main sweep CSV and the pre-"Wrote sweep results" stdout are
  byte-identical with and without `--stability`; a 2-axis grid prints the
  ASCII heatmap and a 1-axis grid does not; `--stability --folds` prints the
  not-yet-wired note and writes no walk-forward stability file. 8 new tests.
- `make check` (ruff + ruff format + mypy --strict + the full fast layer,
  1443 tests) passes with these changes.
- Mutations, reverted one at a time and watched go red, then restored via
  `git checkout --`:

  | mutation | red |
  |---|---|
  | `neighbor_stability` stops excluding a missing/rejected neighbour (counts it as `0.0` instead) | 3 |
  | `SweepSummary.combo_scores` stops averaging window repeats (keeps the last window's score) | 1 |
  | CLI's `if stability:` gate replaced with `if True:` | 1 |
- Regression-safety baseline (unrelated to this change, run to prove no
  import-time side effect): `trading backtest --source synthetic --symbols
  AAPL,MSFT,NVDA --from 2020-01-01 --to 2022-01-01 --strategy sma_crossover`
  reproduces the pinned `equity_curve.csv` / `result.json` SHA-256 hashes
  exactly (see the PR description for the two digests).
