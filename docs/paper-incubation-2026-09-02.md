# Paper incubation pre-registration — sma_crossover & momentum (2026-09-02)

> This is a **pre-registration document, not a report.** No `trading paper --live`
> command has been run to produce it — nothing below should read as if a session has
> already happened. It is written and landed via PR *before* either live session
> runs, the same discipline
> [`docs/deployment-decision-2026-09-01.md`](deployment-decision-2026-09-01.md) and
> [`docs/crypto-research-pass-2026-09-02.md`](crypto-research-pass-2026-09-02.md)
> both applied to their own steps. It commits the plan for **two future sessions**;
> filling in a result here later would mean editing this document, which is exactly
> what pre-registration exists to prevent. Answers KAN-1076 (EPIC-139, "Paper
> incubation: sma_crossover & momentum"), per
> [`research-playbook.md`](research-playbook.md) step 9.

## 1. Candidates and why

`sma_crossover` and `momentum` are the only two of KAN-642's five candidate
strategies that cleared every backtest-stage bar in
[`deployment-decision-2026-09-01.md`](deployment-decision-2026-09-01.md)'s
verdict table (steps 6+7):

| Candidate | OOS Sharpe | IS→OOS retention | Cumulative deflated P (trials) | Paired win rate vs. SPY |
|---|---|---|---|---|
| `sma_crossover` | +1.18 | 99% | 1.00 (237) | 99.9% |
| `momentum` | +1.15 | 107% | 1.00 (238) | 99.7% |

`mean_reversion` and `trend_following` both failed their standalone pre-registered
bars in that run; `cross_sectional` never obtained an OOS result at all (killed by
resource exhaustion, not a confirmed fail). `sma_crossover` and `momentum` are the
only two candidates that document names as qualifying to enter paper incubation
next — its own verdict section states this explicitly: "`sma_crossover` and
`momentum` are the recommended next candidates for a pre-committed paper
incubation run (playbook step 9)." Nothing here re-derives that verdict; this
document starts from it as given.

## 2. Correlation and sequencing decision

`sma_crossover` and `momentum` are correlated at **0.773** — measured in
`deployment-decision-2026-09-01.md` §8's portfolio-fit step, the highest pairwise
correlation of any pair among the five candidates. This is pre-registered here as
expected, not as a surprise to react to: both candidates are pre-registered (§1 of
that document) as testing the **same** mechanism — slow diffusion of information
into mega-cap prices, measured two different ways (an MA-cross proxy for
`sma_crossover`, a direct trailing-return read for `momentum`) rather than two
independent bets on two different effects.

**Decision: the two candidates run as two separate, sequential paper sessions —
never concurrently.** Two independent reasons, both stated now rather than
discovered mid-incubation:

1. **Operational.** Exactly one Alpaca paper account exists, and only one live
   session should run against it at a time — a second concurrent session would
   trade the same account's cash and positions as the first, contaminating both.
   This holds across workstreams, not just within this one: EPIC-140's crypto
   research pass (`crypto-research-pass-2026-09-02.md` §6) states the identical
   rule for its own eventual live step, and the two epics share the same
   account. `make paper-status` must be checked, and the account confirmed flat,
   before launching either of this document's two sessions.
2. **Statistical.** The `fill_divergence.csv` (ADR-0038) and drawdown behavior
   this incubation exists to observe need each strategy's own uncontaminated
   order flow to be meaningful *per-strategy* evidence. Blending both strategies'
   orders into one session's book would mean every fill, every drawdown bar, and
   every divergence row is attributable to "the combined book," not to either
   strategy individually — exactly the kind of conflated evidence
   `deployment-decision-2026-09-01.md` §8 already flags as a reason correlated
   candidates are "one bet, not two," now applied to the *measurement*, not just
   the allocation decision.

**Sequencing implication for step 11 (scale/retire).** A future book that
eventually holds both `sma_crossover` and `momentum` is **one allocation
decision informed by two data points**, not two independent green lights — this
is stated explicitly here, not left implicit, because §1's own numbers (0.773
correlation, identical pre-registered mechanism) mean a pass on one is partial
evidence for the other, not proof of an unrelated second edge. Both sessions'
divergence and drawdown evidence should be read together at the point either
strategy is considered for step 10 (micro-live).

## 3. Universe and interval

Both sessions run `--symbols @blue20 --interval 5m`. This is the only
universe/interval combination this bench has actually measured reliably clearing
`MIN_PAIRED_FILLS = 30` (`src/trading/divergence.py`) in a single ~6.5-hour
session: ADR-0052 measured `sma_crossover` at this exact combination producing
75–108 fills per session across 25 seed/session combinations, median 87,
clearing 30 every single time.

**`momentum` has never been run live at this interval before.** Because
committing a full trading day of real session time to a candidate that turned
out to be a structurally low-fill strategy at 5m would be a wasted incubation
day discovered the expensive way, an offline synthetic proxy check was run
first to sanity-check the fill-rate order of magnitude — not to substitute for
the live measurement, which is what §4 below actually commits to.

**Proxy check (synthetic, not live, not evidence for either kill criterion):**
10 synthetic trading days, `--interval 5m --symbols @blue20 --source synthetic
--seed 7 --market us_equity`, over `2024-01-02..2024-01-16`.

- `trading backtest --strategy sma_crossover ...` produced **943** total order
  fills over the period (`result.json`'s `fills` array length), ~94/trading-day.
- `trading backtest --strategy momentum ...` (default `lookback=60`) produced
  **986** fills over the same period and universe, ~99/trading-day.

Comparable order of magnitude — `momentum`'s synthetic fill rate is not lower
than `sma_crossover`'s, which is the only question this check was built to
answer, and it supports running `momentum` for the same one-day duration as
`sma_crossover` rather than pre-emptively giving it a shorter or longer session.

**A structural concern this empirical check does not, by itself, settle — stated
so it cannot be misread as contradicted by the numbers above.** `momentum`'s
default `lookback=60` is 60 **bars**, not 60 calendar minutes — at `--interval
5m` that is ~5 hours, most of a single ~6.5-hour trading session. Taken at face
value, a naive single-day run with no warmup would spend most of the day still
filling its lookback window before its trailing-return signal could transition
at all, which is a real reason `momentum` could have come out structurally
lower-fill than `sma_crossover` in the synthetic check above. **It did not, and
the reason is specific, not a coincidence the synthetic check happens to hide:**
a live session is not naive about its lookback — `PaperSession`'s
`prime_history` (ADR-0042) loads the warmup window as data *before* the session
starts trading, so by the time the first live bar completes, `momentum`'s
60-bar lookback is already satisfied from primed history, not built up bar by
bar during market hours. The synthetic proxy check above ran through
`trading backtest`, which has no warmup phase to begin with — every bar is "in
session" from the first one — so it could not have exercised this distinction
either way. The two facts are consistent, not in tension: the lookback-length
concern is real for a naive backtest with no priming, and structurally moot for
the live session this document actually pre-registers, because priming is
exactly what removes it.

**Honest limitation, stated because the number above could otherwise be read as
more than it is:** this is a **synthetic** proxy. `SyntheticAdapter` is GBM —
ADR-0056's own caveats apply in full ("still GBM," no fat tails, no realistic
tape gaps) — and a fill count on a smooth synthetic price path says nothing
about how often either strategy's moving-average or trailing-return signal
actually transitions on real, noisier mega-cap tape. It is used here only to
rule out "momentum is structurally a low-fill strategy at 5m before spending a
real session's worth of time finding that out the hard way." **The actual live
fill rate is what each incubation session will report** — this proxy is not
cited again after this section, and no kill criterion in §5 is evaluated
against it.

## 4. Duration

**One full trading-day live session per strategy, as the first incubation
step**, decided now rather than left open-ended:

- **`sma_crossover` first**, at the next US market open — Wed 2026-09-02 09:30
  ET / Wed 21:30 SGT — for the full ~6.5-hour regular session.
- **`momentum` on the following distinct US trading day** — never concurrent
  with `sma_crossover`'s session (§2) or with any crypto live session
  (`crypto-research-pass-2026-09-02.md` §6).

**If either session's `fill_divergence.csv` comes back with fewer than 30
paired fills, that is not a conclusion.** A second session for that strategy is
required before the divergence question is considered answered for it — decided
now, before either session runs, rather than after seeing a shortfall and
rationalizing why one day should be enough after all.

**This is the start of incubation, not the whole of it.** The playbook (step 9)
calls for "a fixed, pre-committed duration" — one session per strategy is this
pass's first fixed commitment, mirroring how ADR-0052's own single divergence
run was one session, not a program. Extending to further sessions afterward is
a decision for a future PM session informed by what these two sessions actually
show, not an open-ended "run until it looks good," which is precisely the
judgment step 9 exists to replace with a number written down in advance.

## 5. Kill criteria

All numbers below are fixed **before** either session runs. The specific
thresholds here are adopted from Pandan card **KAN-1080** — a duplicate
pre-registration card surfaced from this same research line, whose numbers this
document adopts in place of an earlier draft of this section — because they
reuse this bench's own already-measured precedent (ADR-0052's own reading
bands, the shipped `RiskConfig.equity()` guardrail, ADR-0042's warmup guard)
rather than introducing new thresholds with no precedent behind them. KAN-1076
remains this document's and the incubation's canonical ticket.

- **Divergence.** Once a session has n≥30 paired fills, mean realized-vs-modelled
  slippage worse than the 5.0 bps model by more than 2x — i.e., beyond **+10
  bps** adverse — is a **hard stop**: re-open the cost model question before any
  further incubation of either candidate. A result between **+5 and +10 bps**
  worse than modelled is flagged loudly in the write-up but incubation
  continues. This directly mirrors `monday-divergence-run.md`'s own stated
  reading bands (3–8 bps: "the model is about right"; ~40 bps: "badly
  optimistic") rather than inventing a threshold this bench has no precedent
  for.
- **Drawdown/guardrail.** No new, tighter drawdown percentage is introduced
  here. Instead: if the existing `RiskConfig.equity()` drawdown kill switch
  (20% intraday, ADR-0013/0031) actually **latches** during a single-day
  session, that is the stop-and-investigate signal. Both candidates' own
  backtest behavior shows exactly **one** halt episode across 16 years each
  (`deployment-decision-2026-09-01.md`'s step-3 cheap-kill-test table) — a
  guardrail that fires once in 16 years of backtested history firing at all
  inside a single supervised day would be wildly inconsistent with that record,
  and is itself the anomaly worth investigating, not a fixed percentage picked
  without precedent.
- **Contamination check.** Verify no `submitted_ts` in `fill_divergence.csv`
  predates the session's warmup-complete timestamp.
  `monday-divergence-run.md` names this exact check ("Check the first order's
  timestamp... the one failure that would quietly ruin the numbers") — a
  specific, checkable assertion, not a vague caveat. A violation would mean the
  ADR-0042 warmup guard has regressed, and the session's evidence is
  contaminated the same way `PaperSession(warmup=True)` exists to prevent.
- **Operational, not an automatic kill.** A stream of ADR-0036 duplicate-order
  refusals, or any wash-trade/venue refusal outside the documented parked-order
  case (ADR-0041), means treating that session's fills as non-representative
  and investigating before drawing conclusions from it — not an automatic kill
  of the whole incubation. `monday-divergence-run.md`'s own "What to look out
  for" section documents both as sometimes-benign, expected behaviors of the
  parked-order/duplicate-guard machinery.
- **What this document does not decide.** "Consistent with the backtest"
  (KAN-642's own draft deployment bar) is **not** decided from one or two
  single-day sessions — that needs enough paper history to compare against the
  OOS Sharpe confidence interval, which is playbook step 11's job and out of
  scope here. This document's job is step 9's divergence/kill-criteria check
  only.

## 6. Flags for both sessions

```
uv run --env-file .env trading paper --strategy <sma_crossover|momentum> --symbols @blue20 \
  --interval 5m --source alpaca --broker alpaca --live \
  --data-feed iex --divergence --bootstrap \
  --ledger research/kan642_trial_ledger.jsonl \
  --hypothesis "<mechanism/counterparty text, see below>" \
  --market us_equity \
  --from <session date> --to <session date> \
  --out results/paper/<UTC timestamp>-incubation-<strategy>
```

- **`--divergence`** accumulates the live-vs-modelled fill comparison
  (ADR-0038) — the whole reason this incubation exists is to produce
  `fill_divergence.csv` rows for §5's divergence kill/pause check, and the flag
  is what writes them.
- **`--bootstrap`** (the `paper`-side flag ADR-0074/KAN-677 added) puts a
  confidence interval on the session's own Sharpe. Stated plainly, not as a
  defect: this CI will likely be wide or uninformative for a single day's
  return-period count, exactly the honesty `metrics.py`'s own
  `MIN_BOOTSTRAP_OBSERVATIONS` note already states about any short series — a
  wide interval here is the tool correctly saying "not enough data yet," which
  is itself useful information, not a failed measurement.
- **`--ledger research/kan642_trial_ledger.jsonl`** is the **same file** the
  whole KAN-642 research line has already used
  (`deployment-decision-2026-09-01.md` §"Shared trial ledger") — this is a
  judgment call, stated as one rather than assumed: an alternative would be a
  dedicated paper-incubation ledger, kept separate from the backtest/sweep
  trials that produced these two candidates. Continuing the same file is
  chosen instead because `research-playbook.md` §7 explicitly instructs
  pointing `--ledger` "at the **same file** every strategy and session in this
  line of research has used," so that cumulative deflation for this whole line
  of research keeps accruing rather than resetting per document. A paper
  session logs `trial_count = 1` per ADR-0074/KAN-677 (a session is one trial,
  not a search over one).
- **`--hypothesis`** reuses each candidate's exact pre-registered
  mechanism/counterparty text from `deployment-decision-2026-09-01.md` §1,
  appended with a note that this is the paper-incubation session, not a
  restatement of a different claim:
  - `sma_crossover`: *"large, index-heavy holders (funds tracking benchmarks,
    pension rebalancers) cannot instantly reprice a mega-cap on new
    information — flows into and out of a name this size are throttled by
    market-impact-aware execution schedules over days to weeks. A fast/slow
    SMA cross is a lagging proxy for 'the flow has actually started,' entered
    after the fact rather than anticipating it. Counterparty: disposition-
    effect retail sellers who exit winners too early and short-horizon
    mean-reversion traders who fade the first leg of a move, both run over by
    a sustained institutional rebalance that plays out over weeks — paper
    incubation session 1, playbook step 9."*
  - `momentum`: *"identical structural story to sma_crossover (slow
    information diffusion into large-cap prices) but measured directly as
    trailing return rather than through a moving-average proxy — a cleaner,
    more direct read on the same effect. Counterparty: same as sma_crossover —
    disposition-effect sellers and short-horizon mean-reversion traders —
    paper incubation session 1, playbook step 9."*
- **`make paper-preflight` must be run and pass** (flat account, 0 positions, 0
  working orders) immediately before **each** launch, per
  `monday-divergence-run.md`'s own "Before you start" checklist. This is an
  operational step for whoever executes each session, not something this
  document runs itself.

## 7. What this session cannot verify from a desk

No live session has run as of this writing — every number above is a
pre-registered plan, not a result. The `momentum` fill-rate check in §3 is a
synthetic offline proxy, not a live measurement, and is not evidence for either
strategy's kill criteria in §5.

`make paper-live PAPER_STRATEGY=sma_crossover` (or `PAPER_STRATEGY=momentum`)
with `PAPER_EXTRA_ARGS="--bootstrap --ledger research/kan642_trial_ledger.jsonl --hypothesis \"...\""`
is how a future session should actually launch each run — the launcher script
already bakes in `--divergence`, `--live`, `--source alpaca --broker alpaca`,
and today's date via `PAPER_DATE`, so only the strategy and the extra
ledger/bootstrap/hypothesis flags need to be supplied on top of it, following
the exact flags named in §6.

Filling in the divergence or drawdown numbers in this document with anything
other than what §5 already states would defeat pre-registration's purpose —
this document is complete as a plan, and stays unedited once either session
runs. Results, when they exist, belong in a separate results document that
cites the numbers fixed here, the same way
`deployment-decision-2026-09-01.md` kept its pre-registration section frozen
above a `## Results` line appended to only as evidence landed.
