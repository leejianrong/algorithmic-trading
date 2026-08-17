# ADR-0067: Monte Carlo path shuffling — is the drawdown a property of the edge, or of the order?

- Status: Accepted
- Date: 2026-08-18
- Deciders: strategy developer (project owner)
- Tickets: KAN-859

## Context

`docs/algo-trading-notes.md` names Monte Carlo path shuffling as one of this
bench's core validation methods, and until this card it was not built. The idea
is simple to state and easy to get wrong: take a run's own sequence of per-bar
returns, randomly reorder them thousands of times, and see whether the
statistics that matter still look like the ones the real run reported. If they
collapse under reordering, the real run's result depended on the *sequence* its
returns happened to arrive in — sequential path luck — rather than on a
repeatable edge.

ADR-0039 already put a confidence interval around the Sharpe ratio, and it is
tempting to read this card as "more of that". It is not. The stationary block
bootstrap resamples *with replacement*: some observations are drawn more than
once, some are skipped, and the whole point is to ask how much the point
estimate would wobble if history had produced a slightly different sample of
similar returns. Shuffling is a different experiment entirely: every one of
the *exact same* observed returns is used, exactly once, every time — nothing
is added, nothing is dropped, only the order changes. It answers "did the
ORDER matter", a question the bootstrap cannot ask at all, because a
with-replacement resample does not preserve the multiset in the first place.

That distinction has a sharp mathematical consequence the card asks to be
stated plainly rather than discovered by a reader squinting at two numbers.
The annualized Sharpe ratio, as this bench defines it (Q17, `_sharpe_of`), is
the mean of the per-bar returns divided by their sample standard deviation,
scaled by `√periods_per_year`. Mean and sample variance are functions of a
*multiset* — `sum` and `len` do not know or care what order their inputs
arrived in. So the Sharpe computed on any permutation of a return series is
the same number, always, as the Sharpe computed on the original — not
approximately, but by the algebra of the formula. Reporting "a distribution of
1,000 shuffled Sharpes" would therefore be reporting the same number 1,000
times with floating-point summation-order noise in the last bit or two, dressed
up as if it were evidence. Max drawdown has no such invariance: it is the
largest peak-to-trough decline along the equity *path*, and reordering the same
losses and gains can turn a mild grind into a violent air-pocket, or the
reverse. That asymmetry — one statistic order-invariant, the other genuinely
path-dependent — is the entire reason this feature exists, and ADR-0066's own
framing sharpens why it matters *now*: that ADR already established that a
single point estimate averaged over a whole run "reads as a measurement" when
it is not one; this is the same lesson along a third axis (after "how uncertain
is it", ADR-0039, and "does it hide two very different regimes", ADR-0066) —
"was the sequence itself unusually kind or unusually cruel".

Max drawdown is also, concretely, the figure this bench's own operator
guardrails are calibrated against: `RiskConfig`'s drawdown kill switch
(ADR-0013/0031/0055) trips at a fraction of peak equity, and every `--regimes`/
`--bootstrap` number this bench prints leaves drawdown itself unexamined for
path dependence. This card closes that gap directly.

## Decision

Everything lands in `metrics.py`, at the very end of the file (after the
ADR-0066 regime-split block), following the exact conventions ADR-0039 and
ADR-0066 already established: an explicit `seed` parameter on the public entry
point, a private `random.Random` that never touches the module-global RNG, and
existing constants reused rather than duplicated where the meaning is
identical.

### 1. A permutation, never a resample — implemented as one, not approximated

`monte_carlo_shuffle(curve, periods_per_year=252.0, *, resamples=…,
confidence=…, seed=…) -> MonteCarloShuffleReport` takes the same input
`sharpe_confidence_interval` does (an equity curve; it computes
`daily_returns` internally, exactly as every other ADR-0039 entry point does)
and, for each of `resamples` iterations, draws a **uniformly random
permutation** of that exact set of per-bar returns via a new private helper:

```python
def _shuffled_copy(returns: Sequence[float], rng: random.Random) -> list[float]:
    shuffled = list(returns)
    rng.shuffle(shuffled)
    return shuffled
```

`rng.shuffle` is Fisher-Yates: every one of the `n!` orderings of the same `n`
returns is equally likely, and — the property that makes this a shuffle and
not a resample — every element of the input appears in the output exactly
once. Contrast `_stationary_indices` (ADR-0039), which draws indices *with*
replacement and can skip or repeat an observation; the two helpers sit
side by side in the module for exactly that comparison. `_shuffled_copy`'s own
test (`TestShuffledCopyIsAPermutation`) proves the multiset is preserved
(`Counter(shuffled) == Counter(original)`) across many seeds, and a companion
test with a series of 100 distinct values shows a shuffle can never introduce a
duplicate or drop a value the way a bootstrap resample legitimately can.

For every shuffled sequence, `_max_drawdown_of` (already in the module, from
ADR-0066's regime-split block) is scored, exactly as `_sharpe_of` scores each
bootstrap resample in ADR-0039.

### 2. Only max drawdown is resampled; Sharpe is reported once, by design

`MonteCarloShuffleReport.sharpe` is the observed annualized Sharpe computed
**once**, on the real, unshuffled returns — not a percentile band, not a
distribution field with a `low`/`high` shape. This is the direct consequence
of Sharpe's order-invariance argued in Context, and it was verified rather than
assumed: `TestSharpeIsInvariantUnderPermutation` reshuffles a 500-period fixture
1,000 times and scores each with `_sharpe_of`; the maximum absolute deviation
from the unshuffled Sharpe, measured, was **`0.0`** — not merely small.
IEEE-754 summation is not perfectly associative in general, so the test asserts
`math.isclose(..., rel_tol=1e-9, abs_tol=1e-9)` rather than exact equality
(exact bit-identity is not a promise this design makes, only an observation on
the fixtures tested); the practical magnitude is many orders of magnitude
below any difference that would ever be read as a meaningful change in Sharpe.
Reporting the single value, printed beside the ADR-0039 confidence interval
rather than folded into a fabricated shuffled range, is the honest rendering
of a genuinely invariant quantity — and is exactly what the card asked for:
"either literally a single repeated value with a note explaining why, or don't
present it as if shuffling produced meaningful variance."

Max drawdown is not treated this way, because it is not invariant, and that
was verified directly rather than only inferred from random shuffles:
`TestMaxDrawdownIsNotInvariant.test_clustering_the_same_losses_together_is_worse_than_spreading_them_out`
builds two hand-orderings of the identical multiset — five `-5%` losses
clustered together followed by twenty `+1%` gains, versus the same five
losses spread one per four gains — and asserts the clustered ordering's max
drawdown (`22.62%`) is worse than the spread ordering's (`9.27%`). Same
returns, same count, same mean, same variance (so the same Sharpe); a
materially different worst-case peak-to-trough decline. That pair is the
concrete demonstration the whole card exists to produce.

### 3. The distribution is reported as percentiles, and the real path is placed inside it

`shuffled_low` / `shuffled_median` / `shuffled_high` are percentiles (at
`confidence`, `DEFAULT_CONFIDENCE = 0.95`, the same two-sided convention
`SharpeInterval` uses) of the max drawdown across `resamples` random
reorderings. `actual_max_drawdown` is the run's own real, path-ordered max
drawdown — never reordered — and `actual_percentile` (via a new
`_empirical_percentile` helper, `bisect_right(sorted_values, value) /
len(sorted_values)`) is exactly where it ranks inside that shuffled
population, in `[0.0, 1.0]`. This single number is the direct answer to the
card's own framing: a real path near `1.0` was worse than nearly every random
reordering of its own returns — an unusually bad clustering of losses, either
bad luck in the ordering or a structural vulnerability the strategy has to a
particular sequence of moves. A real path near `0.0` was better than nearly
every reordering — an unusually *fortunate* sequence, and a live deployment
should not expect to be this lucky again. Both readings are stated in words in
the summary, not left for a reader to infer from two numbers
(`worse_than_shuffled` / `better_than_shuffled` properties, `None` rather than
`False` when there was nothing to compare — the same "unknown is not the same
fact as false" rule `PerformanceMetrics.underpowered` and `SharpeInterval`
already use).

There is deliberately **no `block_length` field** on `MonteCarloShuffleReport`,
unlike `SharpeInterval`/`PairedBootstrap`. A permutation has no block-size
knob — it is not parameterized by anything between "keep every return exactly
once" (the whole point) and "reorder them completely" (also the whole point);
there is nothing analogous to reduce on a short series the way the block
bootstrap's block length is capped by `effective_block_length`. Its absence is
the visible fingerprint, in the object's own shape, of the difference stated in
Context.

### 4. Too short to shuffle is computed as an honest absence, not a fabricated report

Below `MIN_BOOTSTRAP_OBSERVATIONS = 30` return periods (reused directly, not
duplicated — the same floor ADR-0039 set for "a block bootstrap needs enough
observations to mean something" and ADR-0066 already reused verbatim for
`RegimeMetrics.underpowered`; a reshuffle of a handful of returns has just as
few distinct orderings worth drawing from), every `shuffled_*`/`actual_*` field
is `None` and `notes` says why in words.

Unlike `SharpeInterval`, which is `None` in its entirety below the floor,
`monte_carlo_shuffle` **always returns a `MonteCarloShuffleReport`** — this
mirrors `RegimeReport`'s convention rather than `SharpeInterval`'s bare `None`.
The reason is the same one ADR-0066 gave: a caller (the CLI, the report
renderer) should never have to special-case "no report at all" versus "a report
whose fields are all `None`" — one shape, one place the explanation lives.

### 5. Wired into the CLI as its own opt-in flag, following `--bootstrap`'s exact shape

`backtest --monte-carlo/--no-monte-carlo` (off by default) plus
`--monte-carlo-resamples` (default `DEFAULT_BOOTSTRAP_RESAMPLES`, reused rather
than a fresh constant of the same meaning) and `--monte-carlo-seed` (default
`DEFAULT_BOOTSTRAP_SEED`, reused for the same reason — a fresh RNG stream
seeded with the same public integer is a different sequence of shuffles from
the bootstrap's stationary-index draws, since the two algorithms consume the
RNG differently; there is no correctness reason to mint a second seed constant
that means exactly the same thing "a fixed, printed, non-clock-derived
default"). `_check_monte_carlo_options` rejects a bad resample count *before*
the backtest runs, mirroring `_check_bootstrap_options` exactly — the shuffle
happens after the engine has already produced a result, so validating late
would let a typo throw away a completed multi-year run. No `--monte-carlo-
confidence` flag is exposed, matching `--bootstrap`'s own scope (it does not
expose `--bootstrap-confidence` either); the module default of `0.95` governs
both.

The report is computed **once** in `cli.py` and handed to both `summarize()`
and `write_result_json()` — the same "expensive thing computed once, shared by
both destinations" shape `--bootstrap` and `--regimes` already established.
Neither `summarize` nor `result_to_dict` ever derives it internally, so a run
that never passes `--monte-carlo` pays nothing extra.

### 6. The `result.json` key is OMITTED when absent, not always-`null`

This is the one place this card had a real design choice, and the choice is
forced by arithmetic rather than taste. `significance` (ADR-0039) is present as
`"significance": null` even on a run that never passes `--bootstrap`; `regimes`
(ADR-0066) is **omitted entirely** on a run that never passes `--regimes`.
Both conventions are documented in `report.py`, and this card had to pick one.

The always-`null` convention was only ever safe for the ADR that *first*
introduced a given top-level key: the very first `result.json` written after
ADR-0039 shipped already had `"significance": null` baked into it, so nothing
downstream ever saw a document without that key and pinning a baseline hash
afterward costs nothing. But a baseline `result.json` hash for a plain
`trading backtest` invocation is already pinned **today**, as of ADR-0066 (the
project's own regression-safety check, run before every card in this lane, is
exactly `sha256sum` of that document) — and that pinned baseline predates this
feature entirely, so it has no `"monte_carlo"` key at all, `null` or otherwise.
Emitting `"monte_carlo": null` unconditionally would add bytes to every
existing `result.json` that never asked for this feature, moving a hash that
has no reason to move. So `monte_carlo` follows `regimes`'s **omitted**
convention, not `significance`'s: `result_to_dict` builds the payload with
every existing key first and adds `"monte_carlo"` only `if monte_carlo is not
None`. Verified directly, not merely reasoned about — see "What was measured"
below.

A v1 reader already has to tolerate a missing key exactly as it tolerates a
`null` one (both mean "nothing to show here" for a document written before
this feature existed or by an invocation that did not ask for it), so nothing
downstream can tell the two conventions apart; the cost of the asymmetry is
zero and the benefit is a genuinely unmoved hash for every run that does not
opt in. `RESULT_SCHEMA_VERSION` stays **1**.

## What was measured

Verification ran `trading backtest --source synthetic --symbols AAPL,MSFT,NVDA
--strategy sma_crossover --from 2018-01-01 --to 2022-01-01 --monte-carlo` (1,045
bars, 1,044 return periods, 1,000 resamples, default seed `20260808`):

```
Monte Carlo shuffle (1000 random reorderings of 1044 return period(s), seed 20260808):
  Sharpe (order-invariant): +0.98  — mean/stdev do not depend on the order returns
  are summed in, so reordering cannot change this; it is a single value, not a
  resampled distribution (cf. the bootstrap CI above, which is about estimation
  uncertainty, not path order)
  Max drawdown — actual path:              6.73%
  Max drawdown — shuffled 95% range: [5.68%, 14.81%]  (median 8.74%)
  Actual path's drawdown sits at the 13.0 percentile of the shuffled distribution
```

The real run's own path-ordered drawdown (6.73%) sits below the median of what
a random reordering of the exact same 1,044 returns would have produced
(8.74%) and inside the 95% band — a run whose actual sequence was somewhat
kinder than typical, but not the kind of outlier `worse_than_shuffled`/
`better_than_shuffled` would flag (that needs the 97.5th/2.5th tail, and 13.0
is comfortably inside it). `result.json`'s `monte_carlo` block carries the
identical figures — `"actual_max_drawdown": 0.06731897907093842,
"actual_percentile": 0.13` — confirming the terminal and the file report the
one computation, not two.

**The invariant claim, measured rather than assumed.** Reshuffling a 500-period
and a 1,044-period fixture 1,000 times each and rescoring `_sharpe_of` on every
permutation: maximum absolute deviation from the unshuffled Sharpe, **`0.0`** in
both cases — not merely small, exactly zero on the fixtures tested. The test
suite still asserts `math.isclose(..., rel_tol=1e-9, abs_tol=1e-9)` rather than
literal equality, because IEEE-754 summation order is not guaranteed to be
perfectly associative in general and this design does not want to claim a
stronger guarantee than the algebra actually gives — but the practical gap
between "mathematically invariant" and "what this bench's summation happens to
produce" measured out to nothing.

**The variance claim, measured with a hand-built pair rather than only random
shuffles.** Five `-5%` losses clustered together ahead of twenty `+1%` gains
scores a max drawdown of **22.62%**; the identical five losses and twenty gains
interleaved one loss per four gains scores **9.27%** — same multiset, same
Sharpe by construction, a 2.4x difference in the one statistic this feature
exists to examine. A second fixture (a trending series with three sharp `-15%`
drops folded in) was reshuffled 1,000 times and its 95% shuffled range spanned
more than one percentage point of drawdown, confirming the variance is visible
at realistic scale too, not only in an adversarially hand-built pair.

**Regression safety.** The exact command this lane's baseline is pinned
against —
`trading backtest --source synthetic --symbols AAPL,MSFT,NVDA --from 2020-01-01
--to 2022-01-01 --strategy sma_crossover` — run on this branch *without*
`--monte-carlo` reproduces the pre-existing hashes byte for byte:
`equity_curve.csv` = `220e0bb8…3443e1f`, `result.json` = `01786310…5c8d699`. A
CLI test (`test_cli_monte_carlo.py::TestMonteCarloIsOffByDefault`) pins the
same property mechanically: the default invocation and an explicit
`--no-monte-carlo` invocation produce byte-identical `result.json` and stdout
(module the output path itself), and neither contains a `"monte_carlo"` key at
all.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Resample with replacement, like ADR-0039's bootstrap | That is a different experiment answering a different question ("how uncertain is the estimate"), not this card's ("did the order matter"). It also would not preserve the multiset, so a resampled max drawdown would conflate estimation noise with path-order sensitivity — exactly the confusion this ADR exists to keep apart. |
| Report a "distribution" of shuffled Sharpes | Sharpe is invariant to permutation by construction (mean/variance of a multiset); a reported spread would be reporting rounding noise as if it were a finding. Measured directly at `0.0` maximum deviation across 2,000 shuffles total. |
| A block-permutation (shuffle contiguous chunks, not individual bars) | Would answer a plausible but different question ("does drawdown depend on *medium-scale* clustering") and reintroduce a block-length knob this feature deliberately has none of. The card asks for a full random reordering of every return; a block variant is a reasonable follow-on, not a substitute. |
| `SharpeInterval`'s bare `None` below the floor | `RegimeReport`'s "always an object, fields `None`, `notes` says why" was chosen instead so callers never special-case "no report" versus "an empty one" — the same reasoning ADR-0066 already used, reused rather than re-litigated. |
| `"monte_carlo": null` always present, matching `significance` | Rejected on measurement, not preference: it would move the already-pinned `result.json` hash for every run that never asks for this feature, which the regression-safety check this lane runs before every PR treats as a hard failure. See Decision §6 and the regression-safety measurement above. |
| A fresh `DEFAULT_MONTE_CARLO_SEED`/`DEFAULT_MONTE_CARLO_RESAMPLES` constant | Would duplicate a constant that means exactly the same thing ("the fixed, printed, non-clock-derived default") the bootstrap already defines. Reused directly, the same way ADR-0066 reused `MIN_BOOTSTRAP_OBSERVATIONS` rather than inventing a fresh floor of identical meaning. |
| Fold this into `assess_significance`/`SignificanceReport` as a fourth field | `SignificanceReport` is specifically the ADR-0039 bootstrap family (resample-with-replacement); mixing in a structurally different experiment (permutation, no block length, a different null question) would blur the one distinction this ADR exists to keep sharp. A parallel, independently opt-in report and CLI flag — the same shape `regimes` already took beside `significance` — keeps the two questions visibly separate. |
| Wire `--monte-carlo` into `paper`/`sweep` too | Out of scope for this card (KAN-859 targets `backtest`); `paper` has the same open gap `--bootstrap` still has (no flag at all) and `sweep` would need its own design, since it already keeps only per-trial moments rather than full equity curves — the same reason ADR-0066 left `sweep` unwired for regimes. |

## Consequences

- The report can now say, in the same breath as the Sharpe confidence interval,
  whether the run's drawdown was a typical outcome of its own return sample or
  an outlier of it — the specific path-dependent honesty check ADR-0039's
  interval (which only ever describes the Sharpe) cannot provide.
- `PerformanceMetrics`/`compute` are completely untouched; this is purely
  additive reporting exactly like ADR-0039/ADR-0066, verified the same way
  those ADRs were — a full fast-gate run plus the pinned regression-safety
  hashes before and after.
- **Sharpe's invariance is a property of *this bench's* definition** (simple
  per-bar returns, sample mean/variance, Q17) — it would not hold for a
  path-dependent risk-adjusted ratio computed a different way (e.g., one that
  weighted recent bars, or compounded before dividing). If a future Sharpe
  variant becomes path-dependent, this ADR's "Sharpe is reported once, not
  resampled" decision would need to be revisited for that variant specifically;
  it is not a law of Sharpe ratios in general, only of the one this codebase
  computes.
- No `block_length` concept exists for a permutation, so a future reader
  looking for one (by analogy with `SharpeInterval`) should read its absence as
  intentional, not an oversight — documented explicitly on the dataclass so
  that reader does not have to rediscover it.
- Like `--bootstrap`/`--regimes`, `paper` and `sweep` still have no equivalent
  flag. Recorded as an open gap in `CLAUDE.md` alongside the two pre-existing
  ones, not solved here.
- The floating-point invariance measurement (`0.0` maximum deviation across the
  fixtures tested) is an empirical observation on this interpreter and these
  fixtures, not a guarantee `IEEE-754` summation gives in general; the test
  suite's tolerance (`rel_tol=1e-9`) is deliberately generous rather than
  asserting exact equality, so a future Python/interpreter change that moved
  the least-significant bit would not turn a correct implementation red.
