# ADR-0039: Bootstrap confidence intervals on Sharpe, and trial-count deflation

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)
- Tickets: KAN-617 (bootstrap CIs), KAN-619 (trial accounting / deflated Sharpe)

## Context

ADR-0029 taught the bench to say "this Sharpe came from eleven trades, across four
free parameters, and is therefore an anecdote". That is a *sample-size* check, and
it was the only honesty check attached to the headline number. Everything else the
report prints — Sharpe, Sortino, Calmar, alpha (ADR-0037) — is a point estimate
rendered to two decimals, which is a format that reads as a measurement.

It is not a measurement. A Sharpe ratio estimated from a finite return series is a
random variable with a wide distribution, and daily equity data is not nearly as
plentiful as the bar count suggests. An earlier study on real data put twenty-one
years of daily bars at roughly ±0.23 on the annualized Sharpe, which turns SPY's
0.42 into something like [0.09, 0.84] — a range that includes "barely better than
cash". Two decimals of a number like that is false precision, and the bench had no
way to say so.

Two problems compound it.

**Serial correlation.** The obvious fix — resample the returns and look at the
spread of the resampled Sharpes — is wrong if you resample *individual* returns. A
trend or momentum edge lives entirely in the serial structure of the return series;
shuffling single returns destroys that structure and reports an interval that is
too narrow, which is worse than no interval at all because it is confident.

**Selection.** `trading sweep` (ADR-0016) exists to search a parameter grid, and
`run_walk_forward` (ADR-0026) exists because searching inflates in-sample numbers.
But the *winner* of a search is still reported as a plain Sharpe. Run 24
combinations over one data set and the best of them scores well above zero even if
not one has any edge, purely because you kept the maximum of 24 draws. Nothing on
the screen said "this is the best of 24". Immediately relevant: the study's most
striking figure — a 93% paired win rate for `cross_sectional` — was itself selected
from 6 strategies × 2 universes × 3 halt configurations.

## Decision

Everything lands in `metrics.py`, beside the statistics it qualifies, and follows
the conventions already fixed by Q17 (simple per-bar returns, zero risk-free rate)
and ADR-0022 (`periods_per_year` as the single annualization knob).

### 1. A stationary block bootstrap, seeded explicitly

`sharpe_confidence_interval(curve, ...)` resamples the run's per-bar returns and
returns the two percentiles that bracket the requested confidence, as a
`SharpeInterval` value object.

The resampling scheme is Politis & Romano's **stationary bootstrap**: start at a
uniformly random index, walk forward, restart at a fresh uniform index with
probability `1 / block_length` at each step, wrapping around the end of the series.
Block lengths are geometric with mean `block_length`, which is what makes the
resampled series stationary — fixed-length blocks put their join points at
deterministic positions and are not. Default `block_length` is **60 bars** (a
quarter of daily data), the ticket's number.

**Determinism is part of the API, not an implementation detail.** Every entry point
takes an explicit integer `seed`, constructs its own `random.Random`, and never
touches the module-global RNG. `DEFAULT_BOOTSTRAP_SEED = 20260808` is a public
constant, the seed is carried on the returned object *and printed in the summary*,
and a test asserts `random.getstate()` is unchanged across a bootstrap. A test that
passes on a re-run with no code change is a race, not luck; this bench cannot have
one.

Measured, on the AR(1) fixture the fast layer uses (φ=0.6, 1,000 bars, seed 7,
300 resamples — `tests/unit/test_significance.py::TestBlockLength`): 60-bar blocks
give `[1.099, 4.073]`, width **2.975**; 1-bar (i.i.d.) resampling of the same
series gives `[1.605, 3.571]`, width **1.966**. Shuffling individual returns
narrows the interval by a third and reports a confidence the data does not support.
The test pins that inequality, so a future change that quietly drops block
resampling turns it red.

### 2. Short series get a smaller block, or no interval at all

Two floors, both stated rather than silently applied:

- **Below 30 return periods there is no interval.** `MIN_BOOTSTRAP_OBSERVATIONS =
  30`; under it every function returns `None` and `assess_significance` writes a
  note saying how many periods there were and what the floor is. A curve with no
  variance at all also returns `None`: a flat run has a Sharpe of 0.0 by convention
  (see `sharpe`), not a distribution, and a zero-width `[0.00, 0.00]` interval
  would read as an extraordinarily precise measurement.
- **The block length is capped so a resample still draws blocks.**
  `effective_block_length(observations, requested)` caps at
  `observations // MIN_BLOCKS_PER_RESAMPLE` with `MIN_BLOCKS_PER_RESAMPLE = 4`. A
  40-bar run that asks for 60-bar blocks gets **10**, not 60 — with blocks as long
  as the series, every resample is a near-rotation of the original, every Sharpe
  comes back nearly identical, and the interval collapses into a confident lie. The
  object carries both the requested and the effective length and a
  `block_length_was_reduced` flag, and the summary prints the reduction as a
  `note:` line. The fidelity cost is real and is stated rather than hidden.

### 3. The paired win rate resamples both series on ONE index sequence

`paired_bootstrap(curve, benchmark, ...)` answers "beats the benchmark in X% of
resamples". It is the powerful figure — for two correlated series a paired
comparison cancels the common market factor and leaves the difference in skill —
and it is the easy one to get wrong.

The design rule: **align by timestamp with ADR-0037's `aligned_returns`, draw one
index sequence per resample, and apply that same sequence to both return series.**
Resampling the two independently would compare the strategy in one imaginary market
against the benchmark in a different one, and produce a number that is confidently
wrong. There is exactly one `indices` list in the loop body, and the comment says
why.

The guard is a fixture that cannot be satisfied by accident: a strategy whose
return is the benchmark's **plus a small constant** every bar. On any shared set of
indices its mean is higher by exactly that constant and its standard deviation is
identical, so its Sharpe is strictly higher — always, by construction, not by seed
luck. The test asserts `win_rate == 1.0` exactly, and a mirror test asserts
`win_rate == 0.0` for the uniformly-worse case.

Proven by watching it fail. Changing the loop to draw a second index sequence for
the benchmark (a two-line edit, committed first and reverted after) turns the guard
red with `assert 0.585 == 1.0` — and 0.585 is exactly the figure the neighbouring
`test_resampling_the_two_independently_lands_near_a_coin_flip` computes with its own
independent reimplementation of the mistake. The mistake is not hypothetical; it is
reproduced, measured, and pinned.

"Beats" means **higher Sharpe on that resample**, not higher total return. This
whole slice is about the Sharpe, and a return-based win rate is dominated by drift
rather than by risk-adjusted skill.

### 4. Trials are counted, and the winner is deflated

`SweepSummary.trial_count` is the number of completed runs — one per
`(combination, window)` pair, because that is the granularity `ranked()` sorts and
therefore the granularity at which a winner is *selected*. Combinations the strategy
constructor rejected never ran and never had a chance to win, so `skipped` does not
count; nor does a window dropped for having no data.

`SweepSummary.deflated_winner()` scores the top-ranked run against the Sharpe the
luckiest of those trials would have shown with no edge at all:

- `expected_max_sharpe(N, sigma)` — Bailey & López de Prado's expected maximum,
  `sigma * [(1 - g)·Φ⁻¹(1 - 1/N) + g·Φ⁻¹(1 - 1/(N·e))]`, where `sigma` is the
  spread of *per-bar* Sharpes across the trials and `g` is Euler-Mascheroni. The
  spread matters as much as the count: 24 near-identical combinations offer far
  less room to get lucky than 24 genuinely different ones.
- `probabilistic_sharpe_ratio(moments, threshold)` — the probability the true Sharpe
  exceeds that threshold, corrected for the return series' skew and kurtosis,
  because a Sharpe estimated from negatively-skewed fat-tailed returns deserves less
  confidence than the same Sharpe from clean ones.

`SweepRun` gained a `moments: ReturnMoments | None` field — five floats per run, not
the whole return series — which is all the deflation needs, so no run has to be
repeated and no curve has to be retained.

**A lone `trading backtest` is one trial, not "no correction needed".** With one
trial the null threshold is 0.0 and the figure degenerates to the probabilistic
Sharpe against zero, which is still a real check. What the tool **cannot** see is
every run the operator made in an earlier invocation, on another date range, or with
another strategy. `assess_significance` therefore always emits a note saying the
count covers only this invocation and the correction is a **LOWER BOUND**, never a
complete accounting. That sentence is not optional and not conditional.

### 5. Nothing is printed unless asked for, and nothing is derived silently

`summarize(..., significance=None)` and `result_to_dict(..., significance=None)`
take an **already-computed** `SignificanceReport`. Omit it and both are
byte-identical to today — pinned by a literal golden string, the same technique
ADR-0037 used. Unlike `benchmark_metrics`, the block is **never derived inside
`result_to_dict`**: a bootstrap costs thousands of Sharpe computations, and writing
a `result.json` must not silently pay for one nobody asked for.

Measured cost, on a 21-year daily run (5,478 return periods, `blue20`, synthetic
seed 5): `assess_significance` with the 1,000-resample defaults for both the
interval and the paired figure takes **2.74 s**, and a second call returns an equal
object. That is fine for a deliberate check and unacceptable as a default on every
run, which is why the default is off and `resamples` is a parameter. The fast test
layer uses 50–400 resamples and the whole suite still runs in ~8 s.

`result.json` gains **one additive top-level key**, `significance`, so
`RESULT_SCHEMA_VERSION` stays **1** and `metrics` stays exactly
`dataclasses.asdict(metrics)` — the contract every existing consumer assumes. The
static dashboard renders a matching panel with the three-state absence handling
ADR-0037 established: key missing (a pre-ADR-0039 document) renders nothing, key
present and `null` says plainly that no bootstrap ran, and a present block renders
the figures.

### 6. It reports what it cannot conclude

In ADR-0029's spirit, the wording carries the finding:

- an interval that contains zero prints `⚠ the interval straddles zero — this
  sample cannot distinguish the strategy from having no edge at all; the point
  estimate is not a finding`;
- a deflated probability under `DEFLATED_SHARPE_CONFIDENCE = 0.95` prints `⚠ below
  0.95 — after discounting for N trial(s), this Sharpe is not distinguishable from
  the best of that many skill-free runs`;
- anything that could not be computed produces a `note:` line explaining why, not a
  missing row. "We did not measure this" and "we measured it and it was zero"
  remain the two things this bench refuses to conflate.

## What was measured

Offline, synthetic, reproducible. Every figure below comes from a command that was
actually run on this branch.

**A 21-year Sharpe is pinned to about ±0.4, not to two decimals.**
`cross_sectional` over `@blue20`, 2000-01-01 to 2020-12-31, `SyntheticAdapter(seed=5)`,
default guardrails: Sharpe **1.1101**, 95% CI **[+0.712, +1.517]** — width **0.805**
over 5,478 return periods. The paired figure against an `equal_weight` run on the
same universe and dates: **0.0%** of 1,000 paired resamples, observed edge
**-1.037** (`equal_weight` scored 2.1467). Reproduce with:

```bash
uv run python -c "
from datetime import UTC, datetime
from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.engine import Engine
from trading.metrics import assess_significance
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Portfolio
from trading.universe import get_universe
S, E = datetime(2000,1,1,tzinfo=UTC), datetime(2020,12,31,tzinfo=UTC)
def run(n):
    return Engine(SyntheticAdapter(seed=5), SimulatedBroker(Portfolio(cash=1000.0)), Guardrails(RiskConfig())).run(get_strategy(n), get_universe('blue20'), S, E)
a, b = run('cross_sectional'), run('equal_weight')
r = assess_significance(a.equity_curve, b.equity_curve)
print(r.sharpe_interval)
print(r.paired)
"
```

**A 24-combination sweep raises the bar, but only as far as the spread allows.**
`sma_crossover` over `fast ∈ {5,10,15,20}` × `slow ∈ {30,50,80,120,200,250}`, same
universe/dates/adapter: 24 trials, winner `{fast: 10, slow: 80}` at Sharpe
**1.4987**; the 24 trial Sharpes have a standard deviation of only **0.0915**, so
the expected best-of-24 under the null is **0.181** and the deflated probability
stays essentially 1.0. That is the honest outcome for this grid, and it makes the
mechanism visible: the deflation bites through the *spread* of the candidates, not
merely their count. A grid of genuinely different strategies would bite much
harder. Reproduce with:

```bash
uv run python -c "
from datetime import UTC, datetime
from trading.data.synthetic import SyntheticAdapter
from trading.sweep import run_sweep
from trading.universe import get_universe
s = run_sweep('sma_crossover', {'fast':[5,10,15,20],'slow':[30,50,80,120,200,250]},
              SyntheticAdapter(seed=5), get_universe('blue20'),
              datetime(2000,1,1,tzinfo=UTC), datetime(2020,12,31,tzinfo=UTC))
print(s.trial_count, s.ranked()[0].params, round(s.ranked()[0].metrics.sharpe, 4))
print(s.deflated_winner())
"
```

The ticket's real-data figures (±0.23, SPY 0.42 → [0.09, 0.84], win rates of
93%/81%/50%) are **motivation, not results**. They came from an earlier study on
real prices; nothing here reproduces them and nothing here claims to. The synthetic
numbers above are what this branch measured.

## Alternatives considered

| Option | Why not |
|--------|---------|
| I.i.d. bootstrap over individual returns | Destroys the serial correlation a momentum edge consists of. Measured on the AR(1) fixture: it narrows the interval from 2.975 to 1.966 — a *more confident* answer from *less* information. |
| Fixed-length (moving-block) bootstrap | Simpler, but the resampled series is not stationary — the join points sit at deterministic positions. Politis & Romano's geometric lengths cost one extra `rng.random()` per bar and remove the artifact. |
| Analytic (Lo 2002) Sharpe standard error | Closed-form and fast, but it needs an assumed autocorrelation structure. The bootstrap makes no such assumption, and this bench cares more about being wrong-proof than about being fast here. |
| BCa (bias-corrected accelerated) intervals | Better small-sample coverage, but it needs a jackknife pass and an acceleration constant, roughly doubling the cost and the surface area. The percentile interval is the standard simple choice and honest enough next to survivorship bias (ADR-0027), which dwarfs the difference. |
| Draw the seed from the clock, or leave it unseeded | Two runs of the same command would print different intervals and no test could assert anything. The seed is an explicit parameter with a public default, printed in the output. |
| Silently reduce the block length on a short series | The reduction changes what the interval means. It is recorded on the object, exposed as `block_length_was_reduced`, and printed as a note. |
| Produce an interval anyway below 30 observations | A garbage interval is worse than no interval, because it renders identically to a real one. `None` plus a note in words. |
| Resample strategy and benchmark independently | The exact failure the ticket warns about, reproduced and measured here: the guard fixture drops from 1.0 to 0.585. It throws away the correlation that makes the paired test powerful. |
| "Beats benchmark" by total return rather than Sharpe | A different, lower-power question, dominated by drift. This slice is about the Sharpe. |
| Treat a single `backtest` as "no correction needed" | One trial is still a trial. The threshold is 0.0 and the check degenerates to the probabilistic Sharpe against zero, which is a real check, not a skipped one. |
| Persist a cross-invocation trial ledger | The honest fix for the operator's invisible trials — and a much bigger slice (where does it live, when does it reset, what counts as the same search). Recorded as an open limitation instead of half-built. |
| Put the new fields on `PerformanceMetrics` | `result.json`'s `metrics` key is contractually `dataclasses.asdict(metrics)`, and a significance block is a statement *about* the metrics, not another metric. Same reasoning ADR-0037 used for `benchmark_metrics`. |
| Compute the bootstrap inside `summarize` / `result_to_dict` by default | 2.74 s on a 21-year run, and it would change the bytes of every existing summary. Caller-supplied, defaulted off. |
| Bump `RESULT_SCHEMA_VERSION` to 2 | The dashboard checks exact equality, so a bump rejects every `result.json` already on disk for a purely additive key. Same reasoning as ADR-0031/0032/0037. |

## Consequences

- The report can say "this Sharpe is 1.11, and the data supports anywhere from 0.71
  to 1.52" — and, when the interval crosses zero, that the run measured nothing.
- The sweep's winner can no longer be quoted without the number of combinations it
  beat. `trial_count` and `deflated_winner()` are on the summary object.
- **Neither figure is reachable from the CLI yet.** `metrics.assess_significance`,
  `report.summarize(significance=...)`, `report.summarize_significance`,
  `write_result_json(significance=...)`, and `SweepSummary.deflated_winner()` are
  all wired and tested, but `cli.py` was owned by a concurrent lane during this
  slice and passes none of them. A `--bootstrap` flag on `backtest` (and printing
  the deflation under the sweep table) is a small follow-up, listed in the PR.
- **The trial count is a lower bound, and always will be** without a persistent
  ledger. The tool sees one invocation. An operator who ran six strategies across
  two universes and three halt configurations by hand has made 36 trials and the
  tool will report 1. The output says so every time; it cannot do better alone.
- The bootstrap is `O(resamples × observations)` in pure Python: ~2.7 s for
  1,000 resamples over 5,478 periods, both figures. Fine on demand, not fine by
  default. `resamples` is a parameter on every entry point.
- The percentile interval inherits every bias already in the underlying returns.
  Survivorship bias (ADR-0027) still inflates a curated-basket run; a confidence
  interval around an inflated estimate is a precise statement about a biased
  number. It says how much the *sample* wobbles, not whether the sample was drawn
  from the right population.
- `expected_max_sharpe` assumes the trials are independent draws. Grid points that
  differ by one parameter are strongly correlated, so the true effective number of
  independent trials is below the count, and the deflation is conservative in the
  *unhelpful* direction — it over-corrects for a dense grid. Recorded, not fixed;
  the honest alternative needs an effective-sample-size estimate of its own.
- The trades-per-parameter warning (ADR-0029) and this deflation answer different
  questions — "was there enough data" versus "how many times did you look" — and a
  run can fail either independently. Both print.
- `sharpe()` was refactored to delegate to a shared `_sharpe_of(returns, ...)` so
  the bootstrap scores resamples with the identical definition. The arithmetic and
  its evaluation order are unchanged, and the whole existing suite is green.
