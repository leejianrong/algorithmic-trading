# Paper incubation results — `sma_crossover`, session 1, and an unattended-machine crash (2026-09-13)

> **This measurement and incident investigation was performed by the orchestrating
> (PM) session directly against the real Alpaca paper account and the real local
> artifacts on the operator's machine.** This document transcribes and structures
> that investigation; it does not re-derive any of it, and the writing session had
> no Alpaca credentials and no access to the run's `results/paper/` directory (see
> "Artifact availability" below). Executes
> [`docs/paper-incubation-2026-09-02.md`](paper-incubation-2026-09-02.md)'s
> (frozen, unedited) pre-registration for `sma_crossover`'s session — the first of
> the two sessions committed there. Answers KAN-1076 (EPIC-139).

## Headline

**The pre-registered trading day substantively completed and every kill criterion
clears — cleanly, and in the conservative direction the equity divergence line has
consistently shown.** 71 paired fills (comfortably above `MIN_PAIRED_FILLS = 30`),
mean realized slippage **−5.59 bps** against the 5.0 bps model — realized fills were
*better* than modelled, the same "the model is conservative" direction ADR-0052
found on the original equity divergence run. No guardrail halt fired, no
contamination, no duplicate-order or wash-trade problem.

**Separately, and not touching that result: the host machine crashed or was
suspended/rebooted overnight**, unrelated to the trading logic, sometime between
2026-09-10 00:34 UTC and 13:33 UTC. `PaperSession.finalize()` never ran —
`result.json` and `equity_curve.csv` were never written — which is exactly ADR-0048's
documented "crashed session" case (its own docstring names `kill -9`, an OOM kill,
power loss, or a suspending laptop as explicitly out of scope for ADR-0043's
graceful-SIGTERM fix). `fill_divergence.csv` survived intact, durable by design
(ADR-0048), and is the artifact this entire results section is built on. Alpaca's
own order history — not anything local — is what makes it possible to say the
trading day itself was not truncated by the crash, only the local session's
bookkeeping was.

**As of this writing, the account is not yet confirmed flat.** Six positions were
found unmanaged on return to the machine (2026-09-13) and a flattening SELL was
submitted for each, but the venue was closed at submission time and none has yet
been confirmed filled. See "Remediation" below — do not read this document as
saying the account is known-clean.

## 1. Pre-registration this session executes

Per `docs/paper-incubation-2026-09-02.md` §§4–6 (frozen, unedited by this
document): one full trading-day live session, `sma_crossover`, `@blue20`,
`--interval 5m`, `--divergence --bootstrap --ledger
research/kan642_trial_ledger.jsonl --hypothesis "..."`, launched via the operator
targets that wrap `make paper-live`.

## 2. Launch

Session launched **2026-09-09 13:18:34 UTC** under tmux session
`paper-2026-09-09T131834Z`, pid 245030, `--out
results/paper/2026-09-09T131834Z-incubation-sma_crossover`.

Full command, per the pre-registration's §6 flags:

```
trading paper --strategy sma_crossover --symbols @blue20 --interval 5m \
  --market us_equity --source alpaca --broker alpaca --live --divergence \
  --from 2026-09-09 --to 2026-09-09 \
  --out results/paper/2026-09-09T131834Z-incubation-sma_crossover \
  --data-feed iex --bootstrap \
  --ledger research/kan642_trial_ledger.jsonl \
  --hypothesis "large, index-heavy holders (funds tracking benchmarks, pension
  rebalancers) cannot instantly reprice a mega-cap on new information — flows into
  and out of a name this size are throttled by market-impact-aware execution
  schedules over days to weeks. A fast/slow SMA cross is a lagging proxy for 'the
  flow has actually started,' entered after the fact rather than anticipating it.
  Counterparty: disposition-effect retail sellers who exit winners too early and
  short-horizon mean-reversion traders who fade the first leg of a move, both run
  over by a sustained institutional rebalance that plays out over weeks — paper
  incubation session 1, playbook step 9."
```

(Quoted from `docs/paper-incubation-2026-09-02.md` §6's `sma_crossover` entry; this
worktree does not have the run's own `launch.cmd` to quote byte-for-byte — see
"Artifact availability" below.)

Warmup primed **642 completed bars** as history — `2026-08-27 19:30` through
`2026-09-09 12:25` — with no orders submitted for them, correctly (ADR-0042).

## 3. The incident: an unattended crash, not a code defect

The machine (WSL2) crashed, was suspended, or rebooted at some point after the
session had already been running for several hours. This is unrelated to the
trading logic itself — it is the exact "no supervision, no restart" gap
`docs/monday-divergence-run.md`'s own runbook already warns about ("Stop the
laptop sleeping. There is no supervision and no restart. If the machine suspends,
the run is gone.").

Evidence, in order of what it establishes:

- **(a) The local console log stops mid-session, but not silently.**
  `results/paper/2026-09-09T131834Z-incubation-sma_crossover/console.log`'s last
  regularly-ordered bar-decision line is stamped `2026-09-09 18:15` UTC. But the
  file also contains one line badly out of sequence relative to the surrounding
  bar-time-labeled lines — a real wall-clock logger timestamp of
  `2026-09-10T00:33:59Z`:

  ```
  WARNING trading.data.recent_window: TSLA dropped from this poll: data lookup
  failed (ConnectionError: ('Connection aborted.', ConnectionResetError(104,
  'Connection reset by peer')))
  ```

  This line sits between the `18:15` and `18:20` bar-decision lines in the file,
  and is evidence the process was **still alive and polling** nearly 11 hours
  after launch, well past market close — tolerating the overnight silence exactly
  as ADR-0049 is designed to. The transient per-symbol network failure it reports
  is, on its own, benign and expected: ADR-0035 retries a dropped symbol forever
  and never quarantines it.

- **(b) The session's last written state snapshot is from bar-time `18:20`.**
  `paper_state.json`'s last snapshot is timestamped `2026-09-09T18:20:00+00:00`,
  holding 10 positions (AMZN, COST, CVX, GOOGL, HD, JNJ, META, NVDA, PG, XOM),
  equity $99,776.75.

- **(c) `result.json` and `equity_curve.csv` were never written.**
  `PaperSession.finalize()` never ran — exactly ADR-0048's documented "crashed
  session" case, which its own docstring names as `kill -9` / OOM / power loss /
  a suspending laptop, explicitly out of scope for ADR-0043's graceful SIGTERM
  handling. This bench's own runbook (`docs/monday-divergence-run.md`) warns
  about precisely this: "it is NOT safe to let the machine sleep."

- **(d) `fill_divergence.csv` survived intact** — 22,389 bytes, 72 data rows —
  because of ADR-0048's atomic per-row durability design: each row is journaled
  the moment both the live and the shadow side have settled, so a crash costs
  everything after the last settled row, never anything before it. This is the
  artifact the kill-criteria analysis in §4 below is built on.

- **(e) On return to the machine (2026-09-13), the tmux server itself was gone.**
  `tmux list-sessions` returned `error connecting to /tmp/tmux-1000/default (No
  such file or directory)` — no tmux server at all, not merely a dead pane. `last
  reboot` showed multiple system boots over the weekend: Sat 2026-09-12 16:08,
  16:09, 16:13, and the current boot Sun 2026-09-13 17:32. This confirms the
  *whole environment*, not just one process, went down and came back — multiple
  times.

**The exact moment of the fatal crash cannot be pinpointed** beyond "sometime
after 2026-09-10T00:34 UTC" (the last confirmed-alive log line). Stated plainly
as unrecoverable precision rather than guessed at: nothing in the surviving
artifacts narrows the window further.

## 4. The real timeline, from Alpaca's own order history

The local artifacts stop mid-session, but Alpaca's own closed-order history —
fetched directly against the live account
(`TradingClient.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED,
after=..., direction='asc'))`, the same raw-SDK pattern
`scripts/crypto_fee_reconcile.py` already uses and documents, because the
`AlpacaClient` seam has no `list_orders` call) — is authoritative and independent
of anything the local machine wrote. It shows:

- **76 closed orders total** between 2026-09-09T13:00Z and the time of
  investigation.
- **Active, continuous trading throughout the full regular session**:
  2026-09-09 13:33:02 UTC (first fill, META buy) through 2026-09-09 18:20:22 UTC
  (last fill before the local-log gap, MSFT sell) — dozens of round-trip
  target-weight rebalances, `sma_crossover` behaving exactly as designed.
- **A gap in the order history itself** (not just the local logs) until
  **2026-09-10 13:32:47 UTC**, when four SELL orders fill essentially
  simultaneously (within about 65 seconds of each other): META, NVDA, COST,
  GOOGL. This is the documented parked-order-fills-at-next-open behavior
  (ADR-0036): these orders must have been submitted by the still-alive process
  sometime after Wednesday's ~20:00 UTC close (while the venue was shut, since
  they parked rather than filling same-day) and before the process died, then
  Alpaca's own matching engine filled them automatically at Thursday's 09:30 ET
  open — independent of whether the process was still running by then.
- **Zero order activity after 2026-09-10 13:33:52 UTC.** This means the process
  was definitively dead by then, though it could have died any time between
  00:34 UTC and 13:33 UTC that morning — the same imprecise window §3 already
  states.

**Substantive conclusion: the pre-registered "one full trading day"
(`docs/paper-incubation-2026-09-02.md` §4) was genuinely completed.** Every fill
either happened during Wednesday's regular session or was the documented,
expected settlement of an order the still-alive process had already placed
before the crash. The crash is a real operational failure, but it did not
truncate the trading day's actual data collection.

## 5. Kill-criteria walkthrough (`docs/paper-incubation-2026-09-02.md` §5)

Computed directly from `fill_divergence.csv` (72 total divergence rows).

**Divergence.**

- 71 of 72 rows are paired fills (`live_outcome == model_outcome == "filled"`) —
  comfortably above `MIN_PAIRED_FILLS = 30`.
- Mean `slippage_error_bps` (realized − modelled, per `divergence.py`'s own
  definition — positive means the backtest was optimistic) across the 71 paired
  fills: **−5.59 bps** (median −4.25, population stdev 11.83, min −91.78, max
  +11.20). **Negative means realized fills were better (lower-cost) than the 5.0
  bps model assumed** — the same direction ADR-0052 found on the original equity
  divergence run (which measured realized slippage of +0.51 bps against the 5.0
  bps model, i.e. `slippage_error_bps` of about −4.49, "conservative by ~4.5
  bps"). This session's −5.59 bps is a comparable magnitude in the same
  direction: both runs found the equity cost model conservative, not that the
  two numbers are the same measurement repeated.
- Only **one** row has `slippage_error_bps > +10` (a single +11.20 bps outlier
  among 71) — nowhere near the kill criterion, which the pre-registration states
  in terms of the **mean**, not any single fill. The mean is deeply negative
  (favorable), so neither the hard-stop band (mean worse than modelled by more
  than 10 bps) nor the 5–10 bps "flag but continue" band is triggered at all.
- **Verdict: clears cleanly, in the conservative direction.**

**Drawdown/guardrail.** No halt fired at any point — `halted: false` throughout
`paper_state.json`'s history. **Verdict: clears.**

**Contamination check.** The first real (non-warmup) order was submitted at
`2026-09-09T13:25:17Z`, after warmup completed at `13:18:46Z` — no
`submitted_ts` in `fill_divergence.csv` predates warmup-complete. **Verdict:
clears — the ADR-0042 warmup guard held.**

**Operational check.** No duplicate-order/wash-trade refusals were observed in
the console log or divergence file. **Verdict: clears — nothing to investigate
on this axis.**

One documented, expected row worth naming explicitly rather than treating as a
concern: exactly one `outcome_diverged=true` row — an `HON sell` at
`submitted_ts=2026-09-09T13:50:00Z` where the live broker filled it but the
shadow model rejected it with `"Cannot sell 22.778759 of HON; only 0.0 held
(implicit shorting is disallowed)"`. This is a timing/book-sync artifact of the
counterfactual `ShadowBroker` replaying against a copy of the pre-bar book —
`divergence.py`'s own module docstring documents "a modelled funding rejection
vs a venue fill" as one of the expected row shapes a divergent outcome can take
— not a live broker defect. The live HON position genuinely existed and the
sell filled correctly at the venue.

**Account equity.** Final equity when local artifacts stopped: $99,776.75 (the
2026-09-09 18:30 bar). Actual equity checked live on 2026-09-13, before
flattening: **$99,841.18** against the $100,000 starting balance — essentially
flat.

**Summary: all four pre-registered kill criteria clear.** This is a pass,
subject to the caveat that the account is not yet confirmed flat (§6).

## 6. Remediation (2026-09-13) — NOT YET CONFIRMED COMPLETE

On return to the machine, the account held **6 unmanaged open positions**
(AMZN 18.832217, CVX 22.22935, HD 15.252808, JNJ 17.688485, PG 33.190921,
XOM 28.977235 shares) with **zero working orders** and **zero strategy process
running**. Cash was $71,208.59.

With explicit user confirmation obtained before any order was submitted, a
market SELL for the exact held quantity of each position was submitted directly
via `RealAlpacaClient.submit_order` (the same tested seam `AlpacaBroker` uses in
production). All six came back `status=accepted` — parked, since the venue was
closed at submission time (Sunday) — matching ADR-0036's documented
parked-order behavior exactly. They are expected to fill automatically at the
next market open (Monday 2026-09-14 09:30 ET).

**This flattening has NOT yet been confirmed complete.** A follow-up check
after Monday's open is a required next step, not an assumption this document
makes: re-run `make paper-preflight` and expect 0 positions and 0 working
orders. Until that check runs and passes, this document's own §"Headline"
caveat stands — do not read the account as known-clean.

## 7. Artifact availability

`results/paper/2026-09-09T131834Z-incubation-sma_crossover/` (containing
`launch.cmd`, `console.log`, `paper_state.json`, `fill_divergence.csv`) is not
present in the worktree that wrote this document — `results/paper/` is
gitignored, as is standard for this bench's run outputs, and this worktree is a
fresh checkout with no access to the operator's local filesystem. The raw
artifacts live only on the operator's machine under `results/paper/...`. Every
fact and figure in this document is transcribed from the orchestrating (PM)
session's direct investigation of those files and the live Alpaca account, not
independently re-derived here.

## 8. Two backlog cards this incident directly validates

- **KAN-686** ("Supervise the process: restart on crash without restarting into
  a loop", EPIC-86) — this incident is now concrete, measured evidence for the
  need, not a hypothetical risk. ADR-0043 already solved the *graceful* stop
  case (SIGTERM/Ctrl-C); this incident is the *ungraceful* case
  (power loss/suspend/reboot) that ADR-0048's own docstring already named as
  explicitly out of scope for that fix. Without process supervision, the only
  thing standing between "session crashes overnight" and "account sits
  unmanaged for days" was a human noticing.
- **KAN-829** ("`make paper-flatten`: liquidate the account after a session",
  EPIC-78) — the remediation in §6 above was done by hand, one position at a
  time, directly against the SDK, because no tooling exists to do it. A
  dedicated flatten target would have turned this into one command instead of
  a manual, ad hoc SELL per symbol.

## 9. Recommendation for `momentum`'s still-pending session

`momentum`'s incubation session (the second of the two pre-registered in
`docs/paper-incubation-2026-09-02.md`) has not yet run. Given this incident,
that session should either:

- run on infrastructure less prone to sleep/suspend/reboot over an unattended
  multi-hour window (a cloud VM or always-on host rather than a laptop/WSL2
  machine that can be suspended by the host OS), or
- wait until KAN-686's process supervision lands, so a crash mid-session
  restarts cleanly instead of leaving an unmanaged, unflattened book for days.

Either mitigation directly addresses what actually went wrong here — not the
trading logic (which behaved correctly throughout, including through the
crash) but the complete absence of anything watching the process or the
account once it stopped being watched by a person.

## Why this is not an ADR

Checked `ls docs/adr | sort -V | tail -3` before writing this: the next
available number is **ADR-0075** (last landed:
`0074-walk-forward-and-paper-trial-accounting.md`). This document deliberately
does not claim that number, for the same reason
`docs/crypto-daily-tape-density-2026-09-09.md` gave for its own measurement:
every ADR in this repo records a **decision** — a new default, a new
guardrail, a new config knob, or a reversal of a prior one. This document
records an **incident and a measurement** that confirm existing decisions
rather than changing any: ADR-0048's crash-durability design worked exactly as
specified (the journal under-reported, never misreported), ADR-0042's warmup
guard held, ADR-0049's silence tolerance did what it says, and the equity
divergence result reconfirms ADR-0052's "conservative" finding rather than
overturning it. No code changed, no default moved, no existing decision was
reversed or amended. The two backlog cards this incident validates (§8) are
where any eventual decision — process supervision, a flatten command — would
land, and neither has been built or decided here.
