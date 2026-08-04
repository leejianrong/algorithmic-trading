# ADR-0016: Parameter sweep / walk-forward as an outer loop over runs

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

Choosing strategy parameters (e.g. the SMA crossover's `fast`/`slow` windows)
means running the same strategy many times with different settings and comparing
the results. The roadmap in `SLICES.md` explicitly parks "parameter optimization
/ walk-forward" as *an outer sweep over runs*, out of the paper-mode milestone.

The mechanism question this slice settles: where does the sweep live? The engine
already runs one strategy over one range through one broker with one set of
guardrails (`Engine.run` -> `BacktestResult`), and V4 already computes headline
metrics from that result (`metrics.compute` -> `PerformanceMetrics`). A sweep is
"do that N times and rank"; the only real design choices are (1) whether it is an
engine capability or an outer loop, (2) how a grid becomes parameterized strategy
instances, and (3) how far walk-forward goes in this slice.

## Decision

**A sweep is a pure OUTER loop over `Engine.run`, not an engine feature.** A new
`trading.sweep` module expands a parameter grid into the cartesian product of its
axes, and for each combination builds a parameterized strategy, constructs a
*fresh* `SimulatedBroker` + `Guardrails`, runs the unmodified `Engine.run`, and
computes `metrics.compute` on the result. The engine, broker, sizing, guardrails,
and metrics are imported read-only and unchanged — the single execution path
(ADR-0002) and every domain invariant hold because each sweep run *is* an ordinary
backtest.

**The registry factory is the parameterization seam.** `STRATEGIES[name]` is the
strategy class itself, so `STRATEGIES[name](**combo)` constructs a configured
instance. The sweep is agnostic to which parameters a strategy takes; the grid
keys simply become constructor keyword arguments. A combination the constructor
rejects (e.g. `sma_crossover` with `fast >= slow`) is recorded in a `skipped`
list and its runs omitted, so one bad corner of the grid never aborts the sweep.

**Determinism is inherited, never introduced.** Nothing in the sweep path reads a
wall clock or an RNG; grid expansion order is fixed (grid-key order, first key
varying slowest) and the ranking sort is stable. The same strategy + grid +
adapter + range therefore always yields the same ranked summary. Seeding a
`SyntheticAdapter` makes a whole sweep offline and repeatable, exercised by the
fast test layer with no network.

**Walk-forward is a simple per-window grid sweep in this slice.** `--windows N`
splits `[from, to]` into N consecutive equal calendar spans and runs every
combination independently on each window (its own fresh broker/guardrails),
tagging each result with its window index and bounds. This deliberately does
*not* recombine an in-sample winner into an out-of-sample test — that anchored /
rolling in-sample -> out-of-sample selection is a later slice. `--windows 1`
(default) is a plain grid sweep. **Amended by ADR-0026**, which is that later
slice: `sweep.run_walk_forward` does the IS -> OOS recombination, while the
per-window grid sweep described here is unchanged and remains the in-sample
exploration tool.

**CLI surface.** `trading sweep --strategy … --param name=v1,v2,… --symbols … `
`--from … --to …` mirrors `backtest`'s option style (reusing `_parse_date`,
`_parse_symbols`, `_make_adapter`, and the guardrail flags), prints a table
ranked by Sharpe or total return, and writes a results CSV (one row per run, best
first, with a column per grid axis and the full metric set).

## Alternatives considered

| Option | Why not |
|--------|---------|
| Teach the engine to accept a parameter grid and loop internally | Bloats the one execution path with a concern that is purely outside a single run; a sweep run must be indistinguishable from a hand-run backtest. |
| Reuse a single broker/guardrails across combinations | Portfolio and kill-switch state would leak between runs, silently corrupting every result after the first. A fresh broker + guardrails per run is the only honest option. |
| Skip walk-forward entirely this slice | The window split is cheap and clean over the existing range-based `Engine.run`, and it lays the groundwork for true out-of-sample selection later. |
| Full anchored/rolling walk-forward with in-sample -> out-of-sample selection now | Adds a selection policy and its own ADR-worthy trade-offs; a plain per-window sweep is the right increment and keeps this slice focused. |
| Parallelize runs (threads/processes) | Premature: daily-bar backtests over small universes are fast, and parallelism would complicate determinism and error handling for no current benefit. |

## Consequences

- Adding a strategy or a metric needs no sweep change: new registry names sweep
  automatically, and the CSV carries whatever `PerformanceMetrics` exposes.
- The sweep is only as trustworthy as one backtest — it inherits, and cannot
  launder, the cost/slippage and no-look-ahead assumptions of `Engine.run`.
- Ranking by a single in-sample metric invites overfitting; the walk-forward
  windows are the honest counterweight, and a future slice can add out-of-sample
  selection on top of the same per-window runs without touching the engine.
- Forecloses nothing: the outer loop works identically over a future Alpaca-backed
  adapter (ADR-0004) or any new `DataAdapter`, since it depends only on the seam.
