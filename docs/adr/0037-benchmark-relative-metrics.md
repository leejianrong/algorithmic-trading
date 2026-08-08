# ADR-0037: Benchmark-relative metrics, and return per unit of exposure

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

`--benchmark SYMBOL` has existed since V4 and produced exactly two things: a
`benchmark_equity` column in the equity CSV and one line in the summary comparing
total returns. Nothing computed a *relation* between the two series. Every
question worth asking of a benchmark — how much of the return was just market
exposure, whether the strategy tracked or diversified, whether the active bet was
worth its tracking error — had to be hand-computed in a throwaway script during
the 2000-2020 study.

Worse, the raw comparison is misleading whenever the strategy is not fully
invested. `mean_reversion` averaged **17% gross exposure** in that study; a
buy-and-hold benchmark is ~100% invested by construction. Comparing their total
returns compares a mostly-cash book against a fully-invested one and calls the
difference skill. There is no interest-on-cash model in this bench, so the idle
83% earns exactly nothing — the comparison is not merely unfair, it is measuring
allocation rather than edge. The exposure-adjusted view **changed the ranking**
when it was finally computed by hand.

Two facts about the benchmark shape the design. It is **optional** (off by
default), and since PR #36 a benchmark that cannot be run *warns and returns
`None`* rather than aborting. So "no benchmark" is a normal, frequent state that
the metrics must express cleanly — not as a zero, not as a NaN.

## Decision

**Five new figures**, all in `metrics.py`, all following the conventions already
fixed by Q17 (simple per-bar returns, zero risk-free rate) and ADR-0022
(`periods_per_year` as the single annualization knob). No second convention is
invented.

| Figure | Definition | Undefined when |
|--------|-----------|----------------|
| `beta` | `cov(r_s, r_b) / var(r_b)`, sample (`n-1`) basis | < 2 aligned returns; zero-variance benchmark |
| `alpha` | `(mean(r_s - rf) - beta·mean(r_b - rf)) × periods_per_year` | whenever `beta` is |
| `correlation` | Pearson of the aligned returns | < 2 aligned returns; either side flat |
| `information_ratio` | `mean(r_s - r_b) / stdev(r_s - r_b) × √periods_per_year` | < 2 aligned returns; zero tracking error |
| `return_per_unit_exposure` | `annualized_return / avg_exposure` | average exposure ≤ 0 |

**Alignment by timestamp, never by position.** `align_curves` keys both equity
series by their bar timestamp and intersects them; returns are taken *after*
alignment, between consecutive shared timestamps. Positionally zipping two curves
that differ in length or have different gaps would pair bar *i* of one against an
unrelated bar *i* of the other and manufacture a correlation out of the offset —
which is precisely the error a throwaway script makes. A benchmark that starts
late, ends early, or misses a day contributes only where it genuinely lines up,
and the report prints `Bench overlap: N of M strategy bars` whenever the coverage
is partial, so no figure silently describes a shorter span than the run.

Where the benchmark has a gap, the step that bridges it is longer than one bar —
but it is longer on *both* sides, so the same calendar span is measured either
way. That is the honest pairing; the cost is a slightly uneven sampling grid,
recorded here rather than hidden.

**Absence is structural, not a sentinel.** `BenchmarkComparison` is a value object
that exists only when a benchmark ran. There is no "benchmark comparison with
zeroed fields": no benchmark means no object. A statistic *inside* a present
comparison is `None` when it is mathematically undefined, which is a different
fact and reads differently — the report prints `n/a`, the dashboard prints `n/a`,
and neither ever prints `0.00` for something it did not measure. A beta of zero is
a real, informative measurement (the strategy is uncorrelated with the market) and
must never be confusable with "we had nothing to compare against". This follows
ADR-0029's `trades_per_parameter is None` precedent exactly.

**`return_per_unit_exposure` is the comparability lens, and needs no benchmark.**
It restates the annualized return as the return earned per dollar *actually at
risk*. A 17%-invested strategy and a 90%-invested one become comparable on one
axis. It is a field of `PerformanceMetrics` (always computed) rather than of the
benchmark block, because ranking two strategies against each other needs it just
as much as comparing one against SPY. It is `None`, not `0.0`, for a book that was
never invested.

The definition uses the **annualized** return, not the total return, so the figure
is comparable across runs of different length — matching `calmar`, which already
divides the annualized return by a risk quantity. **The caveat, stated plainly:
this is a comparability lens, not a promise.** It assumes the edge would scale
linearly if the book were levered to full investment, and it would not — costs,
slippage, and the reason the strategy was flat in the first place all bind. It
says "per dollar deployed, this strategy worked harder", nothing more.

**Alpha is annualized arithmetically** (`× periods_per_year`), not geometrically
like `annualized_return`. Alpha is a mean-excess quantity; compounding a mean
arithmetic residual is not a thing anyone means by alpha, and the CAPM convention
is the multiplicative scaling. `rf = 0.0`, matching the Sharpe basis Q17 fixed.

**The comparison lives beside `benchmark_curve` in `result.json`, not inside
`metrics`.** A new top-level key `benchmark_metrics` carries it. This is
deliberate: a benchmark comparison is a *relation between two runs*, not a
property of one, and `PerformanceMetrics` describes a single run. It also keeps
`result.json`'s `metrics` key exactly `dataclasses.asdict(metrics)`, which is what
every existing consumer and test assumes.

**Everything is additive; `RESULT_SCHEMA_VERSION` stays 1.** `benchmark_metrics`
(top level) and `metrics.return_per_unit_exposure` are new keys; every
pre-existing key keeps its exact meaning and value. A v1 reader that ignores them
behaves exactly as before, and the dashboard renders no benchmark panel at all for
a document written before this ADR — it says nothing rather than inventing an
absence.

**A run with no benchmark prints a byte-identical summary.** Every new line is
gated on the benchmark, including the exposure-adjusted one. That last gating is a
judgement call: `return_per_unit_exposure` needs no benchmark and is always in
`result.json` and on the dashboard, but the *text* line reads as a comparison
("+3.35% vs benchmark +15.07%; 16.17% vs 33.51% invested") and belongs with the
thing it compares to. A golden-text regression test pins the no-benchmark summary.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Return `0.0` for beta/alpha/correlation when there is no benchmark | The whole point of the ticket. A zero beta is a real measurement; conflating it with absence is a lie the report would tell every run. |
| `float("nan")` for undefined statistics | Poisons every downstream aggregation silently (a sweep's mean Sharpe becomes NaN), and prints as `nan` — which readers interpret as a bug, not a fact. `None` forces the caller to decide. |
| Zip the two curves positionally | Fabricates a correlation whenever the curves differ in length or gaps. This is the exact failure mode the ticket warns about, and the throwaway-script version of this work. |
| Drop return steps that bridge a benchmark gap | Discards information and complicates the grid for a case that should be rare. Bridging measures the same calendar span on both sides, which is the property that matters. |
| Truncate both curves to the intersection of their first/last timestamps | Handles a late start but not an interior gap, so it is the same bug with a smaller blast radius. |
| `total_return / avg_exposure` for the exposure lens | Not comparable across runs of different length; the annualized form matches `calmar`'s existing shape. |
| Volatility-adjusted return instead of exposure-adjusted | Answers a different question (risk-adjusted, already covered by Sharpe/Sortino). The ticket's finding is about *capital deployed*, which is exposure. |
| Geometric annualization for alpha | Alpha is a mean-excess residual, not a compounding return series. Compounding it produces a number with no standard interpretation. |
| A non-zero risk-free rate | Q17 fixed `rf = 0.0` for Sharpe. A second convention here would make alpha and Sharpe silently incomparable. `rf` is a parameter on `alpha()`, defaulted to zero, so a later ADR can change the policy in one place. |
| Put `BenchmarkComparison` on `PerformanceMetrics` | It describes two runs, not one; and it would change `result.json`'s `metrics` key from "exactly asdict(metrics)" to "asdict plus something derived", breaking that contract for existing readers. |
| Bump `RESULT_SCHEMA_VERSION` to 2 | The dashboard checks exact equality, so a bump rejects every `result.json` already on disk for a purely additive change. Same reasoning as ADR-0031/0032. |
| Print the new lines even without a benchmark | Breaks byte-identity of every existing run's summary for no gain: four of the five figures are undefined without a benchmark anyway. |

## Consequences

- `--benchmark` finally pays for itself: the flag now feeds five figures, the
  `result.json`, and a dashboard panel instead of one CSV column and one line.
- **A latent bug surfaced, and its scope is measured rather than asserted.** The
  benchmark runs `buy_and_hold` under `RiskConfig.unlimited()`, so nothing clamps
  its entry. `buy_and_hold` targets `INVESTED_WEIGHT = 0.998` — 20 bps of headroom
  — and sizes the order from **bar `t`'s close**, but `SimulatedBroker` fills it at
  **bar `t+1`'s open** plus 5 bps slippage (ADR-0001, ADR-0004). When the overnight
  gap on that one entry bar exceeds roughly 25 bps, the notional overshoots the
  cash and the broker rejects for insufficient funds. Worked example, synthetic
  `SPY` seed 7 from 2018-01-01: close 3466.79 → qty 0.287874; open 3477.35 (+0.3045%)
  × 1.0005 → notional 1001.54 against 1000.00 cash →
  `insufficient cash: need 1001.54, have 1000.00`. `buy_and_hold` sets
  `self._invested = True` before returning, so it never retries: **one unlucky
  entry bar leaves the benchmark 100% in cash for the whole run**, reported as
  `Benchmark (SPY): +0.00%`.

  Scope, measured on this branch: **22 of 50 synthetic seeds** over 2018 leave the
  unconstrained benchmark entirely in cash. It is data-dependent, not universal —
  a run whose second bar happens to gap down or sideways is fine, which is why it
  went unnoticed. The default-guardrails path is unaffected: the position cap
  clamps the entry to ~25% of equity, far inside the cash. An insufficient-cash
  rejection is **not an exception**, so `_run_benchmark`'s `except EmptyUniverseError`
  cannot see it and the run reports a confident `+0.00%`.

  The new metrics make it loud — a zero-variance benchmark yields `Beta: n/a` and
  `Correlation: n/a` — where the old single line just said `+0.00%`. The fix was
  outside this slice; **it has since landed — see the KAN-672 amendment below**,
  which also retires the `xfail(strict=True)` reproduction this ADR left behind in
  `tests/unit/test_report.py::TestBenchmarkSilentlyFlat`.
- The exposure-adjusted number invites over-reading. It is a comparability lens,
  not a claim about what a levered version would earn; the docstring and this ADR
  say so, but a reader in a hurry will still take it as a return forecast.
- Alpha and beta are single-factor and unconditional: one benchmark, one slope,
  the whole sample. No regime split, no multi-factor attribution, no rolling beta.
  Those are separate slices.
- The `n/a`s are not decoration. A short run, a benchmark that did not overlap,
  and a flat benchmark all produce them, and each means something different — the
  summary distinguishes "too few shared bars" (one explanatory line) from
  "undefined statistic" (per-figure `n/a`).
- `align_curves` is `O(n)` in dict operations per call and `summarize` calls
  `compare_to_benchmark` once. `result_to_dict` derives the block a second time
  when the caller does not pass one. Negligible against a run, and the
  `benchmark_metrics` parameter lets a caller hand over an already-computed block.
- Survivorship bias (ADR-0027) and sample size (ADR-0029) still apply to every one
  of these numbers. A beta computed over a curated basket of today's winners is a
  beta of a survivorship-inflated series.

---

## Amendment (2026-08-08, KAN-672): the flat benchmark is fixed, and can no longer be silent

- Status: Accepted
- Deciders: strategy developer (project owner)

### What changed

The latent bug this ADR *recorded* is now closed. Two changes, and the second is
independent of the first on purpose.

**1. `buy_and_hold` retries the entry instead of latching it.** The defect was
never the sizing arithmetic — it was a one-shot entry that could not recover.
`self._invested = True` was set *before* the intents were returned, so a single
rejected order ended the strategy's trading life. The strategy now freezes the
universe and the weights on the first bar exactly as before, and keeps the entry
intent alive until the position actually exists, latching `_established` only
then. A leg that *is* held is never re-targeted, so this is a retry of the same
allocation, never a rebalance: buy-and-hold does not become constant-mix.

The retry has one wrinkle worth stating. Re-asserting a weight of *equity* works
while the book is flat (equity is the cash), but not once some legs have filled:
the filled legs have spent the money, so a weight of equity demands cash that is
gone, and the order is rejected on every remaining bar of the run. Measured on a
5-symbol synthetic universe over 50 seeds, that naive form produced **260
rejections** in a single run and still never established the last leg. So a
retry on a partly-established book is funded from the cash that actually remains
— the same intent ("split the money equally across the universe"), expressed
against the money that is actually there, carrying the same `INVESTED_WEIGHT`
headroom rather than a new constant. When no cash is left the quantity rounds to
zero and nothing is submitted, so an unfundable leg costs **one** rejection, not
one per bar. With that, the worst run across the same 50 seeds has 5 rejections
and every leg established.

The first bar is untouched: on a flat book the weight path is taken and the
emitted `TargetWeight`s are identical to what they always were, which is why
every existing golden and e2e expectation stayed green.

**2. A benchmark that fails to invest says so — regardless of the cause.** This
half is deliberately not conditional on the fix above. `summarize` counted only
the *strategy's* rejections, so the benchmark's were invisible; it now prints,
directly under the `Benchmark (…)` line, a caveat when the benchmark's peak
exposure is zero ("the figure above is the return on idle cash, not a market
return") or when it held nothing until later than the first fillable bar, quoting
the benchmark's first rejection verbatim in either case. `cli._run_benchmark`
additionally warns on stderr for the zero-exposure case, because that is where an
operator watching a long run looks and a summary scrolls past. Both conditions
are derived from the run — zero exposure, and an entry later than bar index 1
(an order placed on bar 0 fills at bar 1's open, ADR-0001) — not from a
threshold, so a healthy benchmark's summary is byte-identical.

### Measurement

Same 50 synthetic seeds over 2018 that measured the bug: **22 of 50 flat before,
0 of 50 after.** The worst entry delay is 7 bars of 261 and the worst run has 6
rejected attempts; the reproduction in the ticket (seed 7) goes from
`Benchmark (SPY): +0.00%` at 0.00% invested to `+4.48%` at 98.98% invested, with
the one-bar delay and its rejection printed rather than swallowed.

### Alternatives considered

| Option | Why not |
|--------|---------|
| Size the entry with more headroom (lower `INVESTED_WEIGHT`) | A magic number that covers "most" gaps is not a fix. A larger gap still fails, silently, exactly as before — and the headroom is a permanent drag on every healthy run to buy off a rare one. It also leaves the real defect (a one-shot entry) in place. |
| Run the benchmark under the default guardrails | A 25%-invested buy-and-hold is not a benchmark; it is a different, worse strategy. It would hide the bug by making the entry small enough to fund, and it would make `--benchmark` incomparable with the unconstrained buy-and-hold every reader assumes. Q24's "run it unconstrained" stands. |
| Let `SimulatedBroker` partially fill an underfunded buy | Changes the execution model for every run to fix one strategy's sizing, and quietly turns a rejection — a fact the operator should see — into a fill of a different size than was ordered. ADR-0004's conservative "reject, record, continue" is right; the strategy is what should react. |
| Have the strategy read `rejections` off the broker | There is no such seam, and adding one would let every strategy couple to broker internals. The portfolio already tells the strategy what it needs: whether the position exists. |
| Bound the retry to N attempts | Another magic number, and unnecessary — with the cash-funded retry the loop terminates by itself when the money runs out. |
| Only fix the reporting, leave `buy_and_hold` alone | Leaves the benchmark wrong 44% of the time on synthetic data and reduces the bench to apologizing for it. |
| Only fix `buy_and_hold`, skip the reporting | A future change could reintroduce a flat benchmark and it would again print `+0.00%` as a market return. The reporting guard is tested against a hand-built flat benchmark so it cannot go quiet when the strategy improves — the failure mode the original `xfail(strict=True)` was written to avoid. |
| A new top-level `result.json` key for benchmark health | `benchmark_curve` already carries the truth machine-readably and `benchmark_metrics` already returns `null` statistics for a flat benchmark. The lie was in the *rendered* line, so that is where the correction belongs. Revisit if a consumer needs to branch on it. |

### Consequences for the rest of the bench

- **Existing `buy_and_hold` baseline backtests change only where they were
  already broken.** A run whose entry cleared on the first attempt produces
  byte-identical orders, fills, and equity — the whole fast suite (846 tests,
  including the exact-equity-curve e2e golden and the raw-vs-adjusted
  backtest/paper parity test) passed unmodified. A run whose entry was rejected
  now invests a bar or a few later instead of holding cash for the whole span,
  which is a *correction*, not a regression: the old number was the return on
  cash. On the 50-seed synthetic panel this moves 22 of 50 benchmark runs.
- ADR-0039's paired bootstrap now compares against buy-and-hold rather than
  against cash on those runs, which was the reason this became urgent.
- The strategy emits a concrete `Order` (not a `TargetWeight`) on a
  partly-established retry. That is within the `Strategy` seam — both are allowed
  return types — and the order still passes through sizing, the guardrails, and
  the broker unchanged.
- `RESULT_SCHEMA_VERSION` stays **1**: nothing was added to `result.json`.
- The entry delay is now visible but unbounded. Nothing caps how late a benchmark
  may enter, and beyond a few bars the comparison is measurably unfair; the
  summary says which bar it started holding on so a reader can judge, and that is
  the whole of the guarantee.
