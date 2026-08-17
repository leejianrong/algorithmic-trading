# The research playbook

> A repeatable loop from a hypothesis to a real dollar, so strategy research is a
> process instead of an improvisation. It is written to be followed operationally —
> like [`monday-divergence-run.md`](monday-divergence-run.md) — not read once and
> filed away. Where a step has a tool, the command is here and copy-pasteable
> against today's CLI. Where it doesn't, that is said plainly, with the card number
> that will build it.

## The one rule everything else enforces

**Write the hypothesis and the kill criteria down before you see the result.**

Every other section of this document is bookkeeping in service of that one rule.
A backtest run *after* you already have an opinion about whether it will work is
not a test of the opinion — it is a search for a number that confirms it, and this
bench has a whole shelf of ADRs (0026, 0039, 0059, 0062) about how easy that search
is to win by accident. Pre-registration is the only defense that does not depend on
discipline holding up under the temptation of a good-looking equity curve.

Concretely: steps 1 and 2 below happen in a text file or a Pandan card, **before**
step 3 touches any data. If you cannot point to the sentence you wrote before you
ran anything, you have not pre-registered — you have a comforting story.

## Where this loop actually lives, today

Two pieces of infrastructure this playbook leans on hard landed recently and are
worth naming up front, because they are the difference between this being an
honest process and being a nice suggestion:

- **The cross-invocation trial ledger** (`trading.ledger.TrialLedger`, ADR-0062,
  KAN-858) — `--ledger PATH` on `backtest` and `sweep` appends one JSONL line per
  invocation and widens the ADR-0039 trial-count deflation by everything logged
  before it. Before this landed, every deflated Sharpe on this bench only ever
  saw the trials in *one* command. Now a search spread across many sessions is at
  least partially visible to the correction it is supposed to feed.
- **`--hypothesis TEXT`** — recorded verbatim alongside the count, on the same two
  commands. It is not yet *enforced* — nothing checks it was written before the
  result, nothing checks it is non-empty, nothing blocks a run without one. That
  enforcement is this playbook's job, and today the enforcement is you: the
  discipline of writing `--hypothesis` before you touch `--param`, not a gate the
  tool imposes. Treat every worked command below as incomplete if you skipped that
  step for real.

## The loop

### 1. Hypothesis: name the structural edge, and name who is on the other side

Before opening a terminal, write down, in plain language:

- **The mechanism.** Why would this pattern exist in prices? Not "SMA crossovers
  work" — a specific, falsifiable story about order flow, information asymmetry,
  structural rebalancing, a forced seller, a liquidity provider being paid for
  risk. "Trend-following captures the slow diffusion of information into price
  because large holders can't rebalance instantly" is a hypothesis. "Fast MA over
  slow MA gives a buy signal" is a parameter, not a reason.
- **The other side of the trade.** If you cannot name who is structurally willing
  to lose money to you — and why they keep doing it — you have not found an edge,
  you have found a pattern in noise that a large enough grid search always finds
  (this is exactly what ADR-0039's trial deflation exists to discount). "Retail
  momentum chasers buying the breakout I'm selling into" is an answer. "The
  market" is not; markets don't lose money, participants do, and if you can't name
  one, the honest prior is that the pattern is sampling noise.
- **The kill criteria**, decided now, not after seeing a drawdown: what
  out-of-sample Sharpe, what walk-forward degradation, what live-vs-model
  divergence, and what real-money drawdown makes you stop — in numbers you write
  down today, because "I'll know it when I see it" is exactly the judgement a bad
  result corrupts. Section headings below (§5, §7, §9, §11) each need one of these
  numbers before you get there.

There is no tooling for this step and there should not be — a form does not make a
hypothesis honest, writing it down before the result does. Put it in the Pandan
card, a dated note, or the `--hypothesis` string you will pass at step 4. What
matters is that it exists somewhere with a timestamp *before* step 3's first run.

### 2. Freeze universe, costs, and the OOS slice — before looking at returns

Decide and write down, still before any return number exists:

- **The universe.** A named basket (`--symbols @blue20`, `--symbols @crypto10`) or
  an explicit list, chosen for a reason that has nothing to do with which symbols
  happen to look good in a quick look — that quick look is exactly the kind of
  look this step exists to prevent. `--min-adv` (ADR-0029) screens out anything
  too thin to fill at the modelled price, using a formation window that ends
  before `--from` so the screen itself cannot look ahead.
- **The market and its cost model.** `--market us_equity|crypto_24_7` (ADR-0057)
  is one flag that pins the annualization calendar, the bar-completeness rule, and
  the cost model (ADR-0060) together — it refuses a crypto-shaped symbol on an
  equity session and vice versa, so you cannot end up silently comparing figures
  scored on two different years. Pick it now; changing it after seeing a Sharpe is
  changing the population the statistic was computed over.
- **The OOS slice.** Decide *now* which span is out-of-sample and will not be
  looked at until step 5. In practice this means picking `--folds N` for
  `trading sweep` up front (ADR-0026: `folds + 1` equal segments, each fold trains
  on everything before it and tests on the next segment exactly once) rather than
  eyeballing a date and calling it a holdout later. `--windows N` is **not** this —
  it is a plain in-sample per-window sweep with nothing held out, and the CLI help
  says so; if you reach for it here you have not frozen an OOS slice at all.

Nothing in this bench enforces that you did this before step 3 — that enforcement
is the discipline this document asks of you, not a CLI flag.

### 3. Cheap kill tests first: smallest parameter set, in-sample, free

Run the strategy **once**, at the smallest defensible parameter set, over the
whole in-sample span, with no sweep. Most ideas should die here, and dying here is
free — one backtest costs a fraction of a second offline.

```bash
uv run trading backtest --strategy sma_crossover --symbols @blue20 \
  --source synthetic --seed 5 --market us_equity \
  --min-adv 20000000 \
  --from 2015-01-01 --to 2022-12-31 \
  --out results/research/kill-test/equity_curve.csv
```

What kills it here, cheaply, before any optimisation:

- **Entry count.** The report prints an entry count automatically (ADR-0029); if
  it is in the single digits over a multi-year run, there is nothing to learn from
  a Sharpe computed on it, sweep or no sweep.
- **Trades-per-parameter.** `free_parameter_count` reads the strategy's
  constructor signature; below `MIN_TRADES_PER_PARAMETER = 30` the summary warns
  explicitly. A strategy that needs a sweep to even approach 30 trades per
  parameter is already a bad sign at the smallest parameter set.
- **Sign and shape, not precision.** Is the total return positive at all, does the
  equity curve avoid a single catastrophic bar, does the benchmark comparison
  (`--benchmark SPY`) show anything better than "the return on idle cash" (ADR-0037
  flags a benchmark that never got invested — read that caveat if it appears; it
  means the comparison is meaningless, not that the strategy beat cash). If the
  answer to all three is no at the smallest, most defensible parameter set, stop.
  Optimising a dead strategy only produces a more expensively dead one.

This step deliberately does not use `--bootstrap` or `--ledger` — both cost
something (compute, or a durable side effect on disk) and neither is worth paying
before the cheap kill tests have passed.

### 4. In-sample optimisation, trial count logged to the ledger

Only once step 3 survives: sweep the grid you actually intend to search — decided
in step 1, not expanded after seeing step 3's number — and log every trial to the
ledger with the hypothesis attached.

```bash
uv run trading sweep --strategy sma_crossover --symbols @blue20 \
  --source synthetic --seed 5 --market us_equity \
  --param fast=5,10,15,20 --param slow=30,50,80,120,200,250 \
  --rank-by sharpe \
  --from 2015-01-01 --to 2020-12-31 \
  --ledger research/trial_ledger.jsonl \
  --hypothesis "trend-following captures slow diffusion of information; retail momentum chasers are the counterparty; kill at OOS Sharpe < 0.3 or 40% IS->OOS degradation" \
  --out results/research/sweep/sma_grid.csv
```

This prints the ranked table, and under it the deflation block: the winner's
Sharpe scored against the Sharpe the luckiest of that grid's trials would have
shown with no edge at all (ADR-0039), now widened by `--ledger`'s cumulative count
from every earlier logged invocation (ADR-0062) — so a strategy tried by hand six
times across six sessions is deflated against something closer to the true search
size, not just this one grid.

Two honesty limits, stated because the tool states them every time:

- The **spread** behind the correction still comes from *this* grid's trial
  Sharpes only — the ledger stores counts, not each historical trial's own Sharpe
  (ADR-0062 §3), so a ledger built entirely from single backtests can grow the
  visible count without ever supplying a spread to price it against. Widening the
  count without a matching spread is a genuine limitation, not a bug to route
  around.
- The trial count is **still a lower bound**, even with the ledger. Trials made
  before the ledger existed, on a different ledger file, or by a habit of not
  passing `--ledger` at all, are invisible. The printed note says so every time.

**The `sweep --windows N` mode is not this step either.** It is the same
"exploration, all in-sample" idea as this section, run per-window instead of over
the whole range — useful for eyeballing stability across sub-periods, but every
number it produces is in-sample and none of it substitutes for step 5.

### 5. One OOS shot: true walk-forward, no peeking

`trading sweep --folds N` (ADR-0026) is the one command on this bench that can
answer "does this survive data the optimiser never saw" honestly: each fold tunes
the whole grid on its in-sample span, then runs **the single IS winner, exactly
once**, on the untouched OOS span. Nothing about an OOS result ever feeds back into
selection — that is the entire point, and it is pinned by a test that counts
adapter requests per span rather than merely reading the numbers.

```bash
uv run trading sweep --strategy sma_crossover --symbols @blue20 \
  --source synthetic --seed 5 --market us_equity \
  --param fast=5,10,15,20 --param slow=30,50,80,120,200,250 \
  --rank-by sharpe --folds 4 --wf-mode anchored \
  --from 2010-01-01 --to 2022-12-31 \
  --out results/research/walkforward/sma_wf.csv
```

Read `mean_out_of_sample_sharpe` / `median_out_of_sample_sharpe` against
`mean_in_sample_sharpe`, and `sharpe_degradation` (the gap) and
`sharpe_retention` (the ratio, `None` when the IS mean is not positive — a ratio
against a non-positive base is meaningless, not zero). This is where step 1's
written kill criterion gets applied, not adjusted: if you wrote "kill below 0.3
OOS Sharpe" and it comes back 0.28, that is the criterion working, not a reason to
relax it after the fact.

**One flag does not do what you would want here, and the tool says so instead of
silently accepting it: `--ledger` is not wired into `--folds`.** Passing both
prints a stderr note — `--ledger is not yet wired into --folds walk-forward
(KAN-677); nothing was appended` — and the ledger file is untouched. KAN-677
("walk-forward prints no deflation of its own") is the tracked gap; until it
lands, the search work each fold does internally to pick its winner is invisible
to the cumulative trial count. If that search matters to your honesty bookkeeping,
log it by hand — the fold count and grid size are both printed in the summary.

### 6. Robustness battery

None of these are gates a single command clears; each is a separate check against
the winner that survived step 5. Two are built, one exists as raw material without
a renderer, and two are not built at all — stated plainly rather than described as
working.

| Check | Status today |
|---|---|
| Cost sensitivity | **Partially built.** `--slippage-bps` and `--taker-fee-bps` are real flags on `backtest`/`sweep`/`paper` (ADR-0060) — rerun the OOS-surviving combo at a few multiples of the modelled cost (e.g. `--slippage-bps 5`, `15`, `25`) and watch where the edge dies. There is no automated cost-sweep command yet; **KAN-618** ("Cost-sensitivity sweep (`--slippage-sweep`)") tracks building one, since the machinery already exists and doing it by hand is a few repeated invocations, not a missing capability. |
| Correlated-asset transfer | **Built, by reuse — no dedicated feature needed.** Run the identical strategy and parameters against a different, correlated symbol (`--symbols`) with everything else unchanged and compare Sharpe and equity shape. If the edge evaporates on a highly correlated name, it was fit to that one symbol's noise, not to the mechanism named in step 1. This is discipline in how the existing CLI is used, not new tooling. |
| Parameter heatmap | **Not built.** `sweep.csv`'s per-combination rows over a 2D grid (`--param fast=... --param slow=...`) are the exact raw material a heatmap needs — a smooth surface across neighbouring parameter values says "robust", a cliff at one specific combination says "curve-fit" — but there is no renderer, and no card number is filed for one yet. Load the CSV into a spreadsheet or notebook and eyeball the surface by hand until one exists. |
| Regime split | **Not built.** Tracked as **KAN-621** ("Regime-split metrics"). A 21-year Sharpe today averages the dot-com bust, the GFC, and the 2009–2020 bull run into one number; splitting by volatility or trend regime and checking the strategy sits flat (rather than blowing up) in an unfavorable one is the intent, and nothing in `metrics.py` does it yet. Approximate it today by manually re-running the OOS-surviving combo over hand-picked sub-ranges (`--from`/`--to`) that you believe correspond to different regimes, and comparing. |
| Monte Carlo path shuffling | **Not built.** Tracked as **KAN-859** ("Monte Carlo path shuffling"). This is a different question from ADR-0039's stationary block bootstrap: the bootstrap asks "how uncertain is this Sharpe" (and deliberately preserves serial structure so it does *not* shuffle order); Monte Carlo path shuffling asks "did the *order* of these trades matter" by reshuffling trade returns and checking whether drawdown or Sharpe collapses. Nothing on this bench answers that yet. |

The three built or partially-built rows are worth running before spending time on
the two that are not — cost sensitivity and asset transfer are cheap, offline, and
have killed more strategies historically than exotic statistics ever will.

### 7. Deflate against cumulative trials

Before calling anything "significant," re-run (or re-read, if you already have it)
the deflation from step 4 with `--ledger` pointed at the **same file** every
strategy and session in this line of research has used, so `cumulative_trials()`
reflects everything actually tried, not just this grid:

```bash
uv run trading backtest --strategy sma_crossover --symbols @blue20 \
  --source synthetic --seed 5 --market us_equity \
  --from 2010-01-01 --to 2022-12-31 \
  --bootstrap --ledger research/trial_ledger.jsonl \
  --hypothesis "confirmation run of the OOS-surviving combo, fast=10 slow=80" \
  --out results/research/confirm/equity_curve.csv
```

`--bootstrap` is required here for `backtest` to compute a `DeflatedSharpe` at
all (it is off by default because the 1,000-resample default costs real time);
`sweep` needs no such flag, since it already kept every trial's moments and the
deflation is free arithmetic under the ranking table.

Read the deflated probability against `DEFLATED_SHARPE_CONFIDENCE = 0.95`, and
read the bootstrap's confidence interval on the Sharpe itself — an interval that
straddles zero means, in the tool's own words, "this sample cannot distinguish the
strategy from having no edge at all." A point estimate that survives every
earlier step but produces a straddling interval here is not a finding; it is a
number that needs more data before it means anything.

**This is a lower bound and will remain one.** The ledger only ever supplies a
trial *count*; the *spread* behind the correction still comes from this
invocation's own trials (ADR-0062 §3). And every trial made before you started
logging to this ledger file — including whatever exploration happened before this
playbook existed for you — is invisible. Say so in the write-up, not just in the
tool's printed note.

### 8. Portfolio fit: an uncorrelated 0.5 beats a correlated 1.0

A strategy does not get judged alone if it is a candidate for a book that already
trades something. `trading.metrics.correlation(curve, other_curve)` computes the
Pearson correlation of two equity curves' aligned per-bar returns (`None` when
either side has fewer than two return periods or zero variance) — the same
function `--benchmark` already uses internally for the benchmark-relative block
(ADR-0037), but there is **no CLI command that takes two arbitrary strategy runs
and reports their correlation** the way `--benchmark SYMBOL` reports one strategy
against a buy-and-hold. If the existing book's own equity curve is on disk from a
prior `backtest`/`paper` run, this is a short script today, not a flag:

```bash
uv run python -c "
import csv
from datetime import datetime, UTC
from trading.engine import EquityPoint
from trading.metrics import correlation

def load(path):
    points = []
    with open(path) as f:
        for row in csv.DictReader(f):
            points.append(EquityPoint(
                ts=datetime.fromisoformat(row['ts']).replace(tzinfo=UTC),
                equity=float(row['equity']),
                exposure=float(row.get('exposure', 0.0)),
            ))
    return points

candidate = load('results/research/confirm/equity_curve.csv')
existing = load('results/live-book/equity_curve.csv')
print('correlation:', correlation(candidate, existing))
"
```

There is no automated verdict here, deliberately — "how much correlation is too
much" is a portfolio-construction judgement, not a threshold this bench can pick
for you. The rule of thumb this step's title states plainly: a lower-Sharpe
strategy that is genuinely uncorrelated with what you already trade can improve
the book's blended Sharpe more than a higher-Sharpe strategy that moves with
everything else. Do this arithmetic before incubating a strategy that only
duplicates a risk you are already carrying.

### 9. Paper incubation: a fixed, pre-committed duration, divergence-checked

Decide the incubation length **now**, in step 1 terms — not "until it looks good"
— and run it with `--divergence` so the live-vs-modelled fill comparison
(ADR-0038) accumulates the whole time:

```bash
uv run --env-file .env trading paper --strategy sma_crossover --symbols @blue20 \
  --interval 5m --source alpaca --broker alpaca --live \
  --data-feed iex --divergence --market us_equity \
  --from 2026-08-18 --to 2026-08-18 \
  --out results/paper/<UTC timestamp>-incubation
```

Follow [`monday-divergence-run.md`](monday-divergence-run.md) for the operational
detail of running this unattended overnight — the silence-tolerance policy, the
`make paper-live`/`make paper-stop` targets, what a clean stop looks like, and how
to flatten afterward. For a crypto candidate, read
[`crypto-divergence-run.md`](crypto-divergence-run.md) instead: a market with no
close changes enough of the procedure to be dangerous if you assume the equity
runbook still applies unmodified.

Two gaps worth knowing before you commit to a duration: **`paper` has no
`--bootstrap` and no `--ledger`** — both ADR-0039 knobs exist only on `backtest`
and `sweep` today, so a paper session's Sharpe is a point estimate with no
confidence interval and does not feed the trial ledger. And `MIN_PAIRED_FILLS =
30` is the floor below which the divergence report refuses to conclude anything
about live-vs-modelled slippage — a single day, as ADR-0052 and ADR-0061 both
found, may or may not clear it depending on the strategy's turnover and the
venue's fill rate that day. Pre-commit to enough sessions (or enough days) to
plausibly clear 30 *independent* paired fills, not just enough to make the number
print — ADR-0061 found 8 of 11 crypto fills sharing one market instant (a single
warmup burst), which clears the count without clearing the independence the count
is supposed to represent.

Judge the incubation against the kill criterion you wrote in step 1: a
realized-vs-modelled divergence beyond what you decided was tolerable is a kill,
not a note for later.

### 10. Micro-live

Small real size, same strategy, same parameters, same guardrails — the smallest
position size that still produces a real fill at a real cost, so this step tests
execution reality rather than a backtest assumption. This bench has no dedicated
"micro-live" mode; it is the same `trading paper --broker alpaca --live` path
above pointed at whatever venue account holds real capital, sized down with
`--max-position`/`--cash` and the market's own minimum order size (a $10 notional
floor on Alpaca's crypto venue, per ADR-0058). Everything from step 9 about
pre-committed duration and divergence-checking applies again, at real stakes this
time, and the kill criterion from step 1 is now checked against real dollars
instead of a paper account's simulated fills.

### 11. Scale or retire, against criteria written at deployment time

The decision to scale up, hold size, or retire is made against the **exact
numbers written down in step 1** — not a judgement formed after watching the
equity curve for a few weeks. If step 1 said "scale if micro-live Sharpe holds
within the OOS confidence interval's lower bound for 60 trading days, retire if
realized slippage exceeds the model by more than 2x for 30 consecutive paired
fills," that is the criterion, applied mechanically. If the criteria written at
step 1 turn out to have been wrong — too loose, too tight, missing a case that
mattered — that is a finding for the *next* hypothesis's kill criteria, not a
reason to relitigate this one after the fact.

## What this playbook cannot do for you

Every step above that says "not built" or "no CLI command" is a place this
document substitutes discipline for tooling. That substitution is real and it is
where the process will actually break: nothing stops an operator from writing a
hypothesis after peeking at a result, pointing different runs at different ledger
files, skipping the correlated-asset check because the first result already felt
good, or reading a robustness-battery row as "checked" because it appears in this
table when in fact nobody ran it this time. The tool enforces what it can
(ADR-0039's deflation, ADR-0062's cumulative count, ADR-0026's structural
no-peeking guarantee on the OOS run itself); everything else here is a habit, and
a habit is only as good as the operator's willingness to write the inconvenient
number down before the convenient one shows up.

## Open gaps this playbook exposed, not just inherited

- **No enforcement that `--hypothesis` predates the run it's attached to, or is
  non-empty.** ADR-0062 built the field deliberately without this; it is this
  document's job to ask for the discipline, and a future card's job to check it
  mechanically if that ever becomes worth building.
- **`--ledger` consistency is unenforced.** Nothing stops pointing different runs
  at different ledger files, or forgetting the flag entirely on some invocations
  in a research line. The cumulative count is only as complete as the operator's
  habit of always passing the same path.
- **KAN-677** — `--folds` walk-forward has no deflation of its own and no ledger
  wiring, so step 5's in-fold search is invisible to the cumulative trial count
  today.
- **KAN-618** — no automated cost-sensitivity sweep; step 6's cost check is a
  manual rerun at a few `--slippage-bps`/`--taker-fee-bps` values.
- **KAN-621** — no regime-split metrics; step 6's regime check is a manual
  re-run over hand-picked sub-ranges.
- **KAN-859** — no Monte Carlo path-shuffling; step 6 has nothing to run for it.
- **No parameter-heatmap renderer**, and no card filed for one yet; the raw data
  (`sweep.csv`) already exists.
- **No CLI command for step 8's portfolio-fit correlation** between two arbitrary
  equity curves; `trading.metrics.correlation` does the arithmetic but only
  `--benchmark`'s symbol-vs-strategy comparison is wired to a flag.
- **`paper` has neither `--bootstrap` nor `--ledger`** — a paper or micro-live
  session's Sharpe carries no confidence interval and is invisible to the trial
  ledger, both being `backtest`/`sweep`-only today.
