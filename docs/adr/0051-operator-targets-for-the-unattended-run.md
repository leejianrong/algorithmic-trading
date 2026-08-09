# ADR-0051: Launching the unattended run is `make`'s job, not the CLI's

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

Ten PRs over one weekend made the 2026-08-10 divergence run
(`docs/monday-divergence-run.md`) survivable: SIGTERM finalizes (ADR-0043), the
divergence rows are on disk as they settle (ADR-0048), the feed asks for a window
the provider will answer (ADR-0047), and the session tolerates an hour of silence
before stopping (ADR-0049). Every one of those is *inside* the process.

The operator is in Singapore. A US session is 21:30 Monday to 04:00 Tuesday local,
so they will be asleep for nearly all of it, and the only moment they can influence
the outcome is the launch. What is left over after those ten PRs is exactly the
launch, and three of the four gaps are the kind that look fine until the morning:

1. **A closed terminal still kills the run.** ADR-0043 names SIGHUP as its own
   remaining gap. Nothing in `src/trading/` handles it, so hitting the lid at 22:00
   ends the session at 22:00 — with no artifacts, since an unhandled SIGHUP
   terminates the interpreter without unwinding.
2. **A hardcoded `--out` destroys the previous attempt.** ADR-0048 records that the
   journal truncates `fill_divergence.csv` at session *start*. The runbook says to
   rerun on Tuesday if Monday goes wrong, and `fill_divergence.csv` is the one
   artifact this bench cannot reconstruct — so the documented recovery path, typed
   with the same `--out`, deletes the partial rows ADR-0048 exists to preserve
   before the retry has measured anything.
3. **"Check the account is flat" is four API calls nobody makes at 21:25.** The
   runbook's "Before you start" list is prose. A parked order from a previous
   experiment does not merely sit there: it blocks the opposite side at the venue
   (ADR-0041) and mismarks the reconciled portfolio.
4. **The whole path is unprovable before the day.** There was no way to rehearse the
   exact invocation. `--once` is a different mode (ADR-0042 has it opt *out* of
   warmup), and a `--live` rehearsal against a shut venue takes an hour to
   self-terminate since ADR-0049.

## Decision

Four thin `make` targets — `paper-preflight`, `paper-dryrun`, `paper-live`,
`paper-stop`, plus `paper-status` — over `scripts/paper_preflight.py` and
`scripts/paper_session.sh`. **No Python source changed**, and the command they run
is the runbook's, unedited: same strategy, symbols, interval, source, broker, feed
and flags. It is echoed in full at every launch and copied to `<out>/launch.cmd`,
because an operator debugging at 04:00 needs to see what actually ran, and because
the runbook is the spec these were built from.

**Detach rather than handle SIGHUP.** A handler would be a source change, and it
would buy the wrong thing: SIGHUP would take ADR-0043's finalizing path, so closing
the terminal would write the artifacts and *still end the run*. Losing the night
tidily is not the goal. `tmux` is preferred — the operator can reattach on Tuesday
and read the scrollback — with `setsid` as the fallback when tmux is absent, and the
target says which one it used. Under tmux the pane is mirrored to `console.log` with
`pipe-pane`, deliberately not with `tee`: `tee` would insert a process into the
signal path between the terminal and `uv`, and the stop path depends on that path
being exactly `tmux -> uv -> python`.

**The fallback is `setsid`, not `nohup`, and that is a measurement.** The first
version used `nohup`, on the textbook reasoning that an ignored SIGHUP is inherited
across `fork`/`exec`. Launched inside a pty that was then destroyed, it **died**,
leaving a zero-byte `console.log`. `uv run` installs its own SIGHUP *handler* —
`/proc/<pid>/status` shows SIGHUP in `SigCgt`, not `SigIgn` — which overrides what
`nohup` set, so the wrapper is killable for the second or so before the python child
exists. Once the child is up it is fine: a running `nohup uv run python …` survived a
direct `kill -HUP`, and the child's `SigIgn` really does carry SIGHUP. So `nohup`
fails in exactly the window where an operator closes the terminal straight after
launching. `setsid` removes the question rather than winning the race — the session
gets its own session id and no controlling terminal, and a terminal cannot signal a
process in another session at all. The pid comes from the child writing its own `$$`
before `exec`, because `setsid` forks only when the caller is already a process-group
leader, so `$!` is not reliably the wrapper.

**Both paths were verified against the live account with the venue shut**, by
launching through `script`, letting the pty die, and watching the session keep
running (tmux: 4m39s, then a clean `make paper-stop`). The negative control matters
as much: a `SIGHUP` sent to a running session's process group killed it in under a
second and left `equity_curve.csv` and `result.json` unwritten, with only
`paper_session.log` and the journaled `fill_divergence.csv` (ADR-0048) on disk. That
is the loss these targets exist to prevent, watched rather than argued.

**`--out` is timestamped per launch** (`results/paper/<UTC stamp>-divergence`) and
the launcher refuses a directory that already exists. This is the ADR-0048
consequence stated as a mechanism instead of a warning.

**Stop is SIGTERM, and the target will not send SIGKILL.** `uv run` forks, so the
pid on record is the wrapper; uv forwards SIGTERM and the session finalizes
(verified), while SIGKILL cannot be forwarded and orphans the session. The stop path
re-reads the live pid from tmux, checks `/proc/<pid>/cmdline` still looks like our
session before signalling — a recycled pid must not be killed by a Makefile — waits
for the exit, and then lists the five artifacts with their sizes.

**The preflight reads two things outside the seam, in `scripts/`.** `AlpacaClient`
has no `list_orders` and no market-clock call, and this is not the reason to add
them: a widening is an ADR-0017 decision driven by what the *library* needs, and the
library needs neither. So the preflight uses `RealAlpacaClient` for cash, equity and
positions, and the SDK directly for working orders and the venue clock. The rule
ADR-0017 states is that no SDK type escapes the seam *inside* `trading`; an operator
script is not the library. Both reads are GETs — the preflight places no order and
cancels nothing. The clock line is informational and never fails the check.

**The dry run passes `--max-empty-polls 1`** so the rehearsal stops at the first
quiet poll rather than waiting out ADR-0049's hour. That costs one poll boundary
(≤5 minutes at `5m`) and it is the only difference from the live invocation other
than `--out`. Under a shut venue it primes warmup and trades nothing, which is
success, so the target says that up front and then checks the warmup line itself
rather than leaving a zero-bar exit looking like a pass.

## Consequences

The launch is one command with a preflight in front of it and a rehearsal available
any time. Two of these targets have safety value beyond convenience: `paper-live`
cannot produce a colliding `--out`, and `paper-stop` cannot send `kill -9`.

None of it runs in CI, nothing is added to a required job, and `make check` is
untouched. `scripts/` is linted by `ruff` (which runs over `.`) but not type-checked
— `mypy` is configured with `files = ["src", "tests"]` — so the preflight is written
plainly and asserts nothing about SDK types.

What this does not do: it is not supervision. There is still no restart, nothing
watches the session, and a machine that sleeps still ends the run — EPIC-86.
`paper-status` is `tail`-shaped on purpose (artifacts, `paper_state.json`, the last
console lines) because the dashboard for a running session is KAN-712 and is
deliberately deferred. And the preflight cannot check the two things most likely to
ruin the night: that the machine will stay awake, and that the operator remembered
to launch detached. It prints both as reminders, which is all a read-only check can
honestly do.
