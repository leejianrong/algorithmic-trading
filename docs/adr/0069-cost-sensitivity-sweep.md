# ADR-0069: A cost-sensitivity sweep names the bps level where the edge dies

- Status: Accepted
- Date: 2026-08-23
- Deciders: strategy developer (project owner)
- Tickets: KAN-618

## Context

Every backtest in this bench prices a fill at one fixed `slippage_bps` (5.0 by
default, ADR-0001/0004) and reports one headline Sharpe. That number is a point
estimate under one cost assumption, and the assumption is exactly the thing
`docs/monday-divergence-run.md` and ADR-0052 exist to question: measured fill
slippage varies by an order of magnitude across liquidity and asset class (0.51
bps on mega-caps, +4.23 bps on the S&P 500's thinnest decile, +13.02 bps on
crypto — ADR-0052/0063/0061). A strategy whose edge survives the model's 5 bps
but evaporates at 10 is not the same finding as one that is still profitable at
50, and today there is no way to tell the two apart without manually re-running
`--slippage-bps` by hand and eyeballing the difference.

The card names the risk precisely: "cost fragility is the most common way a
backtest lies." It matters most exactly where this bench is weakest — the
Build status log records that `sma_crossover` and `momentum` traded roughly 7x
more than `equal_weight` and lost to it, so a high-turnover strategy's Sharpe is
disproportionately exposed to the one number everything else in the report
holds fixed. `docs/research-playbook.md`'s robustness battery (KAN-862) already
lists a "cost-sensitivity sweep" as an open item alongside the parameter
heatmap (ADR-0065), regime split (ADR-0066), and Monte Carlo shuffle
(ADR-0067) that landed the same week; this closes it.

`sweep.py` already had the exact shape this needs: `_run_combo` takes a
`CostConfig` per call, and `run_sweep` already loops it over a strategy
parameter grid. The only new mechanism is varying the *cost* argument while
holding the strategy's own parameters fixed, and reporting the crossing point
rather than leaving the reader to eyeball a table.

## Decision

**A third sweep entry point, composed from the existing pieces, not a new
engine feature.** `sweep.run_cost_sensitivity_sweep(strategy, params, adapter,
symbols, start, end, *, slippage_bps, ...)` takes one fixed parameter
combination — no grid, no windows — and calls `_run_combo` once per level in a
caller-supplied `slippage_bps` grid (deduplicated and sorted ascending: "where
the edge dies" is a statement about cost *rising*, not about the order the
grid was typed on the command line). Each level gets a fresh
`SimulatedBroker`/`Guardrails`, exactly like every other sweep loop in this
module. `taker_fee_bps` and `commission_per_share` are held at the caller's
base `CostConfig` throughout — only `slippage_bps` varies, which is what keeps
the crossing-point interpolation below well-defined on a single axis. Refuses
(`ValueError`) a base cost model carrying a per-symbol tier
(`CostConfig.symbol_slippage_bps`, ADR-0063): sweeping the flat rate would not
move the effective rate on any symbol the tier actually applies to, silently
under-reporting exactly the symbols the tier exists to protect.

**"Where the edge dies" is a concrete interpolated number, not a table.**
`CostSensitivitySummary.edge_death(metric="sharpe"|"total_return", threshold=0.0)`
walks the levels ascending and returns an `EdgeDeath`:

- `already_dead=True` when the metric is already at/below the threshold at the
  *cheapest* level tested — there is nothing below the grid to interpolate.
- `survives_grid=True` when the metric never reaches the threshold at the
  *most expensive* level tested — the edge did not die within this grid,
  which is a narrower and more honest claim than "the edge never dies".
- otherwise a linear interpolation between the two tested levels that bracket
  the crossing — a piecewise-linear *estimate* of where the edge dies, not a
  claim about the true, generally nonlinear cost-response curve between them.

Threshold defaults to 0.0 (profitable vs. not) because it needs no second
input (a benchmark return, say) to be meaningful, and it is what "the edge
dies" means in the ticket's own words; a caller who wants "beats the
benchmark" can pass a different threshold.

**CLI: `trading sweep --slippage-sweep 5,10,25,50`**, off by default, on the
existing `sweep` command rather than a new one — the ticket's own suggested
shape. It re-runs the *plain sweep's own winning combo* (by `--rank-by`) at
every level in the grid: the plain grid sweep already ran and ranked every
strategy-parameter combination, so re-using its winner means the operator
does not have to separately spell out which parameters to hold fixed, and it
is exactly "feed a combo through, same as today" from the ticket's design
notes. With an empty `--param` grid (no strategy parameters to search) the
"winner" is just the strategy's own defaults, so `--slippage-sweep` works
standalone too. Mutually exclusive with `--slippage-bps` (an explicit
single-value override): the two options answer different questions and
combining them would leave one silently ignored for the swept dimension.
Follows `--stability`'s exact idiom (ADR-0065): a sibling
`<out>_cost_sensitivity.csv` next to `--out`, a printed table, and — new here
— one printed line naming the edge-death number, e.g. `Edge dies (~Sharpe
crosses 0) at ~29.15 bps (interpolated).` Not yet wired into `--folds`
walk-forward, printing the same "not yet wired" note `--stability` and
`--ledger` already print there — a walk-forward fold has no single winner
across the whole range to re-run at each cost level, the same shape gap.

**Byte-identical without the flag.** The main sweep table, CSV, and
significance block are computed before `--slippage-sweep` is even inspected;
a CLI test asserts the pre-"Wrote sweep results" stdout and the main CSV are
identical with and without the flag.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Fold cost levels into the existing `--param` grid (e.g. sweep `slippage_bps` as a strategy parameter) | `slippage_bps` is a `CostConfig` field, not a strategy constructor argument — `_build_strategy` has no seam for it, and conflating "strategy parameters" with "cost assumptions" in one grid would make `neighbor_stability` (ADR-0065) and `deflated_winner` (ADR-0039) treat a cost re-run as a competing *trial*, which it is not: it never chose anything, it re-prices one already-chosen combo. |
| A hard pass/fail threshold on the CLI ("warn if the edge dies below N bps") | The right threshold is strategy- and universe-dependent (ADR-0065 declined the same shape of feature for stability gaps, for the same reason) — printing the concrete crossing number and letting the reader judge is consistent with how `deflated_winner`'s probability and `stability`'s gap are already presented. |
| Sweep `taker_fee_bps` as a second grid axis (a full 2D cost surface) | The ticket's headline example is a one-dimensional bps grid, and a second swept axis would make `edge_death`'s single-axis interpolation ambiguous (which axis is "rising"?) without a real second use case yet. `taker_fee_bps` is still held fixed at the base cost model and reported per row in the CSV, so nothing about the shape is lost if a later card wants to add it — this is a known, named gap, not a foreclosed option. |
| A separate `trading cost-sensitivity` command | The ticket explicitly asks for a flag on `trading sweep`, and the mechanism is 90% shared with the existing sweep (same adapter construction, same market/risk/cost resolution, same CSV-sibling idiom) — a new command would duplicate all of that argument plumbing for one new flag's worth of behavior. |

## Consequences

- A plain sweep without `--slippage-sweep` is unaffected: same table, same CSV,
  same significance block, same runtime cost.
- The re-run is the sweep's own winning *combo*, not a fresh grid search at
  each cost level — a strategy tuned in-sample at 5 bps is tested at 10/25/50
  bps with the same parameters, which is deliberately what "how fragile is
  this specific finding to cost" asks. It does **not** ask "what parameters
  would be optimal at 50 bps" — that would be a different, more expensive
  re-optimization this card does not build.
- `--slippage-sweep` and `--folds` do not compose yet, the same gap
  `--stability` and `--ledger` already have against walk-forward — a
  walk-forward fold's winner changes per fold, so "the" combo to re-run is
  not well-defined without a design decision this card does not make.
- `taker_fee_bps` is held fixed rather than swept; a crypto cost-sensitivity
  sweep only varies slippage, not the venue fee. Named as an open gap, not
  silently assumed away.

## Verification

**Real worked example (`--source synthetic`, offline, deterministic,
seed 7, `AAA,BBB,CCC,DDD,EEE`, 2018-01-01..2022-12-31)** — the headline claim,
measured rather than asserted:

```
$ trading sweep --strategy sma_crossover --param fast=10 --param slow=50 \
    --symbols AAA,BBB,CCC,DDD,EEE --from 2018-01-01 --to 2022-12-31 \
    --source synthetic --seed 7 --slippage-sweep 5,10,25,50 \
    --out /tmp/sma_sweep.csv
...
Cost sensitivity: strategy=sma_crossover params={fast=10, slow=50} levels=4

slippage_bps  sharpe  total_return  max_drawdown
------------  ------  ------------  ------------
5             0.228   6.68%         10.64%
10            0.178   4.94%         11.00%
25            0.028   -0.12%        12.07%
50            -0.143  -5.60%        15.98%

Edge dies (~Sharpe crosses 0) at ~29.15 bps (interpolated).
```

```
$ trading sweep --strategy equal_weight \
    --symbols AAA,BBB,CCC,DDD,EEE --from 2018-01-01 --to 2022-12-31 \
    --source synthetic --seed 7 --slippage-sweep 5,10,25,50 \
    --out /tmp/ew_sweep.csv
...
Cost sensitivity: strategy=equal_weight params={} levels=4

slippage_bps  sharpe  total_return  max_drawdown
------------  ------  ------------  ------------
5             0.981   53.05%        9.21%
10            0.967   52.10%        9.23%
25            0.926   49.27%        9.31%
50            0.856   44.66%        9.43%

Edge survives this grid: Sharpe never crosses 0 within the levels tested.
```

`sma_crossover`'s Sharpe crosses zero at ~29 bps, 5.8x the model's default 5
bps; `equal_weight`'s Sharpe drops only 13% (0.981 → 0.856) over the same 10x
cost range and never comes close to zero. A plain `trading backtest` on the
same universe/range with each strategy's defaults confirms the mechanism —
turnover, not luck: `sma_crossover` 1362.97% vs. `equal_weight` 238.43% (5.7x,
consistent with the Build status log's "~7x more" figure) — exactly the
"high-turnover strategy dies faster under rising costs" example the ticket
asked to demonstrate.

**Fast tests:**
- `tests/unit/test_sweep.py`: `TestEdgeDeathPure` — interpolation between
  bracketing levels, input-order independence, already-dead at the cheapest
  level, survives-the-whole-grid, an exact zero at a tested level (not
  interpolated), `sharpe` vs. `total_return` read independently, an unknown
  metric raises, a custom threshold is honored, an exact threshold at the
  *cheapest* level is already-dead not interpolated (a mutation-caught case —
  changing `<=` to `<` on that check leaves this test red), no runs is
  `None`. 10 tests.
  `TestRunCostSensitivitySweep` — dedup+sort of an unsorted/duplicated input
  grid, params held fixed across every level, a high-turnover strategy's
  return never improves with more slippage, determinism, an unknown strategy
  raises, an invalid combo raises before running anything, an empty grid
  raises, a per-symbol tier is refused (ADR-0063), a dataless span is
  recorded as unusable rather than raised (ADR-0032), every run records its
  return moments, `taker_fee_bps`/`commission_per_share` are held at the base
  cost model, and the headline degradation-rate comparison itself
  (`sma_crossover` degrades more than `equal_weight` over the same grid). 12
  tests.
- `tests/unit/test_cli_sweep.py`: `TestSlippageSweepCli` — off by default
  writes no sibling file and prints nothing, the sibling CSV is written with
  the expected columns, the report re-runs exactly the plain sweep's own
  winner (cross-checked against calling `run_cost_sensitivity_sweep`
  directly, not merely "some CSV got written"), the printed edge-death line
  appears, `--slippage-bps`/`--slippage-sweep` are mutually exclusive, a
  malformed or negative level exits 2, `--folds` prints the not-yet-wired
  note and writes nothing, the main sweep CSV and pre-"Wrote sweep results"
  stdout are byte-identical with and without the flag, and a grid with no
  runnable combo prints no cost-sensitivity block. 12 tests.
- `make check` (ruff + ruff format + mypy --strict + the full fast layer,
  1619 tests) passes with these changes.
- Mutations, reverted one at a time and watched go red, then restored:

  | mutation | red |
  |---|---|
  | `edge_death`'s first-level check `<=` weakened to `<` | 1 |
  | `run_cost_sensitivity_sweep` stops deduplicating/sorting the input grid | 1 |
  | `run_cost_sensitivity_sweep`'s per-symbol-tier guard disabled | 1 |
- `make test-integration` (offline required layer) unaffected: 4 passed, 43
  skipped (no Alpaca credentials/SDK in this environment).
