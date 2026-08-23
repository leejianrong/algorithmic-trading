# ADR-0071: A diversified naive baseline is a second, mandatory bar to clear

- Status: Accepted
- Date: 2026-08-23
- Deciders: strategy developer (project owner)
- Tickets: KAN-641. Builds on ADR-0037 (benchmark-relative metrics) and reuses
  ADR-0066/0067/0068's additive, omitted-when-absent `result.json` convention.

## Context

The 2000-2020 study's own headline finding, recorded in CLAUDE.md's build
status: `equal_weight` on `core10` beat SPY on return (**+306% vs +280%**),
Sharpe (**0.49 vs 0.42**), **and** drawdown (**44% vs 55%**). Nobody built
`equal_weight`/`core10` to win — it is the dumbest strategy in the registry, run
over a curated 10-ETF basket for no reason other than "hold everything, equally,
all the time". It won anyway. That is diversification doing the work, not edge.

`--benchmark SYMBOL` (ADR-0037) already answers "did this beat one unconstrained
buy-and-hold symbol" — almost always SPY. That is a real question, but it is the
*easy* one: a single-asset benchmark says nothing about whether a strategy's
apparent skill is actually just correlation to a diversified basket a naive
allocator could have held for free. A strategy that beats SPY by underweighting
tech and overweighting bonds during a bond rally has not demonstrated edge; it
has demonstrated that diversification beats concentration, which this bench
already measured once and should not have to re-discover by hand every time.

So this card makes the harder comparison a first-class, structurally identical
citizen: run a naive `equal_weight` allocation across a multi-asset basket
(`core10` by default — already built for exactly this "long-horizon, diversified"
purpose, ADR-0024) under the same cash/costs/dates as the strategy, and report it
the same way `--benchmark` is reported. A strategy that cannot beat *that* is not
earning its complexity, whatever it does against SPY.

## Decision

### Generalize `_run_benchmark`, don't duplicate it

`cli._run_benchmark` used to hardcode `buy_and_hold` and one symbol. It now takes
a `strategy_name: str` and `symbols: list[str]`, plus an optional `label: str` for
display — the exact machinery ADR-0037 already built (unconstrained guardrails,
the run's own `CostConfig`, the `EmptyUniverseError` → warning-and-`None` path,
`_warn_if_benchmark_never_invested`) now serves *both* comparisons. The existing
`--benchmark` call site passes `"buy_and_hold"` and `[bench_symbol]` — unchanged
behaviour, just spelled through the wider signature. The new diversified-baseline
call site passes `"equal_weight"` and the resolved basket. No second
error-handling path, no second "never invested" guard: one function, two callers.

This mirrors the reasoning ADR-0060/ADR-0063 already used for `CostConfig` — a
posture is a value passed through an existing seam, not a second branch of logic.

### CLI: `--diversified-baseline` / `--baseline-basket`, both off/default and opt-in

```
backtest --diversified-baseline
backtest --diversified-baseline --baseline-basket @blue20
backtest --diversified-baseline --baseline-basket AAPL,MSFT,GOOGL
```

- `--diversified-baseline` (bool, `False` by default) turns the whole comparison
  on. Off, the run pays nothing extra and prints exactly the bytes it always did.
- `--baseline-basket` (str, default `"@core10"`) selects the universe, reusing
  `_parse_symbols` — the exact `@name`-or-plain-comma-list parsing `--symbols`
  already has, so an unknown basket name fails the same way (`error: unknown
  basket ...`, exit 2) and a plain list needs no new code path. It is parsed
  **only** when `--diversified-baseline` is set, so a typo'd
  `--baseline-basket @nope` left on the command line without the flag is
  silently inert rather than a surprise failure on an unrelated run — the same
  "not chosen here" idiom `--target-vol`/the risk cap flags already use.
- The comparison strategy is always `equal_weight`, not a further flag. The
  ticket's own framing is "naive equal-weight allocation", and adding a
  `--baseline-strategy` knob would let the baseline itself become a tuned
  parameter — defeating the point of a fixed, boring yardstick.
- `_check_symbol_shapes(chosen_market, baseline_symbols)` runs on the resolved
  baseline universe too, for the same reason it runs on `--symbols` (ADR-0057):
  a crypto-shaped baseline basket under an equity-closing market is exactly the
  silent-wrong-annualization mistake that guard exists to catch, cheap to check
  again since it early-returns on a continuous market.

### `equal_weight` needs no changes

`EqualWeight` (in the registry since V2) already rebalances to
`invested / len(bars)` across whatever symbols it is given, every bar. It fits
this job with zero new code: it is exactly "hold everything, equally, all the
time" — the mechanism behind CLAUDE.md's own headline finding.

### Reporting: the same shape as `--benchmark`, deliberately

`report.diversified_baseline_lines` renders:

```
Diversified baseline (equal_weight/core10): +64.51% (strategy -25.98% vs baseline)
Baseline beta:           0.61
Baseline alpha (ann.):   +0.51%
Baseline correlation:    0.66
Baseline info ratio:     -0.21
Baseline ret/exposure:   +26.15% vs baseline +6.56% (annualized return per unit of avg exposure; 15.93% vs 97.95% invested)
```

right after the `--benchmark` block (when both ran) — total return side-by-side,
then beta/alpha/correlation/information ratio computed by the *same*
`metrics.compare_to_benchmark` ADR-0037 already built (it was already generic
over "any second equity curve", so no change was needed there either), then the
exposure-adjusted return. A reader should be able to scan "beat SPY? beat naive
diversification?" as two answers in the same format, not two different reports.

The never-invested/invested-late honesty check ADR-0037 built for `--benchmark`
matters at least as much here: a diversified baseline that could not fund its
first rebalance across ten symbols (more legs, more chances for one leg's
insufficient-cash rejection to matter) must not print a flattering `+0.00%`
either. Rather than re-deriving this from the CLI (where the original lives as
`_benchmark_deployment_lines`, printing directly), it is now computed **once**,
in `metrics.assess_diversified_baseline`, as plain-text `notes` on the returned
`DiversifiedBaselineReport` — the same `notes: list[str]` convention
`RegimeReport`/`SignificanceReport`/`CostBudgetReport` already use. That is a
deliberate departure from `--benchmark`'s CLI-only rendering: a diversified
baseline's health is exactly the kind of fact that must reach `result.json`
too (a dashboard consumer has no access to stderr), so it is data on the report
object rather than a side effect of printing it.

### `metrics.DiversifiedBaselineReport` / `assess_diversified_baseline`

```python
@dataclass(frozen=True, slots=True)
class DiversifiedBaselineReport:
    label: str
    symbols: tuple[str, ...]
    metrics: PerformanceMetrics
    comparison: BenchmarkComparison
    notes: list[str] = field(default_factory=list)
```

Self-contained rather than a view over fields `result.json` already carries
elsewhere — unlike `benchmark_metrics`, which is joined against the separate
top-level `benchmark_curve` (ADR-0037's `_benchmark_metrics_block` derives it
lazily from two curves already in the document). There is exactly one baseline
run behind this report and no top-level `diversified_baseline_curve` sibling key,
so its own `PerformanceMetrics` (return, Sharpe, drawdown) travels alongside the
relative `BenchmarkComparison` rather than requiring a second lookup a consumer
would have to reconstruct by hand. `symbols` records the basket actually traded
(`BacktestResult.symbols`) so `result.json` is self-describing without needing
`--baseline-basket`'s raw CLI string.

`assess_diversified_baseline(result, baseline, periods_per_year, *, label)`
always returns an object — never `None` — mirroring `compare_to_benchmark`/
`assess_cost_budget`'s contract: the *caller* decides whether to render it.

### `result.json`: additive, omitted when absent — the KAN-860/KAN-859 convention

```json
"diversified_baseline": {
  "label": "equal_weight/core10", "symbols": ["SPY", "QQQ", ...],
  "metrics": { ...PerformanceMetrics... },
  "comparison": {"shared_bars": int, "beta": float | null, "alpha": float | null,
                 "correlation": float | null, "information_ratio": float | null},
  "notes": []
}
```

Following `regimes`/`monte_carlo`/`cost_budget` (ADR-0066/0067/0068) rather than
`significance`/`benchmark_metrics`'s always-present-`null` convention: the key is
**omitted entirely**, not emitted as `null`, when the caller does not supply a
report. A baseline `result.json` hash was already pinned across those three
ADRs before this one landed, so an unconditional `null` would move that hash for
every run that never asked for this feature — the same reasoning, applied a
fourth time. `RESULT_SCHEMA_VERSION` stays **1**.

## What was measured

Real `--source yfinance` runs, five names, 2015-01-01 to 2023-01-01 (real
network calls, not synthetic data — this comparison is only meaningful measured
against real market history):

**A strategy that beats both baselines.** `sma_crossover` on
`AAPL,MSFT,GOOGL,AMZN,JPM`, `--benchmark SPY --diversified-baseline`:

```
Total return:  +118.17%
Benchmark (SPY): +115.83% (strategy +2.34% vs benchmark)
Diversified baseline (equal_weight/core10): +64.51% (strategy +53.66% vs baseline)
Baseline beta:           0.46
Baseline alpha (ann.):   +7.31%
Baseline correlation:    0.48
Baseline info ratio:     0.26
```

Beats SPY narrowly (+2.34pp) and beats the diversified baseline by a wide margin
(+53.66pp) — the harder bar is, correctly, not automatically the tighter one:
five concentrated mega-caps outran a 10-asset-class basket over a period that
happened to favor concentrated tech/financials, which is exactly the kind of
regime-dependent fact a single SPY comparison would have hidden.

**A strategy honestly flagged as underperforming both.** `mean_reversion` on the
same five symbols, same range, same flags:

```
Total return:  +38.54%
Benchmark (SPY): +115.83% (strategy -77.29% vs benchmark)
Diversified baseline (equal_weight/core10): +64.51% (strategy -25.98% vs baseline)
Baseline beta:           0.61
Baseline alpha (ann.):   +0.51%
Baseline correlation:    0.66
Baseline info ratio:     -0.21
```

Both deltas are negative and printed as such — no number was rounded, tuned, or
selectively reported to force a conclusion either direction; this is the honest
result of the second real run.

**`--baseline-basket` composes with a plain symbol list.** `sma_crossover` on
`AAPL,MSFT`, `--diversified-baseline --baseline-basket AAPL,MSFT,GOOGL` (no
`@name`), 2020-01-01 to 2021-01-01:

```
Diversified baseline (equal_weight/AAPL, MSFT, GOOGL): +49.82% (strategy -26.26% vs baseline)
```

Confirms the override path (not just the `@core10` default) reaches a real run
and labels itself correctly from the raw CLI string.

**Byte-identical without the flag.** `equity_curve.csv` is byte-for-byte
identical with and without `--diversified-baseline` on the same synthetic
invocation, and `result.json` is identical once the additive
`diversified_baseline` key is popped from the payload that has it — the same
proof-not-assertion `make check`'s CLI tests already pin, additionally confirmed
in this ADR's own verification run against `--source synthetic`.

## Alternatives considered

| Option | Why not |
|---|---|
| A `--baseline-strategy` flag letting the comparison strategy vary | Turns the yardstick itself into a tunable parameter, defeating the point of a fixed, boring baseline that answers "did you beat naive diversification", not "did you beat some other strategy I picked". |
| Fold the diversified baseline into `--benchmark` (accept a comma list there) | `--benchmark` is documented and tested as *one symbol, buy-and-hold, unconstrained* — widening its contract would be a breaking change to an existing, pinned flag for a semantically different question (single-asset exposure vs. multi-asset diversification), and would make "which comparison did this number come from" ambiguous in `result.json`. |
| Compute the never-invested/invested-late check only in the CLI (mirroring `--benchmark`'s original placement) | Rejected: this bench's own convention (`RegimeReport`/`SignificanceReport`/`CostBudgetReport`) already puts caller-facing honesty notes on the report object so they reach `result.json`, not just stdout — a dashboard consumer with no stderr needs the same caveat a terminal operator sees. |
| A separate top-level `diversified_baseline_curve` key, mirroring `benchmark_curve` | Rejected as unnecessary indirection: unlike `--benchmark`'s design (which predates `BenchmarkComparison` and derives it lazily from two already-present curves), this feature was built with the comparison object as the primary artifact from day one, so there is no reason to split it across two top-level keys a consumer must join. |
| Emit `diversified_baseline` as always-present `null` (matching `significance`) | Rejected on the same grounds ADR-0066/0067/0068 already recorded: a baseline `result.json` hash is already pinned by three earlier ADRs, and an unconditional `null` moves it for every run that never asked for this feature. |

## Consequences

- A run without `--diversified-baseline` is unaffected: no extra backtest run,
  no new line in the summary, no new key in `result.json` — confirmed above by
  a real CLI invocation, not merely a unit test.
- The comparison costs a full second backtest run (unconstrained, same
  cost/cash), the same price `--benchmark` already pays — not free, but bounded
  and opt-in.
- `_run_benchmark`'s generalization is used by exactly two callers today
  (`--benchmark` and `--diversified-baseline`); a third comparison of this shape
  (e.g. a sector-rotation baseline) would reuse it with no further change.
- **Known gaps, left open:**
  - `sweep`/`paper` have no `--diversified-baseline`, the same gap
    `--cost-budget-pct`/`--regimes`/`--monte-carlo` already have on those
    commands.
  - The paired bootstrap (`--bootstrap`, ADR-0039) still reads only
    `--benchmark`'s curve for its win-rate figure; a diversified-baseline-paired
    win rate ("beats naive diversification in X% of resamples") is a natural
    follow-up but is not built here — scope-contained to the reporting
    comparison this ticket asked for.
  - No CSV column for the diversified baseline's equity curve (unlike
    `--benchmark`'s `benchmark_equity` column in `equity_curve.csv`) — the
    ticket's ask was the printed comparison plus the additive `result.json`
    block, and a curve column is a mechanical follow-up if a dashboard panel
    needs it later.
  - `core10`'s own survivorship caveat (ADR-0027 amended) applies to the
    baseline exactly as it always has to any `core10` run: it is *reduced*
    survivorship bias, not *removed*.

## Verification

- `tests/unit/test_diversified_baseline.py`: `assess_diversified_baseline`
  computes the baseline's own metrics and the relative comparison; the
  never-invested and invested-late notes fire correctly (including quoting the
  first rejection); a healthy baseline has no notes; too few shared bars leaves
  `beta`/`alpha` undefined rather than fabricated. 6 tests.
- `tests/unit/test_report_diversified_baseline.py`: `summarize` is
  byte-identical when the report is omitted or explicitly `None`; a
  `--benchmark`-only run does not trigger the block; the block prints its label
  and return, sits after the `--benchmark` block when both are present, surfaces
  the never-invested caveat, prints the relative stats when there are enough
  shared bars and says so instead when there are not; the block never adds a
  `Rejected:`/`Halt:` line; `result_to_dict`/`write_result_json` omit the key by
  default and emit an `asdict`-shaped, JSON-round-trippable block when supplied.
  14 tests.
- `tests/unit/test_cli_diversified_baseline.py`: the flag is off by default (no
  block in stdout, no key in `result.json`, byte-identical `equity_curve.csv`
  and `result.json` with vs. without the flag once the additive key is popped);
  the block reaches both stdout and `result.json` from one computation using the
  run's own `equal_weight` allocation; the default basket is `@core10`'s ten
  symbols; a different `--cash` changes the baseline's own metrics (proving it
  reuses the run's cash, not a hardcoded default); an unknown `--baseline-basket`
  name exits 2 only when `--diversified-baseline` is actually passed. 9 tests.
- `make check` (ruff + ruff format + `mypy --strict` + the full fast layer, 1698
  tests) passes with these changes.
- `make test-integration` (the offline, required CI layer) passes unchanged (4
  passed, 43 skipped on missing creds/SDK).
- Real `--source yfinance` CLI runs (network calls made, not synthetic) producing
  both headline examples in "What was measured" above, plus the
  `--baseline-basket` override and the byte-identical-without-the-flag checks,
  all run directly as part of this card's own verification.
