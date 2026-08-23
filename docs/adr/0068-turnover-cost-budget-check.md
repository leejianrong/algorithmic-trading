# ADR-0068: A turnover/cost-budget check, reported and never enforced

- Status: Accepted
- Date: 2026-08-23
- Deciders: strategy developer (project owner)
- Tickets: KAN-860. Builds on ADR-0060/0061/0063 (per-market and per-liquidity-tier
  costs) and follows ADR-0029's shape (a computed, always-reported warning that
  never vetoes an order).

## Context

CLAUDE.md's own "NOT yet built" list named this card directly: an automated
cost-sensitivity check that "would fail a run loudly when its turnover times its
tier's rate exceeds a stated budget, the way ADR-0029's trades-per-parameter
warning already does." The arithmetic behind it already appears informally,
scattered across three cost-model ADRs, every time one of them wants to say a
number is bad: ADR-0058 measured a crypto backtest at **1454% annual turnover**
against a **25 bps** one-way rate, predicted **3.6%** of equity lost to cost from
that arithmetic, and then measured **4.0 percentage points** actually lost when the
fee was wired in (ADR-0060). Nobody had done that multiplication automatically
before deciding a strategy's turnover was reasonable for its asset class — an
operator did it by hand, if they remembered to.

Two things this bench already built make the check honest rather than a single
guessed constant. ADR-0060/0061 gave every market a *researched* cost posture
(`CostConfig.equity()`/`.crypto()`) rather than a flattering default, and
ADR-0063 (KAN-861) added `symbol_slippage_bps` — an optional per-symbol override
classified once from pre-run ADV, so a mixed-liquidity universe does not price its
500th name like a mega-cap. A single flat "the rate" would be wrong in either
direction the moment `--liquidity-tier-adv` is in play; the check needed the
run's own blended rate, not an assumed one.

## Decision

**Cost drag equals annual turnover times the effective one-way rate.** That is
the whole arithmetic, restated as a computed, always-reported figure instead of
something an operator works out by hand:

```
predicted_drag_pct = turnover * effective_rate_bps / 10_000
implied_max_turnover = cost_budget_pct / (effective_rate_bps / 10_000)
exceeds_budget = predicted_drag_pct > cost_budget_pct
```

### The effective rate is reconstructed from the run's own fills, not assumed

`metrics.effective_cost_rate_bps(fills, costs)` is a notional-weighted average of
`slippage_bps` (or its per-symbol `symbol_slippage_bps` override) plus
`taker_fee_bps`, over every fill the run actually made. This is a deliberate
choice among three options the ticket left open:

1. **A single static rate the caller supplies.** Rejected: it duplicates a number
   already sitting in the run's own `CostConfig`, and the two can drift — a run
   under `--liquidity-tier-adv` prices some symbols at a lower tiered rate and
   some at the market default, and a caller-supplied flat number would misstate
   whichever direction the universe actually skewed.
2. **The run's own `CostConfig`, unweighted (just `slippage_bps` + `taker_fee_bps`,
   ignoring tiers).** Rejected for the same reason: it silently reverts to a flat
   assumption exactly when tiering is doing its job, which is the corollary
   ADR-0063 exists to fix ("cost is a function of liquidity, not of asset class").
3. **The realized cost per unit turnover, measured against ADR-0038's live
   divergence data.** Considered and rejected for this card: a backtest fill's
   slippage is not something to *estimate* after the fact — it is exactly the
   `CostConfig`'s rate for that symbol, applied deterministically by
   `CostModel.fill_price`. Reconstructing it from `Fill.price` vs. some inferred
   reference would be re-deriving a number the broker already computed, with
   rounding noise as the only thing gained. The two questions are genuinely
   different: "what does our model predict this run cost" (this card, backtest-side)
   versus "was the model right" (ADR-0038's divergence report, which needs a real
   venue and is a separate, harder question this card does not touch).

Weighting by notional (rather than counting fills, or using the flat rate alone)
is what makes the blend honest under tiering: a fill on a $9,000 mega-cap leg and
a fill on a $1,000 thin-liquidity leg should not average their rates equally.

`None` — never `0.0` — when a run traded nothing at all: an absent rate is a
different fact from a rate of zero, the same distinction ADR-0029's
`trades_per_parameter` already draws for an unknowable ratio.

### The report: `CostBudgetReport`, `assess_cost_budget`

`metrics.assess_cost_budget(result, costs, cost_budget_pct, periods_per_year)`
computes the run's own `turnover()` (already built, ADR-0016-era) and
`effective_cost_rate_bps()`, and returns a `CostBudgetReport` carrying both plus
`implied_max_turnover` and `predicted_drag_pct`. It always returns a value —
never `None` — mirroring `RegimeReport`/`MonteCarloShuffleReport`'s convention
rather than `SharpeInterval`'s bare `None`: a run that traded nothing still has a
well-defined "no turnover, no cost, no constraint to violate" answer, carried in
`notes` rather than forcing every caller to special-case an absent object.

Three degenerate cases, all handled explicitly rather than by accident:

- **No fills at all** → `effective_rate_bps=None`, `implied_max_turnover=None`,
  `predicted_drag_pct=None`, a note explaining why, `exceeds_budget=False`.
- **A zero effective rate** (a commission-free, unslipped `CostConfig`) →
  `effective_rate_bps=0.0` (a real, known rate, not absent), but
  `implied_max_turnover=None` — there is no ceiling turnover could ever cross at
  a rate of zero — and `predicted_drag_pct=0.0`, so `exceeds_budget` is always
  `False`. A note says so rather than leaving a silent `None` for the reader to
  puzzle over.
- **A non-positive `cost_budget_pct`** → `ValueError` at the call site: a budget
  of zero or less admits no turnover at any positive rate, which is a caller
  mistake to reject loudly, not a data property to report on.

`CostBudgetReport.exceeds_budget` compares `predicted_drag_pct` against
`cost_budget_pct` directly rather than comparing `turnover` against
`implied_max_turnover` — the two are mathematically equivalent when the rate is
positive, but the drag comparison stays well-defined at a zero rate where the
turnover comparison would need a special case anyway.

### Reporting only — the ADR-0029 shape, not a guardrail

Exactly like the trades-per-parameter check, this never touches an order or a
run's outcome. `report.cost_budget_lines` prints the headline figures whenever
there was a rate to assess at all, and a `⚠` warning line only when
`exceeds_budget` is true — the same "compute it, then let the reader see it, warn
only when it actually crosses the line" shape `_regime_metrics_lines`/
`trades_per_parameter` already use. Nothing in `risk.py`, `broker.py`, or
`Engine._step` changes; a strategy at 1500% turnover still runs to completion,
loudly.

### CLI: `backtest --cost-budget-pct`, off by default

One new option, `--cost-budget-pct FLOAT` (e.g. `0.01` for 1%), `None` by
default. `_check_cost_budget_options` rejects a non-positive value **before** the
engine runs — the same pre-flight-validation shape `_check_bootstrap_options`/
`_check_monte_carlo_options` already use, so a typo does not throw away a
completed multi-year run. When passed, the report is computed **once** from the
run's own `costs` (the exact `CostConfig` the broker traded under, including any
`--liquidity-tier-adv` override — computed *after* that tiering is applied) and
handed to both `summarize` and `write_result_json`, mirroring
`--bootstrap`/`--regimes`/`--monte-carlo`'s exact shape. `sweep` is not wired in
this card (see Known gaps).

### `result.json`: additive, omitted when absent

`result_to_dict`/`write_result_json` gain a `cost_budget` parameter and, when
supplied, a top-level `cost_budget` key (`dataclasses.asdict` of the report).
`RESULT_SCHEMA_VERSION` stays **1**. Following `regimes`/`monte_carlo`
(ADR-0066/0067) rather than `significance`'s always-present-`null` convention:
the key is **omitted entirely**, not emitted as `null`, when the caller does not
supply a report. By the time this card landed, a baseline `result.json` hash was
already pinned across several ADRs, so an unconditional `null` would move that
hash for every run that never asked for the new feature — exactly the reasoning
ADR-0066/0067 already recorded, applied a third time.

## What was measured

Two runs on `--source synthetic` (offline, deterministic), both real invocations
of `trading backtest --cost-budget-pct`:

**A crypto run that trips the check.** `--market crypto --symbols
BTC/USD,ETH/USD,SOL/USD --strategy sma_crossover --interval 1h --from 2023-01-01
--to 2023-06-01 --cost-budget-pct 0.01`:

```
Turnover:      33989.14%
Cost budget:   1.00% of equity/year at 30.00 bps effective rate -> turnover 33989.1% -> predicted drag 101.97%
  implied max turnover at this budget/rate: 333.3%
  ⚠ predicted cost drag 101.97% exceeds the 1.00% budget — turnover 33989.1% is 102.0x the 333.3% this budget allows at 30.00 bps one-way
```

30 bps is `CostConfig.crypto()`'s 5 bps slippage plus its 25 bps taker fee — the
effective rate really is reconstructed from the run's `CostConfig`, not a
hard-coded number. This is the same shape as the card's own 1454%/25bps/3.6%
example, at a more extreme (and today, plausible) turnover for an hourly
crossover strategy on three coins.

**The same shape, staying silent.** `--market crypto --symbols BTC/USD,ETH/USD
--strategy buy_and_hold --interval 1d --from 2023-01-01 --to 2023-06-01
--cost-budget-pct 0.01`:

```
Turnover:      118.74%
Cost budget:   1.00% of equity/year at 30.00 bps effective rate -> turnover 118.7% -> predicted drag 0.36%
  implied max turnover at this budget/rate: 333.3%
```

No `⚠` line: `buy_and_hold`'s turnover sits comfortably under the 333.3% this
budget allows at 30 bps, and the block prints the headline figures without
issuing a warning it has not earned.

**Liquidity tiering changes the rate the check actually uses.** A three-symbol
equity run under `--liquidity-tier-adv 1 --liquidity-tier-slippage-bps 1.0`
(a floor every symbol clears) reports `effective_rate_bps: 1.00` instead of the
flat 5.0 default — confirming the blend reads the run's *own* `CostConfig` after
tiering is applied, not a number computed independently of it.

**Byte-identical without the flag.** The same `backtest` invocation with and
without `--cost-budget-pct` produces an identical `equity_curve.csv` and an
identical `result.json` once the additive `cost_budget` key is popped from the
payload that has it — confirmed directly (not merely asserted by a test) on a
real `AAPL,MSFT`/`sma_crossover` run.

## Alternatives considered

| Option | Why not |
|---|---|
| Enforce the budget as a guardrail (reject/clamp orders once exceeded) | The ticket is explicit that this is reporting, not a guardrail — mirroring ADR-0029, which warns rather than aborting. A turnover budget is a research-time judgement about a strategy's viability at a given cost structure, not a per-order risk limit `risk.py`'s guardrails already own; conflating the two would give one module two different jobs. |
| A single caller-supplied flat rate | Rejected in the Decision section above: it duplicates and can drift from the run's own `CostConfig`, and is wrong in either direction under `--liquidity-tier-adv`. |
| Measure the effective rate from ADR-0038's live divergence data instead of the modelled `CostConfig` | A different, harder question ("was the model right") that needs a real venue; this card answers "what does the model predict this run cost", which is answerable offline from the fills the backtest itself produced. |
| Emit `cost_budget` as always-present `null` (matching `significance`) | Rejected on the same grounds ADR-0066/0067 already recorded: a baseline `result.json` hash is already pinned by several earlier ADRs, and an unconditional `null` moves it for every run that never asked for this feature. |
| Compare `turnover` against `implied_max_turnover` instead of `predicted_drag_pct` against `cost_budget_pct` | Mathematically equivalent when the rate is positive, but `implied_max_turnover` is undefined at a zero rate — the drag comparison needs no such special case and is the more direct restatement of "cost drag equals turnover times rate". |

## Consequences

- A run without `--cost-budget-pct` is unaffected: no extra computation, no new
  line in the summary, no new key in `result.json` — confirmed above, not merely
  asserted.
- The check is genuinely free to compute (arithmetic over figures `turnover()`
  and the run's fills already produce), unlike `--bootstrap`/`--monte-carlo`, so
  there was no reason to gate it behind anything but "the caller must ask for a
  budget", which a `None` default already achieves.
- `sweep` does not get this flag in this card. `backtest` is where the CLAUDE.md
  corollary is stated and where a single run's own `CostConfig` is easiest to
  reach; wiring `sweep` (which shares no report-writing path with `backtest`) is
  a mechanical follow-up, not a new decision, left for whoever needs it next —
  the same posture ADR-0063 took for its own `--liquidity-tier-adv`.
- **Known gaps, left open:**
  - `sweep`/`paper` have no `--cost-budget-pct`.
  - The effective rate is a point-in-time reconstruction from the run's
    `CostConfig`, not a rolling one — a strategy whose turnover clusters in a
    few high-cost bars and is otherwise idle reads identically to one that
    trades the same total notional smoothly across the run. Turnover is already
    an annualized, whole-run average (ADR-0016-era `metrics.turnover`); this
    card inherits that granularity rather than introducing a new one.
  - No `result.json`/dashboard visualization of the *distribution* of per-bar
    turnover, only the whole-run figure — consistent with everything else
    `PerformanceMetrics` already reports, but worth naming since this is the
    first check whose entire point is a rate-times-volume product.
  - The check does not know a strategy's minimum viable turnover (e.g. a
    monthly-rebalance strategy forced below its natural cadence to fit a
    budget would still pass) — it only says whether the *observed* turnover fits
    the budget, not whether a different turnover would fit better.

## Verification

- `tests/unit/test_cost_budget.py`: `effective_cost_rate_bps` on a flat
  (untiered) `CostConfig`, a notional-weighted blend of a tiered and a default
  symbol (including an unequal-notional case proving it is not a plain average),
  `None` on no fills, and `0.0` on a fully free `CostConfig`; `assess_cost_budget`
  reproducing the card's own 1454%-turnover/25-bps/~3.6%-drag/~400%-ceiling
  example exactly via hand-built fixtures, a stays-silent-under-budget case, the
  no-fills and zero-rate degenerate cases, and a `ValueError` on a non-positive
  budget; `CostBudgetReport.exceeds_budget` exercised directly for the
  absent/exceeds/exactly-at-the-boundary cases. 13 tests.
- `tests/unit/test_report_cost_budget.py`: `summarize`/`result_to_dict` byte-identical
  when `cost_budget` is omitted or explicitly `None`; the block renders with its
  provenance and the `⚠` line only when the budget is actually exceeded; the
  no-fills case renders only the note; the block never adds a `Rejected:`/`Halt:`
  line (proving it is reporting-only); the `cost_budget` key is omitted from
  `result_to_dict`/`write_result_json` by default and present, `asdict`-shaped,
  and round-trips through JSON when supplied. 11 tests.

- `tests/unit/test_cli_cost_budget.py`: the flag is off by default (no block in
  stdout, no key in `result.json`, byte-identical `equity_curve.csv` and
  `result.json` with vs. without the flag once the additive key is popped); the
  block reaches both stdout and `result.json` from one computation; a tiny
  budget makes the warning fire and a generous one stays silent; liquidity
  tiering changes the effective rate the check actually uses; a non-positive
  `--cost-budget-pct` is rejected before the run (exit 2, no artifacts written).
  10 tests.
- `make check` (ruff + ruff format + `mypy --strict` + the full fast layer, 1621
  tests) passes with these changes.
- `make test-integration` (the offline, required CI layer) passes unchanged (4
  passed, 43 skipped on missing creds/SDK).
- Real CLI runs on `--source synthetic` reproducing the two headline numbers in
  "What was measured" above, plus the byte-identical-without-the-flag and
  liquidity-tiering checks, all run directly (not only asserted by tests) as
  part of this card's own verification.
