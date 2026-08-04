# ADR-0030: Synthetic bars are range-independent — one canonical series per symbol

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

`SyntheticAdapter` (ADR-0012) seeded `random.Random(_symbol_seed(symbol, seed))`
**per call** and then walked the price path forward from whatever `start` the caller
asked for. Each bar's values therefore depended on its position *within the request*,
not on its position in time. The generator was reproducible — the same
`(symbol, seed, range)` always gave the same bars, which is what ADR-0012 promised
and what the tests checked — and still wrong, because the promise was too weak:

```
get_bars("AAA", 2018-01-01, 2019-10-02) first closes: [78.7237, 78.0984, 77.5822]
get_bars("AAA", 2019-10-03, 2021-07-02) first closes: [78.7237, 78.0984, 77.5822]
   -> two DIFFERENT date ranges, byte-identical price paths
sub-range 2019-10-03..2021-07-02 vs parent 2018-01-01..2021-07-02
   -> agreed on 0 of 457 shared timestamps
```

Three consequences, each measured before the fix:

- **Walk-forward degradation was a null test on synthetic data.** `run_sweep` with
  `windows=3` returned *identical* metrics for windows 0 and 1 (total return
  `+0.010534`, Sharpe `+0.391062`); window 2 differed only because it happened to
  contain one extra bar. Any IS→OOS comparison built on synthetic spans (ADR-0026)
  was comparing a span against a copy of itself.
- **Overlapping fetches contradicted each other.** Any caller holding two windows of
  the same symbol held two different prices for the same bar.
- **Synthetic paper-live priced an absurd world.** `RecentWindowFeed` polls
  `get_bars(symbol, datetime.min, now)` by design (ADR-0014/0021), so the walk began
  in year 1: ~528,000 weekday bars per poll (~8.5 s per symbol per poll), compounding
  ~2,000 years of drift into a close of `1.55e+81` for a 2022 bar that a bounded
  backtest fetch priced at `42.17`. The `--once` offline replay was unaffected (the
  CLI pre-fetches into a `FakeAdapter`), so only `paper --live --source synthetic`
  reached it.

Reproducibility was never the invariant that mattered. For a *data adapter* the
invariant is that a bar belongs to a symbol and a timestamp: two requests that
overlap must agree on the overlap, because a data source that answers differently
depending on how you asked is not a data source, it is a function of the caller.
Real providers have this property for free; a generator has to be built with it.

## Decision

**One canonical series per `(symbol, seed, params, frequency)`, anchored at a fixed
epoch.** `EPOCH = 1990-01-01` (a Monday, so the epoch is itself bar 0) is the start of
the series; `get_bars` returns a *slice* of it. Range independence then holds by
construction rather than by care.

**A bar is a pure function of its absolute position, drawn from a counter-based
RNG.** `_session_index(day)` computes a bar's position from the epoch in closed form
(weekday arithmetic, no walking). Each draw is a `blake2b` digest of
`"{symbol}:{seed}:{stream}:{index}"` turned into uniforms and then into standard
normals by Box-Muller. `blake2b` because it is fixed by its specification — identical
bytes on every platform, Python version, and process — precisely where the builtin
`hash()` is salted per process. Nothing consults the wall clock, a global RNG, or the
order in which calls arrive.

**The price level still compounds, so the walk from the epoch stays and is named.**
An independent per-bar draw would destroy the walk, so the *log return* is the
positional draw and the level is `base * exp(cumulative sum of returns from the epoch
to that bar)`. That makes a bar's level `O(bars from the epoch)` to compute — about
250 sessions per calendar year, so ~8,300 steps for a 2022 date, ~20 ms per symbol,
memoized per symbol on the adapter instance so later requests only extend it. This is
the cost of the decision and it is not hidden: it is stated in the module docstring
and in `_cumulative_through`, the one function that walks.

**Bars before the epoch do not exist; earlier requests are clipped, not re-anchored.**
`RecentWindowFeed` polls from `datetime.min` as a supported design, so raising would
break a working path. Clipping keeps that poll correct *and* cheap (117 ms instead of
8.5 s), and it is the one place where "less data than you asked for" is the honest
answer.

**Intraday is a Brownian bridge inside each session, onto the daily close.** The daily
series is the backbone at every cadence: one session's slot increments are positional
draws re-centred so they sum to exactly that session's daily log return — the true
conditional law of a Gaussian walk given its endpoint. Two things follow. The last
intraday bar of a session closes *exactly* on the daily bar's close, so 1h/30m/5m/1m
and 1d are consistent views of one series instead of unrelated walks. And an intraday
request costs `O(days from the epoch)`, not `O(minutes from the epoch)`: a continuous
minute-level walk from 1990 would be ~3.1 M steps (~4-8 s per call), which would have
made the fast gate slow enough to be bypassed, and a gate people bypass protects
nothing.

**This narrows ADR-0022's wording, not its invariant.** ADR-0022's decision is that
frequency is an adapter-construction property and that neither the `DataAdapter`
protocol nor the engine's per-bar step learns the interval; that holds unchanged. What
changes is only how the *generator* scales its own draws internally: it no longer
scales `mu`/`sigma` by `Frequency.periods_per_year`, because the daily backbone now
sets the annualized shape and the bridge distributes it across the session.
`Frequency.periods_per_year` remains what it always was for metrics annualization.

**The hand-rolled normal is guarded by two cheap tests.** A per-bar `random.Random`
costs ~16 µs against ~2 µs for the counter-based draw — at thousands of walk steps per
symbol that is the difference between a usable and an unusable fast gate — but a
hand-rolled Gaussian is exactly where a silent statistical bug could hide for months.
So: exact values for two bars are pinned (byte stability across platforms and Python
versions, re-proved on every run since pytest runs with a random `PYTHONHASHSEED`), and
over 5,000 draws the standardized log returns are asserted to have mean ≈ 0, stdev ≈ 1,
both signs, and real tails. A transposed Box-Muller term or a missing `sqrt` would sail
past every range-consistency test and fails these.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Leave it; document that a synthetic range is only self-consistent | The bug silently turned a validation feature (walk-forward degradation, ADR-0026) into a null test. A test bench built to favour honest numbers cannot ship a data source whose numbers depend on the question. |
| Keep the sequential per-call RNG but always start the walk at the epoch | Achieves the same invariant with a smaller diff, and was tempting. Rejected because a bar's values then depend on how many draws happened to precede it — an implementation detail, not a position — so any future change to the number of draws per bar silently re-prices history, and random access is impossible. |
| Per-bar seed via `random.Random(hash(...))` | `hash()` is salted per process, so the series would change between runs. `hashlib` is the only honest choice, and the existing `_symbol_seed` already used it. |
| Per-bar `random.Random(blake2b(...))` instead of hand-rolled Box-Muller | Measured 16 µs/bar against 2 µs. At ~8,300 epoch-walk steps per symbol that is ~130 ms per symbol per call, which multiplied across the fast suite (and 20-symbol baskets) is a gate people learn to skip. Guarded with a distribution test instead. |
| Vectorize the walk with NumPy (already a core dependency) | 100x faster, but NumPy's `Generator` gives no cross-version stream guarantee (NEP 19), so a dependency bump could silently change every synthetic number. `blake2b` is specified; that matters more here than speed. |
| Hierarchical / Brownian-bridge construction from the epoch, `O(log i)` per bar | Removes the walk entirely and is the mathematically elegant answer, but it is subtle code for a generator whose whole job is to be trustworthy. The session-level bridge captures the useful half of the idea (bounded intraday cost) at a fraction of the complexity. |
| A later epoch (2010, 2015) to shrink the walk | Milliseconds saved in exchange for silently clipping any earlier backtest. 1990 costs ~20 ms and covers every plausible scenario. |
| Continuous intraday walk from the epoch at the bar cadence | Literally matches ADR-0022's wording, but ~3.1 M steps for a 1-minute request in 2022 (~4-8 s per call), and it gives no cross-frequency consistency: the intraday and daily series would disagree at every session close. |

## Consequences

- **Buys:** overlapping requests agree, so a sub-range is a true slice of its parent;
  `run_sweep(windows=3)` now scores three genuinely different spans (three distinct
  results where two used to be identical); a walk-forward on synthetic data is at last
  a real comparison rather than a span against its own copy; synthetic paper-live
  prices the same world a backtest does, 25x faster; and 1h/30m/5m/1m agree with 1d at
  every session close — a cross-frequency consistency the old generator never had.
- **Costs:** every synthetic number moved (no exact-value assertion existed in the
  suite, so nothing had to be re-baselined, but any saved output from before this
  change is stale). Computing a bar's level walks from the epoch — `O(bars from the
  epoch)`, ~20 ms per symbol for a 2020s date, memoized per instance. The adapter is
  now stateful (a pure-value memo) and so not for sharing across threads. Bars before
  1990 do not exist. A price level that has compounded drift since 1990 sits well
  above `base_price` in the 2020s (a 100.0 base trades in the hundreds); fractional
  shares (ADR-0011) make that irrelevant to sizing, and `SyntheticParams.base_price`
  now says so. Intraday variance is *conditional on the day*: a session's total move
  is drawn first and the path is generated given it, so intra-session statistics are
  not those of an unconditional walk — the marginal daily behaviour is unchanged.
- **Amends ADR-0012:** its "same seed + symbol + range → byte-identical bars" is
  superseded by the stronger "same seed + symbol + *timestamp* → identical bar,
  whatever range you ask for". Its GBM-toy caveat stands unchanged and is the more
  important half. Narrows ADR-0022's wording on how the generator scales its own
  draws (cross-referenced there); ADR-0022's actual invariant is untouched.
- **Does not make synthetic data a place to judge a strategy.** This is still
  regime-free GBM: no fat tails, no gaps, no regimes, no microstructure, and now
  explicitly no unconditional intra-session variance. Fixing range independence
  removes a *bug* that made a walk-forward meaningless; it does not make a synthetic
  walk-forward *meaningful*. Only real data can do that, and now demonstrably does:
  the parallel real-data lane measured actual IS→OOS degradation on real ETF data
  (`core10`), with OOS Sharpe +0.48 to +0.75 across three strategies and retention
  88-155% — and showed that the above-100% retention is a **regime artifact** of
  anchored folds, not evidence of robustness. Even there, three folds is far too few
  to conclude anything. Synthetic data validates the plumbing; real data judges the
  edge; forward paper results outrank both.
- **`RecentWindowFeed` is left alone.** It re-fetches an overlapping window on every
  poll (`datetime.min` → `now`) by design. That was safe before this change only by
  accident — `datetime.min` is a constant, so positions were stable across polls, even
  as the prices themselves were nonsense — and it is now correct by construction. Its
  ever-growing fetch window remains a known inefficiency, recorded here rather than
  fixed.
