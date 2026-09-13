# Crypto strategy research pass — results, stage 2 (2026-09-13)

> **This is stage 2 of an 8-point ticket (KAN-1079, EPIC-140)**, continuing
> [`crypto-research-pass-results-2026-09-09.md`](crypto-research-pass-results-2026-09-09.md)
> (stage 1: four cheap kill tests + two small in-sample sweeps). This session runs
> [`research-playbook.md`](research-playbook.md) §5 — the **one true out-of-sample
> shot**, `trading sweep --folds` — for the two candidates that cleared stage 1
> cleanly: `sma_crossover` and `momentum`. `mean_reversion` and `cross_sectional`
> are untouched (their stage-1 dispositions stand; see that document).
> `trend_following` remains explicitly out of scope per
> [`crypto-research-pass-2026-09-02.md`](crypto-research-pass-2026-09-02.md) (frozen,
> not re-derived here). Every number below is pasted from a real command run
> against the real Alpaca paper account's market-data API (`--source alpaca`),
> never fabricated, never from `--source synthetic`.
>
> **Headline finding, stated up front because it reverses the optimistic read
> stage 1's in-sample-only numbers invited:** both candidates **fail all
> computable kill criteria decisively** at the first true out-of-sample test.
> Stage 1's promising in-sample Sharpes (1.13 and 0.68 on defaults; up to 1.14 and
> 1.03 swept) evaporate to a mean out-of-sample Sharpe of **+0.08**
> (`sma_crossover`) and **+0.22** (`momentum`) — retentions of **6%** and **14%**
> against a 50% kill bar. This is the opposite of what the equivalent equity pass
> found for the same two candidates (`deployment-decision-2026-09-01.md`: 99% and
> 107% retention, OOS *improving* on IS) — see "Contrast with the equity pass"
> below for why that asymmetry itself is worth taking seriously rather than
> explaining away.

## Step 5 — methodology (decided before any OOS number existed)

- **Full available range:** `2021-01-01` to `2026-09-12` (yesterday relative to
  this session's real run date of 2026-09-13, computed to avoid requesting a
  forming/incomplete bar). This is **not** stage 1's manual IS/OOS split
  (2021-01-01..2025-07-31 / 2025-08-01..2026-09-08) — per the assignment's own
  instruction, `--folds` cuts its own full requested range into folds and picks
  its own in-sample/out-of-sample boundaries; reusing stage 1's manual split
  would have been a category error (mixing a hand-drawn boundary with the tool's
  own fold-splitting logic).
- **Universe / market / interval:** `--symbols @crypto10 --source alpaca --market
  crypto --interval 1d`, unchanged from stage 1 and the frozen scoping doc.
- **Fold count: 3, anchored (expanding) mode**, matching the equity KAN-642 pass's
  own default and `research-playbook.md`'s worked example. Reasoning stated
  explicitly rather than defaulted: `--folds 3` cuts `[start, end]` into 4 equal
  segments (ADR-0026 — `folds + 1` segments); over this ~5.7-year span that gives
  each fold an OOS test window of roughly 1.4 years (~520 daily bars, confirmed
  by the printed fold boundaries below) — comfortably above
  `MIN_BOOTSTRAP_OBSERVATIONS = 30`, so a `--bootstrap` confidence interval on
  each fold's OOS Sharpe would actually compute (it did, see below). A higher
  fold count would shrink each OOS window and, per the scoping doc's own §1
  caution, cost proportionally more zero-cache network round-trips for the whole
  10-symbol universe per fold. 3 was judged the right balance and was not
  revisited after seeing a result.
- **Grids: narrow, centered on each candidate's stage-1 sweep winner**, per the
  assignment's "8-16 combos, not 50+" instruction and the scoping doc's own
  "narrower grids than the equity pass" caution (no bar cache — every combo, in
  every fold, is a fresh 10-symbol fetch):
  - `sma_crossover`: `fast=3,5,8` × `slow=20,30,45` = **9 combos** (stage 1's
    winner was `fast=5, slow=30`; this grid brackets it on both axes with one
    step in each direction).
  - `momentum`: `lookback=10,15,20,25,30,40,50,60` = **8 combos** (stage 1's
    winner was `lookback=20`; this grid is denser near 20 with wider steps
    further out, since `momentum` has only one free parameter to sweep).
- **Costs:** `CostConfig.crypto()` shared defaults, unchanged (5.0 bps slippage,
  taker fee per the venue's tier). **Caveat repeated, as instructed, in every
  result table:** ADR-0061 measured crypto's 5.0 bps slippage default as
  **optimistic** (+13.02 bps mean realized vs. modelled, n=11, not independent)
  — the opposite direction from equities. Every Sharpe/return number below is
  probably **flattering** relative to what a live crypto session would realize.
- **`SOL/USD` caveat, repeated:** ~20% of its expected daily bars are missing on
  this venue (KAN-1078) — carried through every fold's lookback/rebalance
  arithmetic for that one symbol in the 10-symbol universe.
- **Survivorship caveat, repeated:** `crypto10` is "the worst of the three"
  baskets in this repo — 2026's ten survivors, on a venue that is itself a
  survivor filter. Every number below is an upper bound on an upper bound.
- **Ledger:** both commands logged to `research/crypto_research_ledger.jsonl`
  (the same file stage 1 created, kept separate from the equity line's
  `kan642_trial_ledger.jsonl` for the reasons stage 1's results doc already gave
  — a different population under ADR-0039/0062 deflation), with `--hypothesis`
  text reusing stage 1's exact mechanism/counterparty framing from
  `crypto-research-pass-results-2026-09-09.md` §1, verbatim in substance, with a
  short appended note marking this as the stage-2 walk-forward step and naming
  stage 1's swept winner for continuity.
- **Shared kill criteria, unchanged from stage 1** (applied now, for real, for
  the first time): kill if mean OOS Sharpe < 0.3, or IS→OOS Sharpe retention <
  50%, or paired-bootstrap win rate vs. a `BTC/USD` buy-and-hold benchmark < 55%.

## Resource discipline actually observed

`free -h`/`uptime` were checked immediately before each heavy command, per the
assignment's instruction, and both commands ran under `nice -n 19`:

| When | Swap used | Load average (1/5/15 min) |
|---|---|---|
| Before branch setup | 4.8/8.0Gi | 1.92 / 4.39 / 6.79 |
| Immediately before `sma_crossover --folds` | 4.8/8.0Gi | 4.21 / 4.43 / 6.62 |
| Immediately before `momentum --folds` | 5.1/8.0Gi | 7.20 / 5.17 / 6.53 |
| After both commands completed | 5.3/8.0Gi | 5.32 / 6.03 / 6.72 |

Swap was persistently 60-66% occupied and the 1-minute load average spiked to
7.20 before the `momentum` run — elevated, and consistent with this being a
shared machine under load from other concurrent sessions (this session's own
memory footprint per command is small: a 10-symbol daily-bar fetch is a few
thousand rows). Both commands nonetheless returned promptly, with no timeout, no
retry, and no visible resource-related failure. Given that, and because the
grids were already sized conservatively (9 and 8 combos, well inside the
assignment's "not 50+" ceiling), no further shrinking was applied — but the
readings are recorded here rather than silently omitted, as instructed, in case
a reviewer wants to know how much headroom this session actually had.

## Results

### 1. `sma_crossover` — walk-forward OOS

```
uv run --env-file .env trading sweep --strategy sma_crossover --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2026-09-12 \
  --param fast=3,5,8 --param slow=20,30,45 --rank-by sharpe \
  --folds 3 --wf-mode anchored --bootstrap \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/sma_crossover_walkforward.csv
```

```
Market:        crypto_24_7 (365 days x 1440 min/day) — 1d annualizes at 365 bars/year;
               risk posture: halt re-arms after 30 bar(s)

fold 0  IS 2021-01-01..2022-06-05 -> OOS 2022-06-06..2023-11-07  [fast=3, slow=20]
        IS sharpe +1.81 -> OOS sharpe -0.32
    Sharpe 95% CI: [-1.67, +0.83]  (stationary block bootstrap: 1000 resamples,
        60-bar blocks, 519 return periods, seed 20260808)
      ⚠ the interval straddles zero — this sample cannot distinguish the strategy
        from having no edge at all
fold 1  IS 2021-01-01..2023-11-07 -> OOS 2023-11-08..2025-04-10  [fast=8, slow=20]
        IS sharpe +1.14 -> OOS sharpe +0.60
    Sharpe 95% CI: [-1.22, +2.09]  ⚠ straddles zero
fold 2  IS 2021-01-01..2025-04-10 -> OOS 2025-04-11..2026-09-12  [fast=5, slow=20]
        IS sharpe +1.15 -> OOS sharpe -0.04
    Sharpe 95% CI: [-1.71, +1.41]  ⚠ straddles zero

OUT-OF-SAMPLE mean sharpe +0.08 (in-sample +1.36; degradation +1.29, retained 6%)
1/3 fold(s) profitable out of sample.

Trials:        47 scored (27 this run + 20 carried); luckiest skill-free trial
               would show Sharpe +0.69 (observed +1.27)
Deflated:      P(true Sharpe > that null best) = 0.96
```

CSV (`sma_crossover_walkforward.csv`), pasted in full:

```
fold,is_start,is_end,oos_start,oos_end,params,is_sharpe,is_total_return,oos_sharpe,oos_total_return,oos_max_drawdown
0,2021-01-01,2022-06-05,2022-06-06,2023-11-07,"fast=3, slow=20",1.8078,2.548964,-0.3243,-0.200088,0.435533
1,2021-01-01,2023-11-07,2023-11-08,2025-04-10,"fast=8, slow=20",1.1365,2.447855,0.6009,0.253045,0.353781
2,2021-01-01,2025-04-10,2025-04-11,2026-09-12,"fast=5, slow=20",1.1489,6.583895,-0.0395,-0.085421,0.434522
```

**Verdict against the three shared kill criteria:**

| Criterion | Bar | Observed | Result |
|---|---|---|---|
| Mean OOS Sharpe | ≥ 0.3 | +0.08 | **FAIL** |
| IS→OOS retention | ≥ 50% | 6% | **FAIL** |
| Paired-bootstrap win rate vs. `BTC/USD` | ≥ 55% | not separately computed | see note below |

**KILLED at step 5.** Two of the three numeric bars are breached by a wide
margin (Sharpe less than a third of the bar; retention roughly a twelfth of the
bar) and every one of the three folds' own bootstrap confidence intervals
straddles zero, meaning even the individual point estimates cannot be
distinguished from no edge at all. The third criterion (paired win rate vs. a
`BTC/USD` buy-and-hold benchmark) was **not** separately computed — `sweep
--folds` has no built-in paired-bootstrap-vs-benchmark output, and computing it
honestly would require re-running each fold's specific OOS winner combo against
a benchmark comparison (a different combo per fold, since the IS winner
changed fold to fold — see below). Given the strategy is already killed
decisively on two independent bars, spending additional live-data commands to
compute a third confirmatory number was judged not worth the marginal
resource cost, and is recorded here as an explicit choice, not an oversight.

**Notable and worth flagging on its own:** the in-sample winner picked
`slow=20` — the grid's own minimum — in **all three folds**, with `fast`
wandering (3, 8, 5). The search is not settling on a stable region of the
parameter space; it is repeatedly reaching for the fastest-reacting end of the
swept range. That is a signature more consistent with the in-sample optimizer
fitting to each expanding window's own idiosyncratic short-term noise than
with a stable multi-day trend-persistence mechanism (the hypothesis this
candidate was built to test) — independently consistent with, not merely
coincidental to, the OOS collapse measured above.

### 2. `momentum` — walk-forward OOS

```
uv run --env-file .env trading sweep --strategy momentum --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2026-09-12 \
  --param lookback=10,15,20,25,30,40,50,60 --rank-by sharpe \
  --folds 3 --wf-mode anchored --bootstrap \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/momentum_walkforward.csv
```

```
fold 0  IS 2021-01-01..2022-06-05 -> OOS 2022-06-06..2023-11-07  [lookback=10]
        IS sharpe +2.19 -> OOS sharpe +0.19
    Sharpe 95% CI: [-1.19, +1.40]  ⚠ straddles zero
fold 1  IS 2021-01-01..2023-11-07 -> OOS 2023-11-08..2025-04-10  [lookback=10]
        IS sharpe +1.33 -> OOS sharpe +0.48
    Sharpe 95% CI: [-1.78, +2.18]  ⚠ straddles zero
fold 2  IS 2021-01-01..2025-04-10 -> OOS 2025-04-11..2026-09-12  [lookback=10]
        IS sharpe +1.19 -> OOS sharpe +0.00
    Sharpe 95% CI: [-1.75, +1.41]  ⚠ straddles zero

OUT-OF-SAMPLE mean sharpe +0.22 (in-sample +1.57; degradation +1.35, retained 14%)
2/3 fold(s) profitable out of sample.

Trials:        71 scored (24 this run + 47 carried); luckiest skill-free trial
               would show Sharpe +1.02 (observed +1.42)
Deflated:      P(true Sharpe > that null best) = 0.90
  ⚠ below 0.95
```

CSV (`momentum_walkforward.csv`), pasted in full:

```
fold,is_start,is_end,oos_start,oos_end,params,is_sharpe,is_total_return,oos_sharpe,oos_total_return,oos_max_drawdown
0,2021-01-01,2022-06-05,2022-06-06,2023-11-07,lookback=10,2.1943,3.246786,0.1898,0.005466,0.319104
1,2021-01-01,2023-11-07,2023-11-08,2025-04-10,lookback=10,1.3281,3.135246,0.4796,0.166524,0.376807
2,2021-01-01,2025-04-10,2025-04-11,2026-09-12,lookback=10,1.1920,6.344801,0.0050,-0.070521,0.451430
```

**Verdict against the three shared kill criteria:**

| Criterion | Bar | Observed | Result |
|---|---|---|---|
| Mean OOS Sharpe | ≥ 0.3 | +0.22 | **FAIL** |
| IS→OOS retention | ≥ 50% | 14% | **FAIL** |
| Paired-bootstrap win rate vs. `BTC/USD` | ≥ 55% | not separately computed | same reasoning as above |

**KILLED at step 5**, for the same reasons as `sma_crossover`: two of three bars
breached by a wide margin, and 2 of 3 individual fold CIs straddle zero (only
fold 1's point estimate of +0.48 comes closer, and its own CI `[-1.78, +2.18]`
still straddles zero, so even that fold is not a real finding on its own).

**Same grid-boundary observation as `sma_crossover`:** the in-sample winner
picked `lookback=10` — the grid's own minimum — in **all three folds**. Both
candidates independently exhibit the identical pattern: whatever edge the
in-sample window sees, it keeps living at the fastest end of the parameter
space the grid allowed, not settling into a stable interior optimum. Taken
together across both candidates, this reads less like "the true optimum lies
just outside this grid" (which would predict different fast/slow or lookback
choices per candidate, or convergence toward one specific value) and more like
"the in-sample search is chasing short-horizon noise that a faster parameter
happens to fit best in whichever window it's shown," which is exactly the
failure mode a true walk-forward exists to catch — and did.

## Contrast with the equity pass — why the asymmetry itself matters

The equivalent equity KAN-642 pass (`deployment-decision-2026-09-01.md`) found
the *same two candidates*, on `@blue20`/2008-2023, with **retention above
100%** — OOS Sharpe *improving* on IS (99%, 107%) — described there as "the
strongest possible answer to 'is this fit to noise.'" This crypto pass finds
the opposite: retention of 6% and 14%, i.e. almost total OOS collapse. Two
honest readings, not mutually exclusive:

1. **The crypto-native mechanism drafted in the scoping document
   (`crypto-research-pass-2026-09-02.md` §4 — "retail narrative-chasing,
   crypto's disposition-effect sellers") may simply not hold on this venue's
   real tape** at daily granularity over this window, whatever its surface
   plausibility as a story. A hypothesis with a named mechanism and
   counterparty is still a hypothesis, not a finding, until it survives contact
   with out-of-sample data — and here it did not.
2. **Five and a half years (2021-2026) covers far fewer, and far more
   correlated, market regimes than 16 years (2008-2023) of equity history
   spanning the GFC, 2018, COVID, and 2022.** Crypto's own history is short and
   `crypto10`'s biggest trending move (the 2021 bull run and 2022 collapse) sits
   almost entirely inside fold 0's in-sample span in the anchored design — every
   later fold's IS window is dominated by data that already includes it, and
   the OOS windows (mid-2022 onward) span a comparatively rangier, lower-trend
   period for most of these ten coins. This is a genuine limitation of what
   five years of one asset class's history can test, not a defect in the
   walk-forward tool, and it cuts toward "we don't yet have enough independent
   crypto market regimes to fully trust this null result either" — a caveat in
   the other direction from the survivorship/tape-gap caveats already carried
   throughout this research line.

Neither reading rescues either candidate against the pre-registered kill bars
— both fail those regardless of which explanation is closer to the truth. The
distinction matters only for how much weight a future session should put on
"crypto trend-following is dead" versus "this specific 5.7-year window didn't
have the regime diversity to show it," and this stage 2 session does not have
the additional data needed to settle which is correct.

## Steps 6-8 (robustness battery, cumulative deflation, portfolio fit) — not attempted, and why

Per the assignment's own framing, steps 6-8 apply only to "whichever candidate(s)
actually survived their OOS kill-criteria check." **Neither `sma_crossover` nor
`momentum` survived** — both are killed outright by step 5's own numbers, on
two independent bars each, by wide margins. There is therefore no survivor to
run a cost-sensitivity check, a regime split, a Monte Carlo shuffle, or a
portfolio-fit correlation against (step 8 specifically needs *both* candidates
to survive, per the assignment's own instruction, which did not happen here
either). Running any of steps 6-8 on a candidate already killed at step 5 would
not answer a real question — it would be additional live-Alpaca network cost
spent decorating a verdict that step 5 already settled.

This is judged a complete, honestly-reported stage-2 deliverable on its own,
per the assignment's own explicit instruction not to rush further steps "to
beat a clock" or "to complete more of the ticket" when the honest stopping
point has already been reached. It was reached here for a different reason
than stage 1's (a resource/time boundary) — the *evidence itself* says stop,
which is the cleaner of the two ways this playbook is supposed to end a
research thread.

## Is this an ADR?

**No — a measurement, not a decision**, following the same reasoning stage 1's
results doc applied to itself (and per the assignment's own instruction to make
this judgment call explicitly rather than default to filing one). Nothing in
`src/trading/` changed; no new tool, policy, config default, or CLI behavior
was introduced or altered. This document records what two specific parameter
searches, run through an already-built and already-decided walk-forward
mechanism (ADR-0026/0074), found when pointed at real crypto data for the first
time. The three most recent ADRs at the time of this session
(`docs/adr/0072-sp500-universe-for-cross-sectional.md`,
`0073-tape-density-screen.md`, `0074-walk-forward-and-paper-trial-accounting.md`)
are each a genuine design decision (a new universe-resolution rule, a new
screening tool, a new accounting mechanism); this document is neither.

## What remains for a follow-up session

- **`sma_crossover` and `momentum` on `crypto10` at `--interval 1d` are now
  disposed of, negatively, by real out-of-sample evidence.** Per the shared
  kill criteria this research line pre-registered in stage 1, neither qualifies
  to proceed to paper incubation (playbook step 9) on this evidence. Nothing in
  this session's own data distinguishes "the crypto mechanism doesn't exist"
  from "5.7 years wasn't enough regime diversity to see it" (see "Contrast with
  the equity pass" above) — a future session with a materially longer crypto
  tape (which does not exist yet on this venue; Alpaca's crypto history starts
  2021-01-01) or a different universe could revisit this, but that is new data
  becoming available, not a gap in this session's method.
- **`mean_reversion`'s stage-1-left-open disposition (advance to a sweep, or
  treat as a step-3-adjacent kill) remains open** — this session was explicitly
  scoped to the two walk-forwards for the candidates that cleared stage 1
  *cleanly*, and `mean_reversion` did not. Untouched here, as instructed.
- **`cross_sectional` remains killed at step 3** on structural underpowering
  (11.2 trades/param on `crypto10`'s 10-symbol universe) — untouched here, as
  instructed. Re-opening it still needs either a larger crypto universe or a
  `--param top_k` sweep at a smaller `top_k`, neither attempted this session
  (out of scope) or stage 1's.
- **`trend_following` remains out of scope** for a `crypto10`-only test, per
  the scoping document's own structural argument (its real hypothesis needs a
  multi-asset-class universe the engine cannot yet mix with crypto in one run).
  Untouched here.
- **No CLI path computes a paired-bootstrap win rate against a benchmark for a
  `--folds` walk-forward's OOS winner** — the third of this research line's
  three shared kill criteria was, for both candidates here, moot (already
  killed on the other two bars) rather than unavailable by choice; a future
  session testing a candidate that clears the first two bars would need either
  a new tool or a manual reconstruction (re-run each fold's specific OOS winner
  combo through a plain `backtest --benchmark BTC/USD --bootstrap` over that
  fold's OOS span, one combo per fold since the winner differs by fold) to
  finish evaluating that third bar. Not built here — no candidate needed it.

## Session accounting

- Both walk-forward commands completed without error, without retry, and
  without hitting any rate limit or resource-related failure, despite elevated
  swap and load readings (see the resource-discipline table above).
- Two new entries were appended to `research/crypto_research_ledger.jsonl`
  (`sweep --folds` / `sma_crossover`, 27 trials; `sweep --folds` / `momentum`,
  24 trials), bringing the file to 8 total entries (6 from stage 1, 2 from this
  session).
- `git status` at the time of writing this document shows a clean working tree
  aside from this document, the ledger file's two new lines, and the branch's
  own commits — no orphaned files, no half-written state. No process was left
  running; no `trading paper` command was ever invoked, matching the
  assignment's explicit instruction.
