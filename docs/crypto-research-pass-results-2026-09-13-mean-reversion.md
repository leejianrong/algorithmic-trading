# Crypto strategy research pass — results, stage 3 (`mean_reversion`) (2026-09-13)

> **This is stage 3 of an 8-point ticket (KAN-1079, EPIC-140)**, continuing
> [`crypto-research-pass-results-2026-09-09.md`](crypto-research-pass-results-2026-09-09.md)
> (stage 1: four cheap kill tests + two small in-sample sweeps) and
> [`crypto-research-pass-results-2026-09-13.md`](crypto-research-pass-results-2026-09-13.md)
> (stage 2: true `--folds` walk-forward for `sma_crossover`/`momentum`, both killed).
> This session closes the disposition stage 1 explicitly left open:
> `mean_reversion`'s step-4 sweep (skipped in stage 1 on time-budget grounds) and
> step-5 true out-of-sample walk-forward, run for the first time here.
> `cross_sectional` remains killed at step 3 (structural underpowering) and
> `trend_following` remains out of scope, both untouched this session — see the
> two prior documents.
>
> **Naming note:** this session ran on the same real-world calendar date
> (2026-09-13) as stage 2's own write-up session, so the plain
> `crypto-research-pass-results-2026-09-13.md` filename was already taken. This
> document is `crypto-research-pass-results-2026-09-13-mean-reversion.md` instead —
> same date, disambiguated by candidate name rather than by overwriting or
> renumbering stage 2's file.
>
> Every number below is pasted from a real command run against the real Alpaca
> paper account's market-data API (`--source alpaca`), never fabricated, never
> from `--source synthetic`.
>
> **Headline finding, stated up front:** `mean_reversion` **fails all three
> computable/attempted kill criteria** at the first true out-of-sample test. Its
> stage-1 in-sample Sharpe (0.47, already the weakest of the three time-series
> candidates and already carrying a *negative* info ratio vs. `BTC/USD`) does not
> hold up out-of-sample either: mean OOS Sharpe **-0.09** against a +0.64 pooled
> in-sample figure — retention **-14%** (the OOS mean is negative, so "percent
> retained" is itself negative) — and **0 of 3 folds were profitable
> out-of-sample.** This is the same qualitative outcome stage 2 found for
> `sma_crossover`/`momentum`, but arrived at from a different starting point: those
> two had *strong-looking* in-sample Sharpes that collapsed; `mean_reversion`'s
> in-sample signal was already weak and mixed with a negative relative-performance
> flag from stage 1, and OOS simply confirms and extends that weakness into
> outright negative territory rather than reversing a strong prior read.

## Step 4 — in-sample sweep, the disposition stage 1 left open

Stage 1's cheap kill test ran `mean_reversion` at its shipped defaults
(`period=14, oversold=30.0, exit_level=50.0, weight=0.9`) and found a borderline
pass: positive total return (+55.74%), Sharpe 0.47, 70.5 trades/param (comfortably
above `MIN_TRADES_PER_PARAMETER = 30`), but a **negative info ratio (-0.39)**
against a `BTC/USD` buy-and-hold benchmark (-51.76pp underperformance) — flagged
as the weakest of the three time-series candidates and explicitly **not**
advanced to a step-4 sweep that session, on time-budget grounds. Its disposition
(advance to a full sweep+walk-forward, or treat as effectively killed) was left
open for this follow-up session, per stage 1's own instruction.

**Decision made here: advance it.** The written kill criteria are OOS-based (mean
OOS Sharpe, IS→OOS retention, paired-bootstrap win rate), not an automatic kill on
a negative in-sample info ratio alone — stage 1 itself said as much ("not an
automatic kill by the letter of the written criteria... but a strategy this
session flags as the weakest"). Killing it without ever running its OOS step
would treat a negative *prior* as equivalent to a negative *out-of-sample
finding*, which is exactly the shortcut pre-registration exists to prevent. So
this session ran the sweep and the walk-forward `mean_reversion` was never given,
and reports honestly whatever they show.

**Grid: `period` x `oversold`, bracketing the shipped defaults, `exit_level` and
`weight` held fixed at their defaults** (per the assignment's "8-16 combos, not
50+" instruction and the scoping doc's own "narrower grids than the equity pass"
caution — no bar cache, every combo is a fresh 10-symbol fetch): `period=10,14,20`
x `oversold=20,30,40` = **9 combos**. `exit_level` was left unswept, matching the
shape of stage 2's `momentum` grid (one axis held fixed while the other varies) —
`mean_reversion` has three tunable knobs (`period`, `oversold`, `exit_level`) and
sweeping all three inside the same combo budget the assignment set would either
force a coarser grid on each axis or blow past "not 50+"; `period`/`oversold` are
the two knobs that directly gate the RSI signal's entry/re-entry behavior, while
`exit_level` mainly governs holding-period length, so they were prioritized.

**Hypothesis logged (verbatim from stage 1's `research/crypto_research_ledger.jsonl`
entry, with a short appended note marking this as the step-4 sweep):**

```
A sharp single-pair drawdown on a leverage-heavy crypto venue is disproportionately
likely to be a mechanically-triggered liquidation cascade (forced closes of
over-leveraged longs by an exchange risk engine, clustering at price levels and
self-reinforcing) rather than new information about the asset's value; the
reversion this strategy buys is the price of providing liquidity into that
forced-selling event once it exhausts. Counterparty: leveraged longs forcibly
liquidated by exchange risk engines regardless of their own view, plus
momentum/panic sellers extrapolating the cascade who get run over on the
snap-back. This is a genuinely different mechanism than the equity mean_reversion
hypothesis, which failed twice on equities (ADR-0071, KAN-642) -- tested here
without lowering the bar just because the crypto story is more compelling on
paper. Kill criteria: OOS Sharpe < 0.3, IS->OOS Sharpe retention < 50%,
paired-bootstrap win rate vs BTC/USD < 55% -- same bar as the other candidates,
no discount for the negative equity prior. [Stage 3, step 4: in-sample sweep
over period/oversold following stage 1's borderline-pass, negative-info-ratio
cheap kill test.]
```

```
uv run --env-file .env trading sweep --strategy mean_reversion --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 \
  --param period=10,14,20 --param oversold=20,30,40 --rank-by sharpe \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/mean_reversion_sweep.csv
```

```
Market:        crypto_24_7 (365 days x 1440 min/day) — 1d annualizes at 365 bars/year; risk posture: halt re-arms after 30 bar(s)

Sweep: strategy=mean_reversion symbols=BTC/USD,ETH/USD,SOL/USD,LINK/USD,LTC/USD,BCH/USD,DOGE/USD,UNI/USD,AAVE/USD,AVAX/USD combos=9 ranked by sharpe

rank  period  oversold  window  sharpe  total_return  max_drawdown
----  ------  --------  ------  ------  ------------  ------------
1     14      30        0       0.474   55.74%        33.84%
2     10      30        0       0.452   62.26%        44.61%
3     20      40        0       0.440   58.41%        51.40%
4     14      20        0       0.429   30.86%        18.44%
5     14      40        0       0.415   52.50%        48.48%
6     20      20        0       0.388   9.78%         6.37%
7     20      30        0       0.364   31.22%        37.01%
8     10      20        0       0.297   20.15%        26.91%
9     10      40        0       0.129   -17.72%       62.08%

Trials:        80 scored; the luckiest skill-free one would show Sharpe +0.26 (observed +0.47)
Deflated:      P(true Sharpe > that null best) = 0.68
  ⚠ below 0.95 — after discounting for 80 trial(s), this Sharpe is not distinguishable from the best of that many skill-free runs
  note: the deflation counts 80 trial(s): 9 from this run plus 71 carried over from earlier logged experiment(s) in the ledger — the spread behind the correction is still estimated from this invocation's trials only (the ledger records counts, not each trial's own Sharpe), so this remains a LOWER BOUND twice over: on the trial count made before the ledger existed, and on the spread of the trials it does carry forward

Wrote sweep results to results/research/crypto-kan-1079/mean_reversion_sweep.csv
```

**The IS winner is `period=14, oversold=30` — exactly the shipped defaults** —
which is why this sweep's winning Sharpe (0.474) is nearly identical to stage 1's
cheap-kill-test Sharpe (0.47). Full 9-row CSV:

```
rank,period,oversold,window,win_start,win_end,sharpe,total_return,annualized_return,max_drawdown,win_rate,avg_exposure,peak_exposure
1,14,30,0,2021-01-01,2025-07-31,0.473542,0.557416,0.101545,0.338381,0.661922,0.212415,0.886825
2,10,30,0,2021-01-01,2025-07-31,0.452179,0.62259,0.111447,0.446078,0.686957,0.250114,0.906387
3,20,40,0,2021-01-01,2025-07-31,0.440102,0.58406,0.105632,0.513976,0.63,0.293941,0.900811
4,14,20,0,2021-01-01,2025-07-31,0.42914,0.308633,0.060478,0.184353,0.658333,0.097112,0.873066
5,14,40,0,2021-01-01,2025-07-31,0.415137,0.524977,0.096495,0.484833,0.642353,0.298262,0.901017
6,20,20,0,2021-01-01,2025-07-31,0.387793,0.097807,0.02058,0.063705,0.703704,0.032917,0.684673
7,20,30,0,2021-01-01,2025-07-31,0.363676,0.31222,0.061112,0.370066,0.632258,0.173558,0.895269
8,10,20,0,2021-01-01,2025-07-31,0.296598,0.201536,0.040894,0.269113,0.717213,0.137036,0.890694
9,10,40,0,2021-01-01,2025-07-31,0.128579,-0.177155,-0.041673,0.620764,0.655285,0.313712,0.90888
```

**Deflated probability 0.68 — well below the 0.68 < 0.95 confidence bar, and
noticeably weaker than either `sma_crossover`'s stage-1 sweep (0.92) or
`momentum`'s (0.89).** Even the best-in-grid combo (which turned out to be the
shipped defaults) is not distinguishable from the luckiest of 80 skill-free
trials on this session's own reading — the weakest step-4 deflation of the three
time-series candidates run through this research line so far, consistent with
stage 1's own read that this was the least convincing of the three cheap-kill
passes.

## Step 5 — the one true out-of-sample shot

- **Full available range:** `2021-01-01` to `2026-09-12` (yesterday relative to
  this session's real run date of 2026-09-13, computed the same way stage 2 did,
  to avoid requesting a forming/incomplete bar). Matches stage 2's own OOS range
  exactly (both stage 2 and this session ran on the same calendar day), so the
  fold boundaries below are identical to stage 2's `sma_crossover`/`momentum`
  folds — a deliberate consequence of `--folds` cutting `[start, end]`
  deterministically, not a coincidence this session engineered.
- **Universe / market / interval:** `--symbols @crypto10 --source alpaca --market
  crypto --interval 1d`, unchanged from stages 1-2 and the frozen scoping doc.
- **Fold count: 3, anchored (expanding) mode** — same reasoning as stage 2: over
  this ~5.7-year span each fold's OOS window is roughly 1.4 years
  (~519 daily bars per the printed bootstrap block, comfortably above
  `MIN_BOOTSTRAP_OBSERVATIONS = 30`), and this session did not revisit that choice
  after seeing a result.
- **Grid: the same 9 combos as step 4 (`period=10,14,20` x `oversold=20,30,40`),
  reused rather than re-derived.** The assignment calls for bracketing step 4's
  winner by one grid step in each direction on each swept parameter, exactly as
  stage 2 did for `sma_crossover`/`momentum`. Here that bracket is a genuine no-op:
  step 4's winner (`period=14, oversold=30`) sits at the **exact center** of the
  grid already swept in step 4 (`period` one step below/above 14 is 10/20,
  already in the grid; `oversold` one step below/above 30 is 20/40, already in the
  grid) — so "bracket the winner by one step each direction" and "reuse step 4's
  grid" are the same instruction here, not a shortcut taken to save a fetch. Noted
  explicitly rather than silently reusing the CLI flags from step 4 unremarked.
- **Costs:** `CostConfig.crypto()` shared defaults, unchanged (5.0 bps slippage,
  taker fee per the venue's tier). **Caveat repeated, as instructed:** ADR-0061
  measured crypto's 5.0 bps slippage default as **optimistic** (+13.02 bps mean
  realized vs. modelled, n=11, not independent) — the opposite direction from
  equities. Every Sharpe/return number below is probably **flattering** relative
  to what a live crypto session would realize — which, given this candidate is
  already killed below, only strengthens the kill.
- **`SOL/USD` caveat, repeated:** ~20% of its expected daily bars are missing on
  this venue (KAN-1078,
  [`crypto-daily-tape-density-2026-09-09.md`](crypto-daily-tape-density-2026-09-09.md))
  — carried through every fold's RSI/lookback arithmetic for that one symbol in
  the 10-symbol universe.
- **Survivorship caveat, repeated:** `crypto10` is "the worst of the three"
  baskets in this repo — 2026's ten survivors, on a venue that is itself a
  survivor filter. Every number below is an upper bound on an upper bound.
- **Ledger:** logged to `research/crypto_research_ledger.jsonl` (the same file
  stages 1-2 used, kept separate from the equity line's `kan642_trial_ledger.jsonl`
  for the reasons stage 1's results doc already gave), with `--hypothesis` text
  reusing stage 1's exact mechanism/counterparty framing verbatim, with a short
  appended note marking this as the stage-3 walk-forward step and naming step 4's
  swept winner for continuity.
- **Shared kill criteria, unchanged since stage 1:** kill if mean OOS Sharpe < 0.3,
  or IS→OOS Sharpe retention < 50%, or paired-bootstrap win rate vs. a `BTC/USD`
  buy-and-hold benchmark < 55%. Not loosened or tightened for this candidate
  despite its different (mechanism-appealing, prior-negative) story, per the
  assignment's explicit instruction.

### Resource discipline actually observed

`free -h`/`uptime` were checked before branch setup, immediately before each
heavy command, and after both completed; both commands ran under `nice -n 19`:

| When | Swap used | Load average (1/5/15 min) |
|---|---|---|
| Before branch setup | 2.7/4.0Gi | 5.66 / 9.20 / 10.74 |
| Immediately before step 4 sweep | 2.7/4.0Gi | 2.87 / 7.97 / 10.25 |
| Immediately before step 5 walk-forward | 2.6/4.0Gi | 1.44 / 6.13 / 9.37 |
| After both commands completed | 2.6/4.0Gi | 1.07 / 4.77 / 8.58 |

Unlike stage 2 (where load rose across the session), load **fell** across this
one — this session's own two commands both finished in well under a minute each
with no retry, no rate limit, and no resource-related failure, and the machine's
background load from other concurrent sessions appears to have eased rather than
worsened between the two commands.

### `mean_reversion` — walk-forward OOS

```
uv run --env-file .env trading sweep --strategy mean_reversion --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2026-09-12 \
  --param period=10,14,20 --param oversold=20,30,40 --rank-by sharpe \
  --folds 3 --wf-mode anchored --bootstrap \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/mean_reversion_walkforward.csv
```

```
Market:        crypto_24_7 (365 days x 1440 min/day) — 1d annualizes at 365 bars/year; risk posture: halt re-arms after 30 bar(s)

Walk-forward: strategy=mean_reversion symbols=BTC/USD,ETH/USD,SOL/USD,LINK/USD,LTC/USD,BCH/USD,DOGE/USD,UNI/USD,AAVE/USD,AVAX/USD folds=3 mode=anchored tuned on sharpe

fold 0  IS 2021-01-01..2022-06-05 -> OOS 2022-06-06..2023-11-07  [period=20, oversold=20]  IS sharpe +0.78 -> OOS sharpe -0.02
    Sharpe 95% CI: [-0.76, +1.02]  (stationary block bootstrap: 1000 resamples, 60-bar blocks, 519 return periods, seed 20260808)
      ⚠ the interval straddles zero — this sample cannot distinguish the strategy from having no edge at all; the point estimate is not a finding
fold 1  IS 2021-01-01..2023-11-07 -> OOS 2023-11-08..2025-04-10  [period=14, oversold=30]  IS sharpe +0.66 -> OOS sharpe -0.03
    Sharpe 95% CI: [-0.94, +1.32]  (stationary block bootstrap: 1000 resamples, 60-bar blocks, 519 return periods, seed 20260808)
      ⚠ the interval straddles zero — this sample cannot distinguish the strategy from having no edge at all; the point estimate is not a finding
fold 2  IS 2021-01-01..2025-04-10 -> OOS 2025-04-11..2026-09-12  [period=14, oversold=30]  IS sharpe +0.47 -> OOS sharpe -0.22
    Sharpe 95% CI: [-1.49, +1.89]  (stationary block bootstrap: 1000 resamples, 60-bar blocks, 519 return periods, seed 20260808)
      ⚠ the interval straddles zero — this sample cannot distinguish the strategy from having no edge at all; the point estimate is not a finding

OUT-OF-SAMPLE mean sharpe -0.09 (in-sample +0.64; degradation +0.72, retained -14%)
0/3 fold(s) profitable out of sample — this is the number that counts; the in-sample figures are tuned and always flatter.

Trials:        107 scored; the luckiest skill-free one would show Sharpe +0.52 (observed +0.53)
Deflated:      P(true Sharpe > that null best) = 0.51
  ⚠ below 0.95 — after discounting for 107 trial(s), this Sharpe is not distinguishable from the best of that many skill-free runs
  note: the deflation counts 107 trial(s): 27 from this run plus 80 carried over from earlier logged experiment(s) in the ledger — the spread behind the correction is still estimated from this invocation's trials only (the ledger records counts, not each trial's own Sharpe), so this remains a LOWER BOUND twice over: on the trial count made before the ledger existed, and on the spread of the trials it does carry forward

Wrote walk-forward results to results/research/crypto-kan-1079/mean_reversion_walkforward.csv
```

CSV (`mean_reversion_walkforward.csv`), pasted in full:

```
fold,is_start,is_end,oos_start,oos_end,params,is_sharpe,is_total_return,oos_sharpe,oos_total_return,oos_max_drawdown
0,2021-01-01,2022-06-05,2022-06-06,2023-11-07,"period=20, oversold=20",0.7774,0.067852,-0.0202,-0.004171,0.063705
1,2021-01-01,2023-11-07,2023-11-08,2025-04-10,"period=14, oversold=30",0.6597,0.594856,-0.0324,-0.052774,0.213198
2,2021-01-01,2025-04-10,2025-04-11,2026-09-12,"period=14, oversold=30",0.4684,0.511223,-0.2166,-0.118293,0.400865
```

**Verdict against the three shared kill criteria:**

| Criterion | Bar | Observed | Result |
|---|---|---|---|
| Mean OOS Sharpe | ≥ 0.3 | -0.09 | **FAIL** |
| IS→OOS retention | ≥ 50% | -14% | **FAIL** |
| Paired-bootstrap win rate vs. `BTC/USD` | ≥ 55% | not separately computed | see note below |

**KILLED at step 5.** Both computable numeric bars are breached decisively — the
mean OOS Sharpe is not merely below the 0.3 floor, it is **negative**, and
retention is likewise negative (the OOS mean underperforms even a hypothetical
zero-skill baseline relative to the in-sample figure it was tuned to beat) — and
all three folds' own bootstrap confidence intervals straddle zero, meaning no
individual fold's point estimate can be distinguished from no edge at all. The
third criterion (paired win rate vs. a `BTC/USD` buy-and-hold benchmark) was
**not** separately computed, for the identical reason stage 2 gave for
`sma_crossover`/`momentum`: `sweep --folds` has no built-in paired-bootstrap-vs-
benchmark output, computing it honestly would require re-running each fold's
specific OOS winner combo (which differs by fold here too — see below) against a
benchmark comparison, and the strategy is already killed decisively on the other
two bars. Spending an additional live-data command to compute a third
confirmatory number when the verdict is already unambiguous was judged not worth
the marginal resource cost, recorded here as an explicit choice.

**Parameter-selection pattern, notably different from stage 2's:** fold 0's
in-sample search picked `period=20, oversold=20` — both at the **edges** of the
swept grid (longest lookback, lowest oversold threshold) — while folds 1 and 2
both picked `period=14, oversold=30`, exactly the shipped defaults and exactly
step 4's own full-range winner. This is **not** the same "always reaches for the
grid's fastest-reacting extreme" signature stage 2 found for `sma_crossover`
(`slow=20` every fold) and `momentum` (`lookback=10` every fold) — here two of
three folds land on the *center* of the grid, and only the shortest, most
data-starved fold (2021-01-01..2022-06-05, ~17 months) reaches for an edge. Read
together with the OOS collapse, this is more consistent with "the in-sample
search mostly settles on a stable combo, and that combo simply has no
out-of-sample edge" than with "the search is chasing short-horizon noise
differently each time" — a distinct failure mode from stage 2's candidates, but
a failure mode all the same: a stable in-sample choice that does not generalize
is exactly what a walk-forward is built to catch, and did.

## Contrast with stage 1, stage 2, and the equity pass

Three points of contrast, stated because the assignment asks not to soften a fail
because the mechanism story is appealing:

1. **This candidate's in-sample signal was already the weakest of the three
   time-series candidates before any OOS step ran.** Stage 1 flagged
   `mean_reversion`'s cheap kill test as a "borderline pass" specifically because
   of its negative info ratio against `BTC/USD` (-0.39) — a relative-performance
   red flag `sma_crossover` (+0.42) and `momentum` (+0.03) did not carry. Step 4's
   deflated probability here (0.68) is likewise the weakest of the three
   candidates' step-4 sweeps (`sma_crossover` 0.92, `momentum` 0.89). The OOS
   collapse measured this session is therefore not a surprise reversal of a
   strong prior — it is a **third, independent confirmation** of a weak signal
   that had already shown warning signs at two earlier, cheaper steps.
2. **The collapse is quantitatively worse than stage 2's, not merely
   qualitatively similar.** `sma_crossover`/`momentum` retained 6%/14% of their
   in-sample Sharpe out-of-sample — small positive numbers. `mean_reversion`
   retained **-14%** — the mean OOS Sharpe is outright negative, meaning the
   strategy would have *lost* risk-adjusted return relative to holding cash, on
   average across the three folds, in a period its own in-sample search had just
   finished optimizing over.
3. **The liquidation-cascade mechanism drafted in the scoping document
   (`crypto-research-pass-2026-09-02.md` §4) was explicitly argued to be
   structurally *stronger* than the equity mean-reversion story that failed
   twice on equities (ADR-0071, KAN-642).** That argument was about mechanism
   plausibility, not about pre-committing to a favorable result — and it is not
   borne out here: on real Alpaca daily `crypto10` data, this candidate fails
   its OOS test at least as decisively as the equity version did, and more
   decisively than either `sma_crossover` or `momentum` failed theirs on the same
   crypto universe. A compelling-sounding mechanism story earned this candidate a
   full, undiscounted test (per stage 1's own instruction) — it did not earn it a
   pass.

As with stage 2, this session's ~5.7-year crypto history cannot fully separate
"the mechanism doesn't hold at daily granularity on this venue" from "not enough
independent crypto market regimes exist yet to trust any null result on this
short a tape" — the same caveat stage 2 raised applies here too, and this
session's data does not resolve it either. Neither reading changes the verdict
against the pre-registered kill bars.

## Steps 6-8 — not attempted, and why

Per the assignment's framing (matching stage 2's own reasoning), steps 6-8
(robustness battery, cumulative-ledger deflation beyond what step 5 already
reports, portfolio fit) apply only to a candidate that survived its OOS kill
check. `mean_reversion` did not survive — it is killed outright on two
independent bars, by a wide margin on both. There is no survivor to run a
cost-sensitivity check, a regime split, a Monte Carlo shuffle, or a portfolio-fit
correlation against, and running any of steps 6-8 here would spend additional
live-Alpaca network cost decorating a verdict step 5 already settled.

## Is this an ADR?

**No — a measurement, not a decision**, for the same reason stages 1 and 2 gave
for themselves. Nothing in `src/trading/` changed; no new tool, policy, config
default, or CLI behavior was introduced or altered. This document records what
one more parameter search, run through an already-built and already-decided
walk-forward mechanism (ADR-0026/0074), found when pointed at real crypto data
for a third strategy family.

## What remains for a follow-up session

- **All four in-scope crypto candidates named in the scoping document now have a
  disposition.** `sma_crossover` and `momentum`: killed at step 5 (stage 2).
  `mean_reversion`: killed at step 5 (this session). `cross_sectional`: killed at
  step 3 on structural underpowering (stage 1), untouched here. None of the four
  qualifies to proceed to paper incubation (playbook step 9) on crypto today.
  `trend_following` remains explicitly out of scope for a `crypto10`-only test
  per the scoping document's own structural argument, untouched here.
- **Re-opening `cross_sectional`** still needs either a larger/PIT crypto universe
  (does not exist today) or a policy decision to accept a smaller `top_k` via a
  `sweep --param top_k=2,3,4` run (not attempted in any stage of this ticket so
  far).
- **No CLI path computes a paired-bootstrap win rate against a benchmark for a
  `--folds` walk-forward's OOS winner** — the same gap stage 2 named, hit again
  here for the same reason (moot once a candidate is already killed on the other
  two bars). A future candidate that clears the first two bars would still need
  either a new tool or a manual reconstruction (re-run each fold's specific OOS
  winner combo through `backtest --benchmark BTC/USD --bootstrap` over that
  fold's OOS span) to finish evaluating that third bar.
- **The tooling gap both prior stages named — `trading backtest` has no
  `--param`/strategy-kwarg override, only `sweep` does — did not block anything
  in this session** (`sweep` was the only command needed for both steps), but is
  repeated here for completeness since it remains true and unfixed.
- **This session did not re-litigate or widen the shared kill criteria.** They
  were applied to `mean_reversion` exactly as pre-registered in stage 1, with no
  discount for its differently-motivated mechanism story and no leniency for its
  already-weaker step-3/step-4 showing.

## Session accounting

- Both commands (step 4 sweep, step 5 walk-forward) completed without error,
  without retry, and without hitting any rate limit or resource-related failure.
- Two new entries were appended to `research/crypto_research_ledger.jsonl`
  (`sweep` / `mean_reversion`, 9 trials; `sweep --folds` / `mean_reversion`, 27
  trials), bringing the file to 10 total entries (8 from stages 1-2, 2 from this
  session).
- `git status` at the time of writing this document shows a clean working tree
  aside from this document, the ledger file's two new lines, and the branch's own
  commits — no orphaned files, no half-written state. No process was left
  running; no `trading paper` command was ever invoked, matching the
  assignment's explicit instruction that this task is read-only historical-data
  research, never order-submitting.
