# ADR-0062: A cross-invocation trial ledger widens the deflation, and cannot widen its spread

- Status: Accepted
- Date: 2026-08-17
- Deciders: strategy developer (project owner)
- Card: KAN-858 (EPIC-104, "Research loop & validation rigor"). Builds on ADR-0039
  (KAN-619, deflated Sharpe) and ADR-0059 (KAN-840, sweep annualization basis).
  Blocks KAN-862 (a pre-registration playbook, not built here).

## Context

`metrics.deflated_sharpe` / `assess_significance` and
`sweep.SweepSummary.deflated_winner` (ADR-0039) discount a winning Sharpe against
the number of trials that competed for it — the "best of N coin flips" correction.
`trials` has only ever counted what one process can see: a lone `trading backtest`
is 1 trial, a 24-combo `trading sweep` is 24. `metrics.trial_count_note` has printed
the same caveat since ADR-0039 shipped:

> Runs made in earlier invocations, over other date ranges, or on other strategies
> are invisible to this tool, so the correction is a LOWER BOUND on the
> multiple-comparison problem, never a complete accounting.

That sentence describes a real and growing gap. An operator who hand-tried six
strategies across twenty sessions has made far more than the 1 or 24 trials any
single invocation reports, and the deflated Sharpe — and the `probability`/
`significant` verdict built on it — is systematically **too generous** in exact
proportion to how much research was actually done. The more work an operator puts
in, the more wrong the check gets, which is backwards for a bench built on "favor
honest numbers over flattering ones." `CLAUDE.md`'s own build-status log has carried
this as an open gap since ADR-0039 landed:

> there is no cross-invocation trial ledger — the tool sees one command, so an
> operator who tried six strategies by hand has made 36 trials and the tool will
> report 1. It says so every time; it cannot do better alone.

This card is what lets it do better: a ledger the tool can actually read.

It also unblocks KAN-862, a still-unbuilt research playbook that will require a
hypothesis be written down *before* a result is seen, and will deflate against
*cumulative* ledger trials rather than one invocation's. Neither of those rules is
enforceable — or even statable — without somewhere to write the hypothesis and
somewhere to accumulate the count. This card builds that somewhere; it does not
build the playbook or its enforcement.

## Decision

### An append-only JSONL ledger, one line per invocation

`trading/ledger.py`'s `TrialLedger` is a plain file: `TrialRecord.append()` opens
the path, writes one `json.dumps`-serialized line, `flush`es, `fsync`s, and closes.
No SQLite, no rewriting, no schema migration story — append-only is what makes the
durability argument trivial rather than merely convenient, and it is the same
three-call shape `divergence.DivergenceJournal` uses for ADR-0048's "a crashed file
under-reports; it never misreports" rule, reused rather than reinvented. Unlike the
divergence journal there is no atomic temp-file-plus-`os.replace` step for a final
artifact, because there is no final artifact here to protect: every line already on
disk when `load()` runs is exactly as durable as the `append()` that wrote it.

`TrialLedger.load()` tolerates a **torn final line** — what a process killed
mid-`write` leaves behind — by dropping it silently, and raises on a malformed line
**anywhere else**, because that is real corruption (a hand edit, a disk error, a
bug) rather than an ordinary crash, and silently discarding it would hand an
operator a cumulative count they have no reason to distrust. `cumulative_trials()`
is a plain `sum(record.trial_count for record in load())`.

### The schema

```python
@dataclass(frozen=True, slots=True)
class TrialRecord:
    timestamp: str  # ISO-8601 UTC, passed in by the caller
    command: str  # "backtest" | "sweep" | "walk_forward"
    strategy: str
    symbols: tuple[str, ...]  # sorted by convention, not enforced
    date_from: str
    date_to: str
    interval: str
    market: str
    trial_count: int
    observed_sharpe: float | None
    hypothesis: str = ""
```

Two fields carry a deliberate absence rather than a stand-in. `observed_sharpe` is
`None`, never `0.0`, when a run produced no measurable Sharpe — the same
"absence is not a zero" rule ADR-0029/ADR-0037 already apply to trades-per-parameter
and benchmark statistics. `hypothesis` defaults to `""`, always present rather than
sometimes omitted, so "no hypothesis was given" and "the hypothesis was an empty
string" read as the same fact. It is otherwise unused by this card — nothing reads
it back, nothing enforces that it was written before a result was seen. It exists
now because KAN-862 needs a place to put it, and a field bolted on after the fact
could never carry a promise ("this was pre-registered") made before the field
existed. Building the enforcement is explicitly KAN-862's job, not this one's.

`timestamp` is passed in by the caller (`datetime.now(UTC).isoformat()` in the CLI)
rather than read from the wall clock inside the module — the same discipline
`clock.py` already applies to the engine, and for the same reason: a module that
reads the clock itself cannot be driven deterministically by a test.

### `prior_trials` widens the count, never the spread — and says so

`expected_max_sharpe(trials, sharpe_stdev)` needs two things to place the null: how
many trials competed (`N`) and how spread out their Sharpes were (`sharpe_stdev`).
The ledger can only ever supply the first. Storing every historical trial's own
Sharpe was considered and rejected: it would make the ledger grow without bound,
and it would still be silently wrong the moment an old experiment's file was
deleted or an old session never got logged — a ledger of *counts* is small, stays
append-only forever, and is honest about the one thing it cannot promise.

So `metrics.deflated_sharpe` gains a keyword-only `prior_trials: int = 0`:

```python
augmented_trials = trials + prior_trials
threshold = expected_max_sharpe(
    augmented_trials, stdev_per_bar if stdev_per_bar is not None else 0.0
)
return DeflatedSharpe(trials=augmented_trials, ...)
```

`stdev_per_bar` is computed only from `trial_sharpes` — this invocation's own
trials — exactly as before. A ledger-widened correction therefore uses *this*
invocation's spread as a stand-in for the historical trials' unknown one: a real
approximation, not a free upgrade, and it is named in both the docstring and the
printed note (below). One consequence follows directly and is worth stating
plainly: when this invocation is itself a single trial, `stdev_per_bar` is `None`
regardless of how large `prior_trials` is, so `expected_max_sharpe` still returns
`0.0` — a ledger built entirely from single backtests can grow the visible count
without ever supplying a spread to price it with. `test_a_lone_trial_gets_no_spread_
regardless_of_the_ledger` pins this directly.

`0` is the default everywhere `prior_trials` appears (`deflated_sharpe`,
`assess_significance`, `SweepSummary.deflated_winner`), which is what makes every
call site's pre-ledger behaviour exactly reproducible: `trials` becomes
`len(trial_sharpes) + 0`, unchanged.

`trial_count_note` now takes the **augmented** total plus `prior_trials`, and
changes its wording only when `prior_trials > 0`:

```
the deflation counts 24 trial(s): 18 from this run plus 6 carried over from
earlier logged experiment(s) in the ledger — the spread behind the correction is
still estimated from this invocation's trials only (the ledger records counts,
not each trial's own Sharpe), so this remains a LOWER BOUND twice over: on the
trial count made before the ledger existed, and on the spread of the trials it
does carry forward
```

With `prior_trials == 0` the sentence is byte-for-byte what it was before this
card, which every existing caller of the function still gets. `assess_significance`
and `SweepSummary.deflated_winner`/`cli._sweep_significance_block` both thread
`prior_trials` straight through and use the same function for the note, continuing
ADR-0039 §4's rule that two copies of that sentence are two chances for it to drift.

**The direction that matters, proved rather than asserted.** More `prior_trials`
must never make a winner's significance *easier* to claim — that would turn a
half-built feature into a way to manufacture confidence by logging noise. Because
`expected_max_sharpe` is monotonically non-decreasing in its trial count for a
fixed spread (already established by ADR-0039's own tests), and `prior_trials`
only ever adds to that count, `null_best_sharpe` cannot fall and `probability`
cannot rise as `prior_trials` grows. `TestPriorTrials::test_more_prior_trials_
never_lower_the_null` and `test_more_prior_trials_never_raise_the_probability`
check this across `prior_trials in {0, 1, 10, 100, 1_000}` rather than pinning one
pair of numbers, so the property — not a coincidence of one fixture — is what is
actually pinned.

### CLI wiring: opt-in, on `backtest` and `sweep`

`--ledger PATH` and `--hypothesis TEXT` (default `""`) on both commands, following
the `--bootstrap`/`--divergence` idiom this codebase already uses for an
expensive-or-consequential opt-in: absent, nothing is read or written; a path you
did not give is a path this tool does not touch.

**`backtest --ledger PATH`**: `TrialLedger(path).cumulative_trials()` is read
*before* significance is assembled and passed as `prior_trials` into
`assess_significance` — but only when `--bootstrap` also ran, since a plain
backtest never computes a `DeflatedSharpe` today and forcing the bootstrap on
would be exactly the silent cost `--bootstrap` exists to gate. **The ledger append
itself is unconditional on `--bootstrap`**: a plain `backtest --ledger PATH` still
appends one `TrialRecord` with `trial_count=1`, because the point of the ledger is
for a *later* invocation's `--ledger` to see this run — recording has to happen
whether or not this particular run asked to be deflated.

**`sweep --ledger PATH`**: the same `cumulative_trials()` read feeds
`SweepSummary.deflated_winner(..., prior_trials=prior)` through
`_sweep_significance_block`, and one `TrialRecord` is appended with
`trial_count=len(summary.runs)` (the whole grid) and `observed_sharpe` from the
ranked winner — after the deflation block is rendered, so a failure formatting the
report never costs the log entry, and only when the sweep produced at least one
run (an empty sweep has nothing to log, matching `SweepSummary.trial_count`'s own
rule that a combination the constructor rejected is not a trial).

**`--folds` walk-forward is explicitly out of scope**, per the card. KAN-677
already tracks "walk-forward prints no deflation of its own"; wiring a ledger into
a deflation that does not exist yet would be building on sand. `sweep --folds N
--ledger PATH` prints one stderr note — `"--ledger is not yet wired into --folds
walk-forward (KAN-677); nothing was appended"` — rather than silently accepting
the flag and doing nothing, because a flag that is quietly ignored is exactly the
kind of gap this repo's culture treats as a bug waiting to be found by someone who
trusted it.

`result.json` needed **no schema change**. `DeflatedSharpe.trials` already carries
whatever `deflated_sharpe` computed, and `report.result_to_dict` already serializes
the whole `SignificanceReport` via `dataclasses.asdict(significance)` — so the
augmented trial count flows through the existing `"significance"` key
automatically. `RESULT_SCHEMA_VERSION` stays 1, confirmed by reading
`report.py` before writing any code rather than assuming it.

## What was deliberately not done

- **No per-trial Sharpe history.** Covered above — the ledger trades spread
  fidelity for a bounded, append-only, always-correct file. A future card could add
  it, but that is a different, heavier feature (and a different set of honesty
  questions about how long history should be kept and whether deleting old
  entries is itself dishonest).
- **No SQLite, no locking, no concurrent-writer story.** A JSONL file matches the
  actual usage pattern (one operator, sequential CLI invocations) and is trivially
  `cat`/`jq`-able. Concurrent writers racing the same ledger file is not a
  scenario this bench has, and adding locking for it would be solving a problem
  nobody has yet.
- **No automatic ledger.** There is no default path and no environment variable
  that turns this on implicitly. `--bootstrap`/`--divergence` established the
  precedent that an opt-in with a real cost (compute, or now a durable side
  effect on disk) stays explicit, and a ledger silently accumulating in some
  default location the operator never chose would be a surprise, not a feature.
- **No pre-registration enforcement.** `hypothesis` is recorded verbatim and never
  checked against anything — not that it was non-empty, not that it predates the
  run it is attached to. That is KAN-862's job, deliberately deferred: this card's
  scope is "somewhere to put a hypothesis that a later enforcement can read,"
  not the enforcement itself.
- **No `--folds` wiring.** Covered above.
- **No walk-forward `command` value exercised.** `TrialRecord.command` accepts
  `"walk_forward"` in its type comment for forward-fit, but nothing constructs one
  yet, matching the walk-forward exclusion above.

## What this ADR found while building it

No new defect in existing code. This is a straightforwardly additive card — a new
module, two keyword-only parameters threaded through existing functions, two new
CLI flags — sitting on top of code that ADR-0039 and ADR-0059 had already hardened.
The one thing worth naming explicitly, because it *could* have been mistaken for a
regression the first time it was noticed: `deflated_sharpe`'s `stdev_per_bar` stays
`None` whenever the *current* invocation is a single trial, independent of
`prior_trials`. That is not a bug in this change — it is the honest consequence of
"the ledger only ever supplies a count" stated in code rather than merely in prose,
and it is pinned by `test_a_lone_trial_gets_no_spread_regardless_of_the_ledger` so
a future change cannot quietly "fix" it into fabricating a spread the data does not
support.

## Verification

- **Byte-identical without the flag.** `TestLedgerIsOffByDefault` in
  `test_cli_ledger.py` asserts `result.json`/`equity_curve.csv` (backtest) and
  `sweep.csv` (sweep) are identical whether `--ledger`/`--hypothesis` are omitted
  or passed as `--hypothesis ""` with no ledger — mirroring `test_cli_significance
  .py`'s existing pattern for `--bootstrap`.
- **The three pinned golden commands from `main@ac80bd8`**, run against this
  branch with no `--ledger`/`--hypothesis` flag (this card touches neither
  `backtest`'s nor `sweep`'s default path, and `paper` is untouched entirely):

  | command | artifact | hash matches pinned golden |
  |---|---|---|
  | daily backtest (AAPL/MSFT/NVDA, 2020-2022) | `equity_curve.csv` | yes, exact |
  | | `result.json` | yes, exact |
  | 5m backtest (AAPL/MSFT, June 2021) | `equity_curve.csv` | yes, exact |
  | | `result.json` | yes, exact |
  | `paper --once` (AAPL/MSFT, H1 2021) | `equity_curve.csv` | yes, exact |
  | | `paper_state.json` | yes, exact |
  | | `result.json` | yes, up to a 2-character drop in the pasted golden text (62 hex chars instead of the required 64; the computed hash contains the pasted string as a subsequence with `5d` inserted) |

  Two of the three commands as given in the card were missing the required
  `--strategy` flag and used a `--broker synthetic` value the CLI does not accept
  (`simulated`, not `synthetic`); `sma_crossover` and the default `simulated`
  broker were used, and every other hash landed exact, which is strong evidence
  those were the intended invocations and that the one non-matching digit pair is
  a transcription artifact in the card rather than a real divergence.
- **Property tests, not fixed-number pins**, for the one direction that matters:
  `null_best_sharpe` non-decreasing and `probability` non-increasing as
  `prior_trials` grows across five values (`0, 1, 10, 100, 1_000`).
- **Mutation testing**, each reverted non-destructively and the fast suite (1,425
  tests) re-run before restoring:

  | mutation | tests turned red |
  |---|---|
  | `deflated_sharpe` stops adding `prior_trials` to the trial count | 7 |
  | `backtest`'s ledger-append call disabled | 5 |
  | `sweep`'s ledger-append call disabled | 3 |
  | `TrialLedger.load` stops tolerating a torn final line | 2 |

- `uv run mypy` (strict, whole tree) and `uv run ruff check`/`ruff format --check`
  are clean; the full fast suite is 1,425 passed, 2 skipped (optional extras),
  96 deselected (integration/network markers).

## Consequences

- An operator can now run `trading backtest --ledger runs.jsonl --bootstrap` (or
  `trading sweep --ledger runs.jsonl`) repeatedly across sessions, strategies, and
  date ranges, and each run's deflation is scored against the true cumulative
  count the tool has actually logged — not just what one invocation ran.
- The correction is still, honestly, a **lower bound**: trials made before the
  ledger existed, trials made by a different operator or a different ledger file,
  and (as stated above) the spread of every historical trial are all still
  invisible. The printed note says so every time `prior_trials > 0`.
- KAN-862 can now build a pre-registration playbook against a schema that already
  carries `hypothesis`, without a second migration to add it later.
- **Still open**: nothing enforces that `--ledger` is used consistently (an
  operator can point different runs at different ledger files, or none, and the
  tool has no way to know); `--folds` walk-forward has no ledger wiring and no
  deflation of its own to wire it into (KAN-677); and there is still no
  cross-*operator* or cross-*machine* ledger — this is one file, on one disk,
  read and written by one CLI.
