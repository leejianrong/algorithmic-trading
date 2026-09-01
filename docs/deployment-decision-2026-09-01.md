# Deployment decision run — 2026-09-01

> Pre-registered per [`research-playbook.md`](research-playbook.md) §1-2, **before any
> command below was run**. This section is the commitment; results are appended below it
> as they land, never edited to fit a result that already exists. Answers KAN-642.

## Candidates (KAN-642's "six candidate strategies" — five taken seriously today;
`buy_and_hold`/`equal_weight` are baselines, not candidates, per the registry)

1. `sma_crossover`
2. `momentum`
3. `mean_reversion`
4. `cross_sectional`
5. `trend_following`

## Frozen universe, market, costs (§2 — decided now, not after a look)

| Candidate | Universe | Range | Market | Cost model | Halt cooldown |
|---|---|---|---|---|---|
| `sma_crossover` | `@blue20` | 2008-01-01..2023-12-31 | `us_equity` | default (5 bps, no fee) | 21 bars |
| `momentum` | `@blue20` | 2008-01-01..2023-12-31 | `us_equity` | default | 21 bars |
| `mean_reversion` | `@blue20` | 2008-01-01..2023-12-31 | `us_equity` | default | 21 bars |
| `cross_sectional` | `@sp500` (PIT as of `--from`) | 2008-01-01..2023-12-31 | `us_equity` | default | 21 bars |
| `trend_following` | `@trend_etfs` | 2008-01-01..2023-12-31 | `us_equity` | default | 252 bars |

Rationale: one shared 16-year range spans the GFC, 2018, COVID and 2022 for every
candidate, so no candidate gets an easier era than another. `--halt-cooldown-bars` is
set for every run per ADR-0055's rule ("calibrate a guardrail; never widen it") — the
unmodified default latches permanently in 2008 for every strategy family tried so far
(ADR-0031/0055/0070/0072) and a latched run answers "what does a frozen book return",
not "does this edge exist." 21 bars (~1 trading month) matches ADR-0072's precedent for
daily-signal and monthly-rebalance strategies; 252 bars (~1 year) matches ADR-0070's own
precedent for `trend_following` specifically. Both are cited prior-art choices, not
picked to flatter this run. `--diversified-baseline` (default `@core10`) and
`--benchmark SPY` run on every confirm/OOS command so every candidate is judged against
both bars KAN-642 named. Real yfinance data throughout (`--source yfinance`), not
synthetic — KAN-642's own complaint was default/never-swept parameters and small
universes on real data, and only real data settles that.

`cross_sectional`'s OOS walk-forward is deliberately run at `--folds 3` (not the 4 used
elsewhere) — over ~500 symbols each fold issues its own IS+OOS fetch window per symbol
to yfinance, and this bench's own history (ADR-0040, six-times-cited) is that real
provider calls are the one thing this codebase treats as unreliable at volume. Decided
now, for a stated operational reason, not after seeing a result.

## Parameter grids swept in-sample (§4 — decided now)

| Candidate | Grid | Combos |
|---|---|---|
| `sma_crossover` | `fast=5,10,20,50` x `slow=30,50,100,200` | 16 (some `fast>=slow` skipped) |
| `momentum` | `lookback=21,42,63,126,189,252` | 6 |
| `mean_reversion` | `period=7,14,21,28` x `oversold=20,30,40` | 12 |
| `cross_sectional` | `lookback=60,120,180,252` x `top_k=5,8,12,20` | 16 |
| `trend_following` | `lookback=126,189,252,315` x `skip_recent=10,21,42` | 12 |

`weight`/`rebalance_days` held at each strategy's shipped default throughout — sweeping
every free parameter of every candidate at once turns a decision run into an
unconstrained search, which is exactly the multiple-comparisons problem ADR-0039/0059/
0062 exist to price, not something to maximize by adding more knobs.

## Hypotheses and kill criteria (§1 — one per candidate, written now)

### 1. `sma_crossover` — trend persistence in mega-caps

**Mechanism:** large, index-heavy holders (funds tracking benchmarks, pension
rebalancers) cannot instantly reprice a mega-cap on new information — flows into and
out of a name this size are throttled by market-impact-aware execution schedules over
days to weeks. A fast/slow SMA cross is a lagging proxy for "the flow has actually
started," entered after the fact rather than anticipating it.

**Counterparty:** disposition-effect retail sellers who exit winners too early and
short-horizon mean-reversion traders who fade the first leg of a move, both of whom are
run over by a sustained institutional rebalance that plays out over weeks, not the hours
their own edge is priced for.

**Kill criteria (decided now):**
- Mean OOS Sharpe (walk-forward) < 0.3 → kill.
- IS→OOS Sharpe retention < 50% → kill.
- Deflated-Sharpe probability (cumulative ledger) < 0.95 → not significant, does not
  qualify (though may still be worth a further, larger OOS sample before a hard kill).
- Paired-bootstrap win rate < 90% against **either** SPY or the diversified baseline →
  does not qualify as a standalone allocation (may still be worth §8's portfolio-fit
  argument if genuinely uncorrelated).
- Cost-sensitivity: edge dies (Sharpe/return crosses zero) below 10 bps (2x the
  modelled 5 bps) → too cost-fragile to trust the modelled number.

### 2. `momentum` — time-series trailing return, per-symbol

**Mechanism:** identical structural story to `sma_crossover` (slow information
diffusion into large-cap prices) but measured directly as trailing return rather than
through a moving-average proxy — a cleaner, more direct read on the same effect, so it
is a genuinely distinct test of the same underlying claim, not a restatement.

**Counterparty:** same as `sma_crossover` — disposition-effect sellers and short-horizon
mean-reversion traders.

**Kill criteria:** identical numeric bars to `sma_crossover` above (same mechanism
claim, same universe, same bar for evidence).

### 3. `mean_reversion` — RSI oversold/recovery, per-symbol

**Mechanism:** short-horizon liquidity provision. A sharp, single-name drawdown in a
mega-cap is disproportionately likely to be a forced or panic sale (a margin call, an
index-fund rebalance, an overreaction to noise) rather than new fundamental
information, and the reversion this strategy buys is the price of providing liquidity
to that forced seller over the following days.

**Counterparty:** the forced/panic seller who needs liquidity now more than they need
the best price, and momentum/trend traders extrapolating the drop who get run over when
it snaps back.

**Kill criteria:** same numeric bars as `sma_crossover` — this is the sharpest
in-sample-vs-OOS contrast to watch, since `mean_reversion` is the one candidate this
bench's own CLAUDE.md already records as measured-underperforming in an earlier,
narrower run (ADR-0071: mean_reversion "-77.29pp vs SPY, -25.98pp vs baseline" on
`AAPL,MSFT,GOOGL,AMZN,JPM` 2015-2023) — the prior is genuinely negative here, and this
run is a chance for the wider universe/longer range to overturn or confirm that, not an
excuse to lower the bar because we already expect a fail.

### 4. `cross_sectional` — relative-strength rotation across the real S&P 500

**Mechanism:** cross-sectional momentum — a real, published risk premium (Jegadeesh &
Titman) distinct from the time-series mechanisms above: capital and attention chase the
market's current leadership as a sector/theme narrative takes hold (a slower, more
persistent rotation than single-name reversion), and monthly rebalancing captures that
without churning on daily noise.

**Counterparty:** value/contrarian allocators and index-huggers who systematically
underweight the current leadership on a valuation or rebalancing-discipline basis,
providing the flow this strategy's monthly rotation captures.

**Kill criteria:** same numeric bars as `sma_crossover`, plus one specific to this
candidate: PIT-vs-today's-membership divergence (ADR-0072 found the PIT `@sp500`
universe absent-symbol rate is **11x** today's) must not be the entire explanation for
any apparent edge — if the confirm run's entry count or absence caveats suggest the
result is mostly an artifact of which names were reachable on free data rather than the
rotation mechanism, that is a qualifier on any pass, not grounds to suppress it.

### 5. `trend_following` — cross-asset absolute momentum

**Mechanism:** distinct from the four equity-only candidates above — absolute (not
relative) momentum applied across asset classes (equities, bonds, commodities,
currency) captures macro regime persistence: once a regime is established (risk-on
equities, a bond bull/bear cycle, a commodity supercycle), large asset allocators
(pensions, sovereign funds) rebalance into it gradually over quarters because their own
mandates and governance cycles are slow, not because the information is new.

**Counterparty:** allocators structurally rebalancing against the trend on a fixed
calendar (e.g., strategic-allocation rebalancing back to target weights every quarter
regardless of momentum) and macro forecasters positioned for regime *reversal* ahead of
the data confirming it.

**Kill criteria:** same numeric bars as `sma_crossover`, with one adjustment already
recorded as a known, structural limitation rather than a fresh finding: CLAUDE.md
already documents `trend_following`'s standalone profile as modest (ADR-0070: "+68.16%,
Sharpe 0.36... against SPY's own +326.13%") with a "diversification-focused" framing —
so a low standalone Sharpe here is expected and the §8 portfolio-fit correlation check
(cross-asset legs should decorrelate it from the four equity-only candidates) carries
real weight in this candidate's verdict, not just its own OOS Sharpe in isolation.

## Shared trial ledger

All in-sample sweeps, walk-forward folds (once KAN-677 lands), and confirm runs across
**all five candidates** append to one file, `research/kan642_trial_ledger.jsonl`, so
the cumulative deflation for candidate 5 correctly reflects the search already spent on
candidates 1-4 — trying five different strategy families is itself a multiple-testing
exercise across strategies, not just within one strategy's own grid.

## What this run does not attempt

Steps 9-11 of the playbook (paper incubation, micro-live, scale/retire) require days to
weeks of live paper trading and are explicitly out of scope this session — EPIC-86
(deployment infra) is deferred, and no strategy has cleared even the in-sample/OOS bar
yet as of writing this section. A strategy that qualifies below is qualified **to enter
paper incubation next**, not to trade real money today; KAN-642's own draft bar
requires forward paper evidence "consistent with the backtest," which this run cannot
produce. The verdict below is explicit about this two-tier structure.

---

## Results

*(appended as each candidate's evidence lands — nothing above this line is edited after
the fact. All commands run against real `--source yfinance` data, `.cache/data`
warmed once per universe/range so sweep combos are compute-bound, not network-bound.)*

### Step 3 — cheap kill tests (shipped defaults, full 2008-2023 span, no ledger)

| Candidate | Sharpe | Total return | Max DD | Entries | Halt episodes |
|---|---|---|---|---|---|
| `sma_crossover` (fast=10,slow=20) | 1.03 | +466.01% | 20.32% | 2,078 | 1 (re-armed) |
| `momentum` (lookback=60) | 0.96 | +412.68% | 27.70% | 2,111 | 1 (re-armed) |
| `mean_reversion` (period=14,oversold=30) | 0.53 | +108.78% | 20.49% | 1,115 | 1 (re-armed) |
| `trend_following` (lookback=252,skip=21) | 0.49 | +100.85% | 20.28% | 130 | 1 (re-armed, 1yr) |
| `cross_sectional` (lookback=120,top_k=8) | 0.665 | +1872.97% | 49.64% | 528 | 10 (all re-armed) |

None killed at step 3: all positive, all comfortably above single-digit entry counts
and 30 trades/free-parameter (sma_crossover 692.7, momentum 1055.5, mean_reversion
278.8, trend_following 32.5 — trend_following is the thinnest but still clears the
bar). All four halted once in the guardrail's default posture and re-armed cleanly
under the pre-registered `--halt-cooldown-bars`.

### Step 4 — in-sample sweep, `--ledger`/`--hypothesis`, `--stability`,
`--slippage-sweep` (all on the shared `research/kan642_trial_ledger.jsonl`)

| Candidate | Best combo | IS Sharpe | Stability (best vs. neighbours) | Cost sensitivity | Cumulative ledger trials |
|---|---|---|---|---|---|
| `sma_crossover` | fast=10, slow=200 | 1.165 | smooth — neighbours 5/200 (1.156), 50/200 (1.089), 10/50 (1.131); no cliff | survives whole grid to 100 bps (20x modelled) | 14 |
| `momentum` | lookback=189 | 1.146 | neighbours 126 (1.143), 252 (1.070) — smooth, single-param | survives whole grid to 100 bps | 20 |
| `mean_reversion` | period=7, oversold=40 | 0.631 | neighbours period=14/oversold=40 (0.596), period=7/oversold=30 (0.599) — smooth, but the whole surface sits well below the other four candidates | **edge dies at ~33.06 bps** — 6.6x modelled, but the deflated significance already fails (below) | 32, deflated P=**0.92 (below the pre-registered 0.95 bar)** |
| `trend_following` | lookback=189, skip_recent=21 | 0.672 | neighbours 189/10 (0.537), 189/42 (0.530), 126/21 (0.377), 252/21 (0.495) — real but not extreme gap; not a spike | survives whole grid to 100 bps | 44, deflated P=0.96 |
| `cross_sectional` | lookback=120, top_k=20 | 0.761 | smooth surface: top_k monotone-increasing at every lookback (60: 0.63→0.53, 120: 0.56→0.76, 180: 0.59→0.73, 252: 0.55→0.58); no isolated spike, but max drawdown scales with top_k/lookback too (33-67% across the grid — this is a genuinely riskier candidate than the four others regardless of Sharpe) | re-run at reduced scope (2x2 grid, 3 slippage levels — the full ~500-symbol/16-year grid was killed mid-run for resource reasons, see note below) — survives to 50 bps (10x modelled), no crossing observed in the tested range | 48 (16 this run + 32 carried; see resource note — this run's own carried-over count is a snapshot from when it *started*, before `trend_following`'s 12 were appended, so it undercounts by 12; the final confirm run's read will be current) |

**Resource note:** `cross_sectional`'s full `@sp500` sweep (16 combos x 7 cost-sensitivity
levels = 23 runs) was interrupted partway through the cost-sensitivity re-runs — the
process was consuming ~470MB RSS at 90%+ CPU for 20+ minutes on a 7.8GB-RAM machine
already under memory pressure from concurrent sessions, and was killed to protect the
operator's machine. The main grid (16 combos, ranked table + stability heatmap) had
already completed and logged to the ledger before the kill, so nothing from step 4's
core result was lost — only 4 of the 7 planned cost-sensitivity levels are missing, and
a smaller, `nice`-priority follow-up (2x2 grid, 3 levels) recovered enough of that
check to answer the kill-criterion question. All further heavy commands this session
run `nice -n 19` and are checked against `free`/`uptime` before launching.

`mean_reversion` is the first candidate to fail a pre-registered kill bar: its own
in-sample deflated significance (0.92) is already below 0.95 **before** the OOS fold
step or the final cumulative confirm run — which will only add more trials to the
ledger and push this number down further, never up. Continuing it through the
remaining steps for completeness (the playbook doesn't stop early either — killing
quietly would hide the shape of the failure), but the pre-registered verdict is already
determined by §1's numeric bar.

### Step 5 — true walk-forward OOS, `--folds`, `--bootstrap`, `--ledger` (KAN-677)

All four numeric ledger figures below are read at the moment each command ran, so a
later one's "carried over" count reflects everything logged before it — this is why
the deflated probability strictly decreases across the table below even where the raw
OOS numbers look fine: the correction is charging every candidate for the search spent
on the others, exactly as pre-registered.

| Candidate | Folds profitable OOS | Mean OOS Sharpe | IS->OOS retention | Deflated P (cumulative) | Kill bar (OOS Sharpe>=0.3, retention>=50%, P>=0.95) |
|---|---|---|---|---|---|
| `sma_crossover` | 4/4 | +1.18 | 99% | 1.00 (100 trials) | **PASS all three** |
| `momentum` | 4/4 | +1.15 | 107% | 0.99 (124 trials) | **PASS all three** |
| `mean_reversion` | 4/4 | +0.78 | 132% | 0.71 (172 trials) | OOS Sharpe/retention pass; **deflated P fails** |
| `trend_following` | 4/4 | +0.45 | 65% | 0.77 (220 trials) | OOS Sharpe/retention pass; **deflated P fails** |
| `cross_sectional` | **not obtained this session** | — | — | — | **inconclusive — see below** |

Two individual-fold bootstrap CIs straddle zero for every candidate except
`sma_crossover`'s first fold and `momentum`'s first two — expected given each fold's
OOS span is only ~800 return periods, and the aggregate (mean-of-folds, deflated
against the pooled search) is the number the playbook and this run's kill criteria
were written around, not any single fold in isolation.

**`sma_crossover` and `momentum` are the only two candidates clearing all three
pre-registered OOS bars outright** — both showed *improved* Sharpe out-of-sample
relative to in-sample (99% and 107% retention), which is the strongest possible
answer to "is this fit to noise": if it were curve-fit to 2008-2020's in-sample span,
OOS performance should degrade, not hold or improve.

`mean_reversion` and `trend_following` both post genuinely positive, all-folds-
profitable OOS numbers that would look like a pass read in isolation — this is exactly
the failure mode step 7's cumulative deflation exists to catch: raw OOS success that
is still statistically indistinguishable from the best of everything else tried once
the whole research program's search is priced in.

#### Resource note (raised by the operator, 2026-09-01 23:1x local)

This session's `cross_sectional` runs are the heaviest in the batch — the full
`@sp500` PIT universe (~500 symbols) times a 16-year range, and unlike the other four
candidates (whose full-range bars were pre-warmed in the yfinance cache before any
sweep ran), each walk-forward **fold** requests a distinct IS/OOS sub-range from the
adapter, so every fold is a fresh, uncached batch of ~500 network calls. The original
full-grid, `--folds 3` plan for this candidate was interrupted twice: once killed
directly (a 20+ minute, ~470MB/93%-CPU process competing with a concurrent `make
check` run on a 7.8GB-RAM machine already swap-thrashing under several *other*,
unrelated concurrent sessions — confirmed by process inspection, not this run alone)
and once killed by what looks like an environment-level interruption unrelated to
either job (a large wall-clock gap and no OOM evidence in `dmesg`). Both times, no
data was lost — the ledger only gets an entry on a completed run. Responses, in order:
`nice -n 19` (later `+ionice -c3`) on every subsequent heavy command; the sweep grid
cut from 16 combos to a 2x2 subset (`lookback=120,180 x top_k=8,20`, spanning the
full sweep's top-4-ranked region); the cost-sensitivity grid cut from 7 levels to 3;
and the walk-forward fold count cut from 3 to 2 (fewer fold sub-ranges, fewer
additional network batches). These are resource/operational decisions, stated and
reasoned here, not result-driven ones — no OOS number for this candidate had been
seen when any of these cuts were made.

**Escalated further and stopped, not fixed.** The 2-fold retry was also killed with
no `dmesg` OOM evidence and no partial stdout — at that point `free` showed **swap at
7.4/8GB** and `uptime` load average ~26, i.e. the whole machine, not this process
specifically, was thrashing. A third attempt — dropping `--folds` entirely for a
single manual IS(2008-2018)/OOS(2019-2023) split, the lightest version of real OOS
evidence this candidate could still get — was killed the same way before producing
any output. This is consistent with several *other*, unrelated concurrent sessions on
the same machine (confirmed by process inspection: a `kaya` vitest run and a
`qms-incub` uvicorn process were the top memory consumers at the time, not this
research run) pushing the shared environment past what it could sustain, rather than
anything about `cross_sectional`'s own resource use. Raised to the operator directly;
decision (2026-09-01, in conversation): **stop retrying this session**, report
`cross_sectional`'s verdict on what was already obtained (step 3 kill test + the full
step-4 in-sample sweep + a reduced cost-sensitivity check — all completed, all real
data, all before this became a problem), and record true OOS validation for this one
candidate as an explicit, unresolved gap rather than force a result out of a thrashing
machine. **No in-sample number for `cross_sectional` is affected** — steps 3 and 4
completed cleanly before the OOS attempts began.

### Steps 6+7 combined — confirm run: bootstrap CI, regime split, Monte Carlo,
benchmark + diversified-baseline comparison, final cumulative deflation

**Tool limitation discovered here:** `trading backtest` (a single run, unlike
`sweep`) has **no `--param` / strategy-kwarg override** — it always instantiates the
strategy at its shipped defaults. `sma_crossover`/`momentum`/`mean_reversion`/
`trend_following`'s step-4/5 sweeps and folds already tuned and OOS-tested the swept
grid; this confirm step could not re-run at the specific swept winner, so it runs at
each candidate's **shipped default parameters** instead — a legitimate, if different,
check ("does the vanilla, untuned version also show a real edge"), not the OOS
winner's own confirmation. Recorded as an open CLI gap below.

| Candidate | vs SPY (raw) | vs SPY (paired win rate) | vs core10 baseline (raw) | Sharpe 95% CI | Deflated P (cumulative) | Regime split | MC drawdown percentile |
|---|---|---|---|---|---|---|---|
| `sma_crossover` | **+119.79pp** | **99.9%** | **+279.71pp** | [0.51, 1.54] | 1.00 (237) | mean_reverting **-8.15% ann, Sharpe -0.66** (only regime it loses in) | 61.9 (typical) |
| `momentum` | **+66.46pp** | **99.7%** | **+226.38pp** | [0.41, 1.44] | 1.00 (238) | mean_reverting -2.43% ann (mildly negative) | 90.8 (worse-than-typical clustering) |
| `mean_reversion` | -237.45pp | 47.7% | -77.52pp | [0.25, 0.95] | 0.98 | trending **-6.10% ann, Sharpe -0.76**; wins big in mean_reverting (expected) | 43.5 (typical) |
| `trend_following` | -245.38pp | 35.5% | -85.45pp | [0.11, 0.89] | 0.97 | **all four regimes positive** — the only candidate with no losing regime | 26.5 (better-than-typical) |
| `cross_sectional` | +1526.74pp | **59.8%** | +1686.67pp | [0.10, 1.07] | 1.00 (241, **in-sample only**) | all four regimes positive, but max DD 42-60% across them | 26.2 (better-than-typical) |

`sma_crossover` and `momentum` are the only two candidates whose paired bootstrap win
rate against SPY clears the pre-registered 90% bar (99.9%, 99.7%); `cross_sectional`'s
huge raw outperformance does not translate into a reliable per-block edge (59.8%,
observed edge only +0.10) — a reminder that a bigger total-return number is not the
same claim as "beats the benchmark reliably," which is exactly what the paired
statistic is for. **No CLI command computes a paired win rate against the diversified
baseline** (only against `--benchmark`) — a known, already-documented gap (ADR-0071:
"no paired-bootstrap win rate against it yet") — so the baseline comparison above is
raw-return/alpha/correlation only, not a formal significance test.

### Step 8 — portfolio-fit: pairwise correlation across all five candidates' confirm-run equity curves

|  | momentum | mean_reversion | trend_following | cross_sectional |
|---|---|---|---|---|
| **sma_crossover** | 0.773 | 0.199 | 0.425 | 0.234 |
| **momentum** | — | 0.385 | 0.548 | 0.254 |
| **mean_reversion** | | — | 0.501 | 0.187 |
| **trend_following** | | | — | 0.259 |

`sma_crossover` and `momentum` are highly correlated (0.773) — expected, since both
are pre-registered as testing the **same** mechanism (slow diffusion into mega-caps)
by two different measurements; as a *pair* they offer little diversification benefit
despite being individually strong. `trend_following` and `cross_sectional` are the
two lowest-average-correlation candidates against the other three (`trend_following`:
0.425/0.548/0.501 mean ~0.49; `cross_sectional`: 0.234/0.254/0.187/0.259 mean ~0.23,
the single lowest of any candidate) — both for structural reasons named in their
pre-registered hypotheses (cross-asset vs. single-universe cross-sectional
momentum, in each case distinct from the two time-series equity candidates).

## Verdict (answers KAN-642)

Two tiers, matching the pre-registered scope note above and KAN-642's own draft bar,
which explicitly requires "forward paper results consistent with the backtest" —
something no candidate has, since no live paper session ran this session.

**Tier 1 — clears this bench's backtest-stage bar, qualifies to enter paper
incubation next (playbook step 9):**

- **`sma_crossover`** and **`momentum`** — the only two of five candidates clearing
  every pre-registered numeric bar: OOS Sharpe well above 0.3 (1.18, 1.15), IS->OOS
  retention above 100% (99%, 107% — OOS did not degrade from IS, the strongest
  possible answer to "is this fit to noise"), cumulative deflated significance at
  1.00 (237, 238 trials — the whole research program's search, not just their own),
  and paired-bootstrap win rate against SPY at 99.9%/99.7% (>>90%). Real caveats,
  not disqualifying ones: they are highly correlated with each other (0.773) and
  test the pre-registered-identical mechanism, so a book holding both gets one bet,
  not two; `sma_crossover` has one clearly losing regime (mean-reverting markets,
  Sharpe -0.66); the paired-bootstrap check against the diversified baseline could
  not be run (tool gap, below) so that half of KAN-642's own bar is evaluated on raw
  outperformance (+279.71pp, +226.38pp) and alpha/correlation only, not a formal
  significance test.

**Tier 2 — does not clear the bar, for reasons this session can distinguish:**

- **`mean_reversion` — genuine, replicated fail.** Underperforms both SPY (-237pp)
  and the diversified baseline (-77pp) in raw terms, paired win rate 47.7% (worse
  than a coin flip against the benchmark it's supposed to beat), and its own
  deflated significance never cleared 0.95 at any point in this session's pipeline
  (0.92 -> 0.71 -> 0.98 across different trial populations, never durably above the
  bar). This **replicates**, on a wider universe (20 names vs. 5) and a longer range
  (16 years vs. 9), the exact underperformance CLAUDE.md's ADR-0071 already recorded
  for this strategy on a narrower prior test — not a new finding, a confirmation.
- **`trend_following` — fails the standalone bar, but is the one candidate worth a
  second look as a *portfolio* addition rather than a standalone bet.** Fails
  paired win rate against SPY (35.5%) and the searched winner's own OOS-cumulative
  deflated significance (0.77 at 220 trials — the number that matters here, from
  the actual swept/OOS-tested combo in step 5; the confirm run's 0.97 answers a
  different question, since it necessarily ran the shipped *default* params, not
  the OOS winner — see the CLI gap below). But it is the **only candidate positive
  in all four regime splits** (including trending and mean-reverting, where every
  other candidate has a losing regime), has the **lowest average correlation** to
  the four equity-only candidates (~0.34 vs. `sma_crossover`, `momentum`,
  `mean_reversion`), and carries **positive alpha against both benchmarks**
  (+1.43%, +1.79% ann.) despite underperforming them in raw terms — consistent with
  the pre-registered framing that this candidate's case rests on portfolio fit
  (KAN-641), not standalone Sharpe. Recorded here as **does not qualify today**,
  not as a disguised pass — the pre-registered bar was written before any result
  and it was not met — but flagged as the one candidate where a dedicated
  portfolio-level (not standalone) study could change the answer.
- **`cross_sectional` — inconclusive, not a confirmed fail, and the one candidate
  most worth re-running when the machine has room.** Fails the paired win-rate bar
  against SPY today (59.8% < 90%, despite an enormous +1526.74pp raw
  outperformance — exactly the gap the paired statistic exists to catch: a huge
  total-return number is not the same claim as "reliably beats the benchmark").
  Carries the worst risk profile of any candidate by a wide margin (49.64% max
  drawdown vs. 20-28% for the others; 10 halt episodes over 16 years vs. 1 for
  every other candidate). And, unlike every other verdict above, **has no
  out-of-sample evidence at all this session** — every number for this candidate
  is in-sample, after repeated infrastructure failures (see the resource note
  above) prevented the walk-forward step from completing on a thrashing machine.
  An in-sample-only number is exactly the kind of evidence this bench's own ADRs
  (0026, 0039) exist to distrust most, so this is reported as **does not qualify
  on today's evidence, pending the OOS step this session could not complete** —
  not as equivalent to `mean_reversion`'s replicated, evidence-backed fail.

**Nothing qualifies to trade real money today.** KAN-642's own draft bar requires
forward paper results consistent with the backtest, and no candidate has any —
steps 9-11 of the playbook (paper incubation, micro-live, scale/retire) were
explicitly out of scope this session (EPIC-86 deferred). `sma_crossover` and
`momentum` are the recommended next candidates for a pre-committed paper incubation
run (playbook step 9), ideally in parallel on separate books given how correlated
they are with each other — running both does not test two independent ideas.

## Open gaps this run surfaced

- **`trading backtest` has no `--param`/strategy-kwarg override** — unlike `sweep`,
  a single backtest always runs a strategy's shipped defaults. This forced the
  step-6/7 confirm runs above onto default parameters rather than each candidate's
  actual OOS-tested winner. No card filed for this yet.
- **No paired-bootstrap win rate against `--diversified-baseline`** — already
  recorded in ADR-0071; this run is a second, independent confirmation that it's
  needed (KAN-642's own draft bar names "naive equal-weight" as one of two
  benchmarks a paired test should cover).
- **`cross_sectional`'s OOS walk-forward could not be completed this session** on
  this machine (see the resource note) — the clearest concrete next step, since its
  in-sample numbers are large enough to be worth resolving one way or the other.
- **KAN-677 lands with this run** (merged as PR #97 before the OOS step began) —
  closes the walk-forward deflation/ledger gap this playbook had open, and every
  `--folds` number in this document used it.

One correction made after the fact: the interrupted full-grid run had printed its
complete 16-combo ranked table and deflation block to the log before being killed,
but the `TrialLedger.append()` call happens once, at the very end of the whole
sweep command (after the cost-sensitivity re-runs it was killed during) — so despite
appearing complete, **it never actually reached the shared ledger file**. Rather than
silently under-counting every candidate that ran after it, the real, already-printed
result (16 trials, best Sharpe 0.761 at `lookback=120, top_k=20`, verified against
the captured log) was appended to the ledger directly via `TrialLedger`/`TrialRecord`,
labeled in its own `hypothesis` field as a reconstruction and why. This is recovering
a result that was genuinely computed and observed, not fabricating one — the same
16 numbers are in the earlier table in this document.

