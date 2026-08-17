# ADR-0066: Regime-split metrics — the same PerformanceMetrics, split by the run's own bars

- Status: Accepted
- Date: 2026-08-18
- Deciders: strategy developer (project owner)
- Tickets: KAN-621

## Context

Every headline figure this bench prints — Sharpe, Sortino, Calmar, annualized
return — is a single number averaged over the whole requested range. A 21-year
daily backtest folds the dot-com bust, the GFC, and the 2009-2020 bull run into
one Sharpe, and a strategy that only works in a low-volatility grind looks
identical, on that one number, to a strategy that is genuinely regime-robust.
ADR-0039 already taught the bench that a point estimate "reads as a measurement"
when it is not one; this is the same lesson applied along a different axis — not
"how confident are we in this number" but "does this number hide the fact that it
is really two very different numbers averaged together."

Two existing conventions bear directly on how this has to ship. ADR-0029/ADR-0039
established that a sample too small to mean anything is **computed and flagged**,
never hidden or silently suppressed — `MIN_TRADES_PER_PARAMETER` and
`MIN_BOOTSTRAP_OBSERVATIONS` are the precedents. And ADR-0053/0054/0059 are the
running argument that an unstated classifier rule or annualization basis is
exactly the kind of silent assumption this codebase's culture exists to catch —
so the regime rule here has to be one explicit, documented function of the run's
own bars, not a description in prose that the code does not quite match.

## Decision

**1. Two independent trailing-statistic axes, each split at the run's own
median.** Over the run's own per-bar returns (`daily_returns(equity_curve)`,
never the benchmark's — see Alternatives), a trailing window of
`REGIME_WINDOW = 20` bars (about a trading month at daily bars; a bar count, not
a calendar duration, so the same 20-bar lookback applies unchanged at any
interval) computes two statistics at every bar once the window is full:

- **Volatility** — the sample standard deviation of the trailing window,
  annualized by `sqrt(periods_per_year)` (exactly `sharpe`'s own annualization,
  applied to a rolling window instead of the whole series). A bar is labeled
  `"high_vol"` when that trailing figure is at or above the run's own **median**
  trailing volatility across every classified bar, `"low_vol"` otherwise.
- **Trend** — the Kaufman-style efficiency ratio over the same window,
  `abs(sum(window returns)) / sum(abs(window returns))`, in `[0, 1]`. Near 1 means
  the window's moves mostly ran one direction (net displacement close to the sum
  of the moves); near 0 means they largely cancelled. A bar is labeled
  `"trending"` at or above the run's own median trailing efficiency ratio,
  `"mean_reverting"` otherwise.

The first `REGIME_WINDOW - 1` return periods have no full trailing window and are
**warmup**: unclassified on both axes, excluded from every regime's bar count and
every regime's fills (not folded into a `None`-labeled bucket, and not padded with
a fabricated estimate — the same "not enough data to say anything" silence
`sharpe_confidence_interval` already keeps under `MIN_BOOTSTRAP_OBSERVATIONS`).

Splitting at the run's own median rather than a fixed absolute cutoff (e.g. "20%
annualized volatility") is deliberate: there is no threshold that means the same
thing across a five-minute crypto run and a 21-year daily equity one (ADR-0054's
whole argument against a hard-coded constant, one level up), so every run supplies
its own scale. `RegimeReport.vol_threshold` / `.trend_threshold` record exactly
what that scale was, so a reader never has to trust an unstated number.

**2. Two axes reported separately, never crossed.** `RegimeReport` carries four
slots — `high_vol`, `low_vol`, `trending`, `mean_reverting` — not a four-way cross
of volatility × trend. Crossing them would quarter an already-thin bar count a
second time, defeating the point of a feature built specifically to surface small
samples honestly (see "What was measured" below: even a two-way split already
approaches `MIN_BOOTSTRAP_OBSERVATIONS` on a modest range).

**3. The same `PerformanceMetrics`, restricted to a regime's own bars — not a new
metric shape.** `compute_regime_report` builds a `RegimeMetrics` per label whose
`.metrics` field is a real `PerformanceMetrics`: total/annualized return, Sharpe,
Sortino, Calmar, max drawdown, win rate, turnover, avg/peak exposure, trade count,
trades-per-parameter, and return-per-unit-exposure, exactly the fields `compute`
already assembles. Because a regime slice is a **discontiguous** subsequence of
bars (e.g. "every bar this run spent in a high-vol regime"), there is no real
`EquityPoint` curve to hand the existing curve-shaped functions, so private
per-return-sequence twins of them do the arithmetic instead
(`_total_return_of`/`_annualized_return_of`/`_max_drawdown_of`/`_sortino_of`/
`_calmar_of`, plus the already-existing `_sharpe_of`): compound the regime's own
returns back-to-back, starting from the run's real starting equity, and walk the
resulting synthetic path exactly as the whole-run functions do. This is the only
sense in which "the regime's total return" is well-defined: what the strategy
would have earned had it experienced only those bars, in that order.

Trade-based figures (win rate, turnover, entry count) need the fill blotter, and
here correctness demands **not** simply filtering the fill list to one regime's
timestamps: `_regime_trade_stats` walks *every* fill in submission order to keep
the running per-symbol quantity and average cost correct, and only *tallies* a
fill into the regime's counters when the fill's own timestamp falls in that
regime. A SELL that closes a position opened in a different regime (or during
warmup) is still priced against its true average cost; a regime slice does not get
to pretend the position history outside it never happened.

**4. Small-sample regimes are computed, never hidden — and the floor is
borrowed, not invented.** `RegimeMetrics.underpowered` reuses
`MIN_BOOTSTRAP_OBSERVATIONS` (already 30, ADR-0039's own floor for "a return
series too short for its Sharpe to mean anything") rather than a fresh constant —
a regime Sharpe computed from 8 return periods is exactly as unreliable as a
whole-run Sharpe from 8 would be, and reusing the number keeps one definition of
"too thin to trust" in the module instead of two that could drift apart. A thin
regime's `PerformanceMetrics` is still fully computed and printed (the reader
decides, per ADR-0029's rule), with an explicit warning line and a note naming the
floor it missed.

**5. Additive, opt-in, computed once and shared — ADR-0039's exact shape.**
`compute_regime_report` is a new function; no existing function's signature or
behavior changes (`compute` is untouched, pinned by a test that calls it before
and after `compute_regime_report` runs and asserts equality). `summarize`,
`result_to_dict`, and `write_result_json` each gain one additive keyword,
`regimes: RegimeReport | None = None`, rendered/serialized only when supplied. The
CLI gains `backtest --regimes/--no-regimes`, off by default; when passed, the
report is computed exactly once and handed to both the terminal summary and
`result.json`, mirroring how `--bootstrap` already avoids computing the same
expensive thing twice.

**6. One deliberate asymmetry: the `result.json` key is *omitted*, not
`null`, when absent.** Every other additive block this bench has shipped
(`significance`, `benchmark_metrics`) is present as a key with a `null` value
when the caller did not ask for it. `regimes` breaks that pattern on purpose: a
plain `trading backtest` run with no flags at all is pinned, by the repo's own
regression convention, to write byte-identical output forever — and an
unconditional `"regimes": null` key would still add bytes to every `result.json`
that had never seen this feature, moving a hash that has no reason to move for a
run that never touched the flag. So `result_to_dict` builds the dict with every
existing key first and adds `"regimes"` only `if regimes is not None`. A v1 reader
already has to tolerate a missing key exactly as it tolerates a `null` one (both
mean "nothing to show here"), so nothing downstream can tell the two conventions
apart — the asymmetry costs a reader nothing and buys back full byte-identity for
every run that does not opt in. `RESULT_SCHEMA_VERSION` stays **1**.

## What was measured

Verification ran `trading backtest --source synthetic --symbols AAPL,MSFT,NVDA
--strategy sma_crossover --regimes` over 2015-01-01..2022-01-01 (1,827 bars, 1,826
return periods):

| regime | bars | total return | annualized | Sharpe | Sortino | Calmar | max DD |
|---|---|---|---|---|---|---|---|
| whole run | 1,826 | +21.27% | +2.70% | 0.43 | 0.62 | 0.15 | 17.80% |
| high_vol | 904 | +2.65% | +0.73% | 0.13 | 0.19 | 0.05 | 15.83% |
| low_vol | 903 | +18.14% | +4.76% | 0.84 | 1.25 | 0.66 | 7.22% |
| trending | 904 | +36.39% | +9.04% | 1.34 | 2.07 | 0.91 | 9.91% |
| mean_reverting | 903 | -11.09% | -3.23% | -0.44 | -0.61 | -0.23 | 14.04% |

`high_vol.bar_count + low_vol.bar_count == trending.bar_count +
mean_reverting.bar_count == 1,826 - (REGIME_WINDOW - 1) == 1,807`, confirming
warmup exclusion and full partitioning on each axis independently. The whole-run
Sharpe of 0.43 is bracketed by 0.13 and 0.84 on the volatility axis and by -0.44
and 1.34 on the trend axis — a single number that would have obscured a strategy
whose edge, on this run, lives almost entirely in trending, low-volatility
stretches.

**Regression safety.** The same command *without* `--regimes` was run against
this branch and its `equity_curve.csv`/`result.json` hashes were confirmed to
match the pre-existing baseline (`220e0bb8…3443e1f` /
`01786310…5c8d699`) byte for byte — the omitted-key decision (§6) was tested by
first shipping the naive always-`null` version, discovering the mismatch against
that exact baseline, and correcting it before this ADR was written.

**Continuity across a regime boundary** was checked directly: a BUY during
warmup followed by a SELL during a classified regime is judged against the BUY's
true average cost (not a truncated-history cost of zero), and total entries
attributed across all four regime slots never exceeds the real number of
position-opening fills in the whole blotter (`test_regime_metrics.py`,
`TestTradeAttributionRespectsFullHistory`).

## Alternatives considered

**A four-way cross (high-vol-trending / high-vol-mean-reverting / …).** Rejected:
it would quarter the bar count a second time, and the measured run above already
sits close to `MIN_BOOTSTRAP_OBSERVATIONS` on some slices at two-way splits.
Reporting two independent two-way splits gives twice the sample per bucket for the
same total data, at the cost of not directly answering "how did the strategy do
specifically in trending *and* high-vol bars" — a reader who wants that
combination can derive it from the per-bar labels, which are cheap to reconstruct
from the same rolling statistics, but this ADR does not ship it as a headline
figure.

**A fixed, absolute volatility threshold** (e.g. "high-vol above 20% annualized").
Rejected for the same reason ADR-0054 refused a hard-coded annualization basis: no
single number means the same thing across every market and interval this bench
supports, and a run's own median is scale-free by construction.

**Classifying off the benchmark's returns instead of the strategy's own.** The
card explicitly left this open. Deferred: a benchmark is optional
(`--benchmark`), and the regime split should be available on every run whether or
not one was supplied; classifying off the strategy's own realized volatility and
trend is also the more directly interpretable question ("how did this strategy do
when *its own* book was choppy"), whereas benchmark-relative regime classification
answers a related but different question (market regime vs. strategy-book
regime) that is a natural, separate extension.

**A statistical regime model (HMM, Bayesian changepoint detection, ...).**
Deliberately out of scope. The whole point of this feature is that its rule is
auditable in one paragraph and reproducible by hand; a latent-state model would
need its own significance machinery (how many states, how much history to fit)
and would reopen exactly the "unstated classifier" problem this ADR exists to
avoid, on a much larger scale.

**Emitting `"regimes": null` unconditionally, matching `significance`'s
precedent.** Rejected on measurement, not preference: it moves the `result.json`
hash of every run that never passes `--regimes`, which the regression-safety
check this ticket was built against treats as a hard failure. See Decision §6.

## Consequences

**What this buys.** A reader can now see "Sharpe 1.34 in trending bars, -0.44 in
mean-reverting ones" beside the existing whole-run number, without waiting on a
second command or a second run — the classifier and the metrics both come from
data the engine already produced. Small samples are computed and flagged rather
than either hidden or presented with false confidence, consistent with every
other significance check in the module.

**What this does not answer.** This is a **descriptive** segmentation of one
run's own bars, not an out-of-sample regime test. A strategy whose parameters
were implicitly fit to perform well in, say, trending markets will still show a
high trending Sharpe here — the split does not distinguish "genuinely
regime-robust" from "happened to be tuned for the regime that dominated this
range," which is the same overfitting risk ADR-0029/ADR-0039 already warn about
for the whole-run number. Reading a strong regime slice as validation without
walk-forward (ADR-0026) or deflation (ADR-0039) context would be exactly the
mistake those ADRs exist to prevent.

**Where it is wired, and where it is not.** `backtest --regimes` only; `paper`
and `sweep` have no equivalent flag. `sweep` in particular would need its own
design (a regime split *per trial*, or one computed on the winner only) rather
than a mechanical copy of this wiring, since a sweep already keeps only
`ReturnMoments` per trial, not full equity curves. The dashboard does not render
`result.json`'s `regimes` block; the additive shape (mirroring
`divergence_rows`'s precedent) is intended to make that a follow-on rather than a
redesign.

**The one intentional schema asymmetry.** `result.json`'s `regimes` key is
omitted, not `null`, when the report was not requested — every other additive
block in this schema uses the always-present-`null` convention. A machine reader
that assumes every documented key is always present (even if `null`) would need
to special-case this one; the docstring on `result_to_dict` and the schema
comment both say so at the point a future reader would look.

**`REGIME_WINDOW = 20` is a bar count, fixed regardless of interval or market.**
On a short paper session or a single walk-forward fold, 20 bars may consume most
or all of the available history, leaving little or nothing to classify — exactly
the case `underpowered` and the "too short to classify" note exist to name
honestly, not a silent failure, but a real limit on when this feature has
anything useful to say.
