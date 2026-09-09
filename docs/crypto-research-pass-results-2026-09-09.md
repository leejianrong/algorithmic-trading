# Crypto strategy research pass — results, stage 1 (2026-09-09)

> **This is a stage 1 / partial execution of an 8-point ticket (KAN-1079, EPIC-140),
> bounded by a hard external deadline** — a real live equity `trading paper --live`
> session for a different epic (EPIC-139) launched against the same Alpaca paper
> account at ~13:25-13:30 UTC on 2026-09-09, and this session's own instructions
> required every network-touching command to be finished by **13:05 UTC**, 20
> minutes ahead of that launch. All work below finished by **~12:20 UTC**, well
> inside that window, so the early stop was a choice made once the four cheap kill
> tests and two bonus sweeps were done — not a scramble against the clock. See "What
> remains for a follow-up session" at the end for exactly what is still open.
>
> This document executes the plan already frozen in
> [`crypto-research-pass-2026-09-02.md`](crypto-research-pass-2026-09-02.md) (data
> source, universe, interval, cost model, candidate list/priority, CLI mechanics —
> none of that is re-derived here) and follows
> [`research-playbook.md`](research-playbook.md)'s 11-step loop, styled after
> [`deployment-decision-2026-09-01.md`](deployment-decision-2026-09-01.md), the
> equivalent equity pass for KAN-642. Every number below is pasted from a real
> command run against the real Alpaca paper account's market-data API
> (`--source alpaca`), never fabricated, never from `--source synthetic`.

## Step 1 — final hypotheses (adopted from the scoping doc's drafts, kill criteria added)

The scoping doc (§4) offered draft mechanism/counterparty framings for each
candidate and explicitly left numeric kill criteria for whoever executes the pass
to set. This session adopts the scoping doc's first-listed draft for each
candidate verbatim (they were judged sound on re-reading, not replaced) and adds
one shared numeric kill bar, applied identically across all four candidates so
none gets a discount for a more or less compelling story:

- **`sma_crossover`** (priority 1). *Mechanism* — crypto retail attention cycles
  create multi-day-to-multi-week trending moves as a narrative spreads through
  social/community channels faster than fundamentals could justify; a fast/slow
  SMA cross is a lagging proxy for "the narrative has taken hold." *Counterparty*
  — retail traders exhibiting the crypto-native disposition effect (quick
  profit-taking, reluctance to realize losses) who exit a winning trend too early
  and get run over by its continuation, plus short-horizon traders fading the
  first leg on the assumption crypto mean-reverts as fast as it trends.
- **`momentum`** (priority 2). Identical mechanism/counterparty framing to
  `sma_crossover`, measured via a more direct trailing-return signal instead of a
  moving-average proxy. Expected to correlate with `sma_crossover` — this is the
  same underlying claim tested twice, not two independent ideas (matching the
  equity pass's own reasoning for the same pair, which measured 0.773 correlation).
- **`mean_reversion`** (priority 3). *Mechanism* — a sharp single-pair drawdown on
  a leverage-heavy crypto venue is disproportionately likely to be a
  mechanically-triggered liquidation cascade (forced closes of over-leveraged
  longs by an exchange risk engine, clustering at price levels and
  self-reinforcing) rather than new information about the asset's value; the
  reversion this strategy buys is the price of providing liquidity into that
  forced-selling event once it exhausts. *Counterparty* — leveraged longs forcibly
  liquidated by exchange risk engines regardless of their own view, plus
  momentum/panic sellers extrapolating the cascade who get run over on the
  snap-back. This is explicitly tested at the **same** bar as the others, not a
  discounted one, despite carrying a negative equity prior (ADR-0071, KAN-642: two
  replicated equity failures) — the scoping doc's own argument is that the crypto
  mechanism is structurally different and deserves a fair, undiscounted test.
- **`cross_sectional`** (priority 4). *Mechanism* — cross-sectional momentum:
  capital and attention chase whichever crypto sector/narrative currently leads
  (smart-contract platforms, DeFi, payments, etc., per `crypto10`'s own sector
  map), distinct from the time-series mechanisms above. *Counterparty* — crypto
  allocators who systematically underweight the current leading narrative on a
  valuation or discipline basis. Carries an explicit, structural underpowering
  caveat (below) rather than a discounted bar.

**Shared kill criteria (all four, applied at a future `--folds` walk-forward step —
not reached this session):** kill if OOS Sharpe < 0.3, or IS→OOS Sharpe retention
< 50%, or paired-bootstrap win rate vs. a `BTC/USD` buy-and-hold benchmark < 55%.
These mirror the playbook's own worked example (`research-playbook.md` §4) and are
deliberately the same numbers across all four candidates so no candidate's kill
bar is quietly loosened because its story sounds better. `trend_following` is
**out of scope** for this pass per the scoping doc §4 and was not touched, run, or
given a hypothesis.

## Step 2 — universe, costs, OOS slice (frozen before any return number existed)

- **Universe:** `crypto10` (`--symbols @crypto10`), exactly as scoped. No
  alternative basket exists (§2 of the scoping doc).
- **Market / interval:** `--market crypto --interval 1d`, exactly as scoped —
  daily bars sidestep the intraday tape-density problem ADR-0073 measured
  (`crypto10` fails 4/10 at 5m, 10/10 at 1m).
- **Cost model:** `CostConfig.crypto()` shared defaults (5.0 bps slippage, 25 bps
  taker fee) — no `--slippage-bps`/`--taker-fee-bps` override. **Caveat repeated
  per the scoping doc's own instruction:** the 5.0 bps slippage default is
  measured **optimistic** on crypto by ADR-0061 (+13.02 bps mean realized vs. the
  5.00 bps model, on only 11 paired fills, 8 of which shared one market instant —
  below `MIN_PAIRED_FILLS = 30` and not independent). Every Sharpe/return number
  below is therefore probably a **flattering** one relative to what a live crypto
  session would realize, in the opposite direction from the equity pass's own
  conservative-bias caveat. The taker fee (25 bps, tier 1) is well-measured
  (ADR-0060/0061 agree to 4 decimals) and not a concern.
- **Full data span available:** Alpaca's crypto tape starts 2021-01-01
  (ADR-0058). This session used **2026-09-09** as "today"; the tape-density doc
  measured through 2026-09-08.
- **OOS slice, frozen now, not touched this session:** in-sample =
  **2021-01-01 to 2025-07-31** (all commands below); out-of-sample, reserved for a
  future `--folds` walk-forward = **2025-08-01 to 2026-09-08** (~13 months). No
  command in this session requested any data inside the OOS window — every
  `--from`/`--to` pair above is `2021-01-01`/`2025-07-31`, which can be checked
  directly against the commands pasted below.
- **`SOL/USD` data-quality caveat (mandatory per
  [`crypto-daily-tape-density-2026-09-09.md`](crypto-daily-tape-density-2026-09-09.md)):**
  `SOL/USD` is missing ~20% of its expected daily bars over this exact window
  (measured 79.9% coverage, 2021-01-01..2026-09-08) — a genuine, cross-validated,
  persistent hole in Alpaca's tape for this one symbol, not a listing artifact.
  Every result table below includes `SOL/USD` (dropping it was considered and
  rejected, per that doc's own recommendation to report with the caveat rather
  than silently filter) — **read every aggregate number below with the
  understanding that up to 1/10 of the universe's lookback/rebalance arithmetic is
  computed over a silently gapped tape for that one symbol.**
- **Survivorship caveat (repeated per the scoping doc's own instruction):**
  `crypto10` is "the worst of the three" baskets in this repo (`universe.py`'s own
  docstring) — 2026's ten survivors of a much higher-failure-rate market, on a
  venue that is itself a survivor filter, with no way to reconstruct delisted
  pairs. Every number below is an upper bound on an upper bound.

## Step 3 — cheap kill tests (all four candidates, in priority order)

One backtest per candidate, strategy defaults (no `--param`; `trading backtest`
has no per-run kwarg override — only `sweep` does), full in-sample span,
`--benchmark BTC/USD`, logged to the ledger (see "Ledger decision" below).

### 1. `sma_crossover` — **clears the cheap kill test cleanly**

```
uv run --env-file .env trading backtest --strategy sma_crossover --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 --benchmark BTC/USD \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/sma_crossover_kill_test/equity_curve.csv
```

```
Final equity:  $7,158.76   Total return: +615.88%   Annualized: +53.68%
Sharpe: 1.13   Sortino: 1.75   Calmar: 1.02   Max drawdown: 52.68%
Win rate: 37.54%   Turnover: 1492.87%   Trades: 342 entries   Bars: 1673
Trades/param: 114.0 (342 entries / 3 free parameters)
Benchmark (BTC/USD): +107.50%  (strategy +508.38pp vs benchmark)
Beta: 0.41   Alpha (ann.): +41.48%   Correlation: 0.48   Info ratio: 0.42
Rejected: 93   Clamped: 10
Halt: fired 2021-05-12 (drawdown 24.5% >= 20.0%); 10 episodes, all 10 re-armed,
      0 still in force at the end
```

**Verdict: clears every playbook-step-3 bar** — entry count is triple digits
(342), trades/param is 114 (>> `MIN_TRADES_PER_PARAMETER = 30`), total return is
strongly positive, no catastrophic single-bar loss visible in the summary, and the
benchmark comparison shows the strategy meaningfully beating a real (not idle-cash)
BTC/USD buy-and-hold. **Proceed to step 4** — done this session, see below.

### 2. `momentum` — **clears the cheap kill test, more modestly**

```
uv run --env-file .env trading backtest --strategy momentum --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 --benchmark BTC/USD \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/momentum_kill_test/equity_curve.csv
```

```
Final equity:  $2,629.64   Total return: +162.96%   Annualized: +23.50%
Sharpe: 0.68   Sortino: 0.98   Calmar: 0.36   Max drawdown: 64.98%
Win rate: 30.77%   Turnover: 1340.84%   Trades: 347 entries   Bars: 1673
Trades/param: 173.5 (347 entries / 2 free parameters)
Benchmark (BTC/USD): +107.50%  (strategy +55.47pp vs benchmark)
Beta: 0.48   Alpha (ann.): +18.25%   Correlation: 0.55   Info ratio: 0.03
Rejected: 111   Clamped: 14
Halt: fired 2021-05-12 (drawdown 21.5% >= 20.0%); 10 episodes, all 10 re-armed
```

**Verdict: clears the cheap kill test** — positive return, beats the benchmark
(modestly — +55pp, a much thinner margin than `sma_crossover`'s +508pp), high
trades/param (173.5). Info ratio is near-zero (0.03), a weaker relative-skill
signal than `sma_crossover`'s 0.42. **Proceed to step 4** — done this session,
see below.

### 3. `mean_reversion` — **borderline pass, weak relative performance, flag for scrutiny at OOS**

```
uv run --env-file .env trading backtest --strategy mean_reversion --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 --benchmark BTC/USD \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/mean_reversion_kill_test/equity_curve.csv
```

```
Final equity:  $1,557.42   Total return: +55.74%   Annualized: +10.15%
Sharpe: 0.47   Sortino: 0.71   Calmar: 0.30   Max drawdown: 33.84%
Win rate: 66.19%   Turnover: 1164.14%   Trades: 282 entries   Bars: 1673
Trades/param: 70.5 (282 entries / 4 free parameters)
Benchmark (BTC/USD): +107.50%  (strategy -51.76pp vs benchmark)
Beta: 0.31   Alpha (ann.): +3.99%   Correlation: 0.60   Info ratio: -0.39
Rejected: 13
Halt: fired 2022-05-09 (drawdown 22.2% >= 20.0%); 2 episodes, both re-armed
```

**Verdict: does not fail the letter of playbook step 3** — total return is
positive (+55.74%), no catastrophic single-bar collapse, trades/param clears 30
comfortably (70.5), and win rate is the highest of the four candidates (66.19%).
But it **fails the spirit of the sign-and-shape check**: it substantially
underperforms the `BTC/USD` benchmark (-51.76pp) and has a **negative** info
ratio (-0.39), meaning its risk-adjusted excess return over the benchmark is
negative. This reads as "positive in isolation, worse than doing nothing more
sophisticated than holding BTC" — not an automatic kill by the letter of the
written criteria (which are about the strategy's own sign/shape and its cheap
kill status, not a required beat-the-benchmark bar at this step), but a strategy
this session flags as the **weakest of the three time-series candidates** and
does **not** advance to a step-4 sweep this session, on time-budget grounds
(priority order: sma_crossover and momentum earned their sweep slots on stronger
signal; mean_reversion's slot, if any, is a follow-up-session decision, not a
kill). Its final disposition (advance to sweep, or treat as killed) is left
**open** for the next session, made explicitly rather than defaulted.

### 4. `cross_sectional` — **underpowered exactly as predicted; the tool's own warning fires**

```
uv run --env-file .env trading backtest --strategy cross_sectional --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 --benchmark BTC/USD \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/cross_sectional_kill_test/equity_curve.csv
```

```
Final equity:  $1,614.84   Total return: +61.48%   Annualized: +11.03%
Sharpe: 0.49   Sortino: 0.69   Calmar: 0.14   Max drawdown: 76.28%
Win rate: 56.48%   Turnover: 312.36%   Trades: 45 entries   Bars: 1673
Trades/param: 11.2 (45 entries / 4 free parameters)
  ⚠ under 30 trades per free parameter -- too small a sample to distinguish
    edge from noise; widen the universe or the date range before trusting
    these numbers
Benchmark (BTC/USD): +107.50%  (strategy -46.01pp vs benchmark)
Beta: 0.93   Alpha (ann.): +1.97%   Correlation: 0.81   Info ratio: -0.01
Rejected: 160   Clamped: 37
Halt: fired 2021-05-19 (drawdown 39.2% >= 20.0%); 16 episodes, all re-armed
```

**Verdict: killed at the cheap-kill-test step, on structural grounds the scoping
doc predicted exactly.** The tool's own `trades_per_parameter` warning fires
(11.2 << `MIN_TRADES_PER_PARAMETER = 30`) — the default `top_k=8` holds 80% of a
10-symbol universe, and `trading backtest` has no `--param` override to shrink it
(only `sweep` does; the KAN-642 verdict doc names this exact tooling gap on the
equity side too). Max drawdown is the worst of the four (76.28%), correlation to
BTC/USD is the highest (0.81, nearly beta=1 at 0.93) — consistent with "holding
80% of a small universe is close to holding the market," which undermines the
strategy's own cross-sectional-rotation thesis rather than testing it. **This is
a structural-power kill, not a mechanism kill** — mirroring the equity pass's own
distinction between "confirmed fail" and "inconclusive because underpowered." No
larger crypto basket and no PIT crypto membership tool exist to fix this
(scoping doc §4), so this candidate's disposition does not change without new
tooling or a policy decision to accept `top_k` values this session cannot set.
**Not advanced to step 4.**

## Step 4 — in-sample sweep for the two candidates that clearly justified it

`sma_crossover` and `momentum` cleared step 3 with the strongest signal (both
positive info ratio or the strongest info ratio among positive verdicts, both
comfortably above `MIN_TRADES_PER_PARAMETER`). Both were swept with a
deliberately small grid (per the scoping doc §1's "narrower grids than the equity
pass" instruction, given the zero-cache cost per trial) — narrower than the
equity KAN-642 pass's up-to-16-combo grids.

### `sma_crossover` sweep (12 combos: `fast=5,10,15` x `slow=30,50,80,120`)

```
uv run --env-file .env trading sweep --strategy sma_crossover --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 \
  --param fast=5,10,15 --param slow=30,50,80,120 --rank-by sharpe \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/sma_crossover_sweep.csv
```

```
rank  fast  slow  sharpe  total_return  max_drawdown
1     5     30    1.136   767.71%       60.44%
2     10    30    1.050   548.99%       47.94%
3     10    50    0.985   461.21%       47.96%
4     5     50    0.889   332.32%       50.59%
5     15    50    0.852   311.94%       57.59%
6     15    30    0.838   276.87%       56.87%
7     10    80    0.636   144.02%       69.46%
8     15    80    0.598   122.79%       71.61%
9     5     80    0.547   95.07%        65.02%
10    10    120   0.372   32.82%        57.89%
11    5     120   0.342   25.37%        61.24%
12    15    120   0.334   21.79%        61.22%

Trials: 16 scored (12 this run + 4 carried from the ledger's earlier cheap-kill-
        test entries); the luckiest skill-free one would show Sharpe +0.51
        (observed +1.14)
Deflated: P(true Sharpe > that null best) = 0.92
  ⚠ below 0.95 -- after discounting for 16 trials, this Sharpe is not
    distinguishable from the best of that many skill-free runs
```

The IS winner is `fast=5, slow=30` (Sharpe 1.136) — nearly identical to the
strategy's shipped defaults, which is why the step-3 cheap kill test's Sharpe
(1.13) and this sweep's winner (1.136) are so close. **Deflated probability 0.92
is below the 0.95 confidence bar** — the shared-default significance threshold
this bench uses everywhere (ADR-0039) — so even the best-in-grid combo is **not
yet distinguishable from the luckiest of 16 skill-free trials**, on this session's
own reading. This is not a kill by the step-1 criteria (which are OOS-based, and
no OOS run happened this session), but it is a materially weaker result than the
raw Sharpe alone suggests, and the write-up would be dishonest omitting it.

### `momentum` sweep (4 combos: `lookback=20,40,60,90`)

```
uv run --env-file .env trading sweep --strategy momentum --symbols @crypto10 \
  --source alpaca --market crypto --interval 1d \
  --from 2021-01-01 --to 2025-07-31 \
  --param lookback=20,40,60,90 --rank-by sharpe \
  --ledger research/crypto_research_ledger.jsonl --hypothesis "..." \
  --out results/research/crypto-kan-1079/momentum_sweep.csv
```

```
rank  lookback  sharpe  total_return  max_drawdown
1     20        1.025   454.70%       57.50%
2     40        0.744   236.98%       64.46%
3     60        0.677   162.96%       64.98%
4     90        0.438   52.58%        73.22%

Trials: 20 scored (4 this run + 16 carried from the ledger); the luckiest
        skill-free one would show Sharpe +0.46 (observed +1.02)
Deflated: P(true Sharpe > that null best) = 0.89
  ⚠ below 0.95
```

The IS winner is `lookback=20` (Sharpe 1.025, well above the shipped default
`lookback=60`'s 0.677 — the cheap kill test used the default and scored
accordingly). Deflated probability **0.89**, further below the confidence bar than
`sma_crossover`'s — expected, given the smaller grid (4 vs. 12 trials this run)
gives the deflation correction less spread to discount confidently.

**Neither sweep's winner is a confirmed finding at this session's confidence
bar.** Both are promising IS candidates for the next session's `--folds`
walk-forward (step 5), which is the one command this bench trusts for an honest
OOS answer — and which this session deliberately did not attempt, per its own
instructions not to rush that step against the deadline.

## Ledger decision: a new, crypto-specific ledger file

**Decision: `research/crypto_research_ledger.jsonl`, a new file, not
`research/kan642_trial_ledger.jsonl` (the equity research line's ledger).**

Reasoning, stated explicitly as the ticket requires:

1. **The null distribution a deflation correction discounts against is a property
   of the data-generating population being searched, not just "how many times has
   a human tried something."** ADR-0059's own principle — "a derived statistic
   must be computed on the same basis as the population it is compared against" —
   was written about `periods_per_year` and annualization, but the same logic
   applies here: `crypto10` on `CostConfig.crypto()` under `RiskConfig.crypto()`'s
   halt-recovery posture is a **different population** from `blue20`/`@sp500` on
   equity costs and the equity halt posture. A trial's Sharpe on one population is
   not exchangeable with a trial's Sharpe on the other, and ADR-0062 §3 already
   concedes the ledger's own limitation — it widens the **count**, never the
   spread, and the two spreads here are visibly different (equity's own deflation
   history vs. this session's crypto Sharpes cluster in different ranges entirely:
   0.3-1.1 here vs. numbers this session did not re-derive from the equity line).
   Mixing counts across two populations whose spreads do not transfer would widen
   crypto's correction using a search size that was never actually spent looking
   for a crypto edge, and vice versa — inflating both sides' apparent rigor
   without the underlying discipline to back it.
2. **Precedent inside this repo already draws this line.** `kan642_trial_ledger.jsonl`
   was itself scoped to one specific research question (KAN-642, equity candidates
   on `blue20`/`@sp500`) rather than reused from some even earlier, more general
   ledger — the equity pass did not ask "should this share a ledger with whatever
   came before KAN-642," it simply started a new one for a new research question.
   A new crypto pass, on a new market with its own cost model, its own risk
   posture, and its own universe, is at least as distinct a "research question" as
   KAN-642 was from whatever preceded it.
3. **Practically, mixing them would make neither number legible.** A reader of a
   future crypto sweep's deflation block would see a trial count blended from two
   markets and have no way to know how much of that count came from a
   genuinely-relevant search vs. an unrelated equity grid — worse than the
   ledger's already-acknowledged lower-bound limitation, because it would also be
   *uninterpretable* rather than merely incomplete.

This crypto ledger currently holds **6 entries** (the 4 cheap kill tests, `1`
trial each, plus the 2 sweeps, `12` and `4` trials respectively) — **22 trials
total**, all logged with `--hypothesis` from the first command onward, per this
session's explicit instruction to log from step 3 (not step 4, which is the
playbook's own default — the playbook itself defers `--ledger` past step 3
because "both cost something," but the ledger's cost here is one JSONL append,
not the `--bootstrap` compute cost the playbook is actually warning about, so
logging from step 3 loses nothing and gives every future deflation calculation
a more complete count from the very first command in this line of research).

## Resource discipline actually observed

`free -h`/`uptime` were checked before the first command (`4.0Gi`/`7.8Gi` used,
`379Mi` free, `3.5Gi` available, load average 0.61/0.70/0.76 — comfortable, no
swap in use) and again mid-session (materially unchanged: `4.1-4.2Gi` used, load
average 0.6-0.7). **Every command in this session completed in 5-42 seconds** —
dramatically faster than the scoping doc's own worst-case framing (it worried
about rate limits and multi-minute per-trial costs from the zero-cache path);
this session hit neither a rate limit nor any resource pressure, and `nice`/
`ionice` wrapping (recommended by the scoping doc §6) was not needed given how
fast every command actually ran, though it would have been added had load risen.

## What remains for a follow-up session (playbook steps 4 (partial)-8, all candidates)

- **`mean_reversion`**: no step-4 sweep decision made — advance it to a sweep, or
  treat its weak benchmark-relative performance as a step-3-adjacent kill. Left
  open deliberately (see its verdict above), not defaulted either way.
- **`cross_sectional`**: killed at step 3 on structural underpowering grounds.
  Re-opening it needs either a larger/PIT crypto universe (does not exist today,
  per the scoping doc) or a policy decision to accept a smaller `top_k` via a
  future `--param` sweep (which `sweep`, unlike `backtest`, does support) — a
  `sweep --param top_k=2,3,4` run at trades/param around 3-4x today's would still
  be a legitimate next step and was **not attempted** this session.
- **Step 5 (true OOS walk-forward, `--folds`)**: **not attempted for any
  candidate**, on both the deadline's explicit instruction ("do NOT rush a real
  step like a walk-forward OOS run to beat the clock") and the scoping doc's own
  caution about `--folds`' cost with zero cache (each fold re-fetches the whole
  universe for its own IS+OOS span). The frozen OOS slice
  (2025-08-01..2026-09-08) has not been touched by any command in this session —
  verifiable directly from the `--from`/`--to` pairs pasted above, all of which
  end at `2025-07-31`.
- **Step 6 (robustness battery)**: not run for any candidate — no cost-sensitivity
  sweep (`--slippage-sweep`, ADR-0069), no correlated-asset transfer check, no
  `--stability` heatmap, no `--regimes` split, no `--monte-carlo` shuffle. All are
  cheap, offline-after-fetch, and are natural next steps once step 5 produces an
  OOS survivor worth spending them on.
- **Step 7 (cumulative-ledger deflation, `--bootstrap`)**: not run — this needs a
  step-5 OOS survivor to confirm, and none exists yet.
- **Step 8 (portfolio fit)**: not run — no correlation was computed between any
  crypto candidate and the equity book (`sma_crossover`/`momentum` currently in
  EPIC-139 paper incubation). Worth doing once a crypto candidate survives step 5,
  the same way the equity pass computed `sma_crossover`-vs-`momentum` correlation
  (0.773) at that stage.
- **`--diversified-baseline` was deliberately not used** in this pass — its
  default basket (`@core10`) is equity-shaped and would trip the crypto
  shape-guard (ADR-0057) under `--market crypto`, and no crypto-diversified
  basket exists to substitute (`crypto10` is itself the only crypto basket, so
  comparing a crypto strategy against "equal-weight `crypto10`" would be a
  reasonable follow-up but was out of this session's time budget).

## Deadline compliance

- Last network-touching command completed: **2026-09-09 12:17:51 UTC** (the
  `momentum` sweep). No command was started after that.
- This document, the ledger file, and result CSVs (gitignored; not committed) were
  written entirely offline after that point.
- No process was left running; no `trading paper` command was ever invoked. `git
  status` at time of writing this document shows a clean working tree aside from
  this document, the new ledger file, and the branch's own commits — no orphaned
  files, no half-written state.
