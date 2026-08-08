# ADR-0043: A stopped session still writes its artifacts, and says what it did

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

ADR-0033 established that a live paper session must not lose its run when it is
stopped, and fixed the one exit that existed: `Ctrl-C`. `PaperSession.finalize()`
plus a `except KeyboardInterrupt` in the CLI turn an interrupt into an equity CSV,
a `result.json`, and a printed summary.

That covers exactly one signal. Nothing in `src/trading/` handled any other, and
`grep -rn "signal" src/trading` returned nothing at all before this slice.

**Every other way of stopping a process sends SIGTERM.** `docker stop`,
`docker restart`, `systemd stop`, a VPS reboot, a supervisor's restart policy, and
a plain `kill` all send it, and Python's default disposition terminates the
interpreter without unwinding — no `finally`, no `except`, no `finalize()`. So the
loss ADR-0033 fixed comes straight back through the deployment mechanism, which is
the point: EPIC-86 is about running this bench in a container, and containerising it
without handling SIGTERM would reintroduce the bug *as a consequence of deploying*.

Reproduced against a real process before anything was written — a live synthetic
session, signalled during its post-warmup sleep:

```
$ kill <pid>
rc -15
files ['paper_session.log']
```

`paper_session.log` survives only because it is written line by line as the session
runs. `equity_curve.csv`, `result.json` and the summary — everything the run is
*for* — never happen. Compare the same session sent SIGINT: `rc 0`, all three files.

The second half of the problem is that nobody is watching. Everything this bench
says, it says with `typer.echo` to a terminal. The package had **one**
`logging.getLogger` (`data/recent_window.py`, ADR-0035) and **no** `basicConfig`
anywhere, so the feed guard's escalation warnings landed on the root logger's
last-resort handler with no timestamp, and its recovery line — `"%s is back in the
feed"`, logged at INFO — was written and seen by nobody, ever. An unattended session
has no other channel: when it stops at 11:04 the only question is *why*, and prose
scrolling past in a container's stdout ring buffer is not an answer.

## Decision

### SIGTERM raises; it does not set a flag

A signal handler raises `SessionTerminated`, a **subclass of `KeyboardInterrupt`**,
so the `except KeyboardInterrupt` ADR-0033 already added catches it verbatim. One
exit path, two names for what triggered it. The alternative — a second `except` for
a second exception type — is two paths to `finalize()` that can drift apart, which
is the same argument ADR-0002 makes about backtest and paper.

The considered alternative was a **cooperative stop flag** the `PaperSession` loop
checks each iteration. It is cleaner in the abstract (no exception from a signal
context, no landing mid-call) and it does not work here:

- A live session spends nearly all of its life inside `Clock.sleep_until`, waiting
  out a bar interval. On Monday's `--interval 5m` run that is up to five minutes; at
  `--interval 1h` it is up to an hour. A flag is only read when the loop comes back
  round.
- **Docker sends SIGKILL ten seconds after SIGTERM.** A session asleep for another
  four minutes would be killed before it ever looked at the flag — losing the
  artifacts exactly the way no handler at all does, just with more code.

Raising interrupts the sleep immediately: PEP 475 makes a signalled `time.sleep`
retry *unless* the handler raised, in which case the exception propagates. The cost
is real and accepted — an exception raised from a handler lands at whatever bytecode
boundary the interpreter reached, so it can surface from inside any call the loop is
making. That is survivable only because of the next decision.

### A signal that arrives during finalization is ignored

`_TerminationGuard` is armed for the session loop and **disarmed the moment the loop
is left**. From then on every SIGTERM is logged and dropped, so writing
`equity_curve.csv` and `result.json` cannot be interrupted half way. Truncating the
artifacts that the first signal was honoured in order to save would be strictly
worse than taking a moment longer.

The rule is one sentence: **SIGTERM stops the loop; it never interrupts
finalization.** That covers both the impatient second `kill` and the first signal
landing while a normally-finished session is still writing. Finalization is bounded
work — assembling an in-memory result and writing four small files, milliseconds in
practice, and measured at well under the 10 s grace period in the fast layer — so
the budget is nowhere near binding. `kill -9` remains available to an operator who
disagrees, and the log line says so.

### The handler is installed by `paper`, not by the process, and never by an import

Signal disposition is process-global state, and it belongs to whoever owns `main`.
So the handler goes on inside the `paper` command, for the length of that command,
and the previous handler is restored on the way out. A library import installs
nothing.

It is scoped to `paper` rather than to every command for the same reason ADR-0035
guards the paper feed and not the backtest loader: **a killed backtest is
re-runnable from its inputs; a killed live session is gone**, and a live session is
the only survivorship-free evidence this bench collects (ADR-0027). `backtest` and
`sweep` do not need it and do not get it.

Installation degrades quietly rather than failing: `signal.signal` raises
`ValueError` off the main thread, and a platform may not deliver SIGTERM at all.
Neither is a reason to refuse to trade, so the session logs one warning saying the
guard is unavailable and runs exactly as it did before this ADR.

### The exit code is 0

A session that was asked to stop, stopped, and wrote everything it was asked to
write has succeeded. `docker stop` does not read the code, and the Ctrl-C path has
exited 0 since ADR-0033. Reporting 143 would make an orderly shutdown look like a
failure to any wrapper that checks. Accepted trade-off: a wrapper that wants to
*distinguish* "stopped by signal" from "ran to completion" must read the log, which
now says which one it was.

### Logging: one configuration, owned by the entry point, on stderr

`trading/logging_config.py` holds `configure_logging`, called from the Typer app
callback and nowhere else, with global `--log-level` and `--log-format` options.
Nothing runs at import time, so importing `trading` as a library leaves a host
application's handlers untouched — asserted in a subprocess, because this test
session has already configured logging a dozen times over.

Three choices worth stating:

**Logs go to stderr; the report stays on stdout.** `docker logs` merges both, so
nothing is lost, but a terminal operator keeps the per-bar decision lines
uninterrupted, and the existing `typer.echo(..., err=True)` warnings already live on
stderr. It also means a shell pipeline consuming a run's output never has log lines
injected into it. Buffering was checked rather than assumed: `click.echo` flushes
every line, and `logging.StreamHandler` flushes every record, so both streams appear
in `docker logs` when they happen rather than when a 4 KB buffer fills.

**`--log-level` governs *our* loggers, not the world.** The requested level is set
on the `trading` logger; the root stays at WARNING, which is effectively where it
sits today. Otherwise `--log-level DEBUG` on a live Alpaca run would switch on
urllib3/httpx per-request logging and bury the session in third-party chatter — the
exact drowning this configuration exists to prevent. Raising the level *above*
WARNING does apply to everything: **quieting is global, verbosity is ours.**

**JSON lines are available and are not the default.** `--log-format json` emits one
object per record (`ts`, `level`, `logger`, `message`, and `exception` only when
there is a traceback). A machine-readable log is worth more than a pretty one once
nobody is reading it live, and it keeps a traceback inside a single record instead
of smeared across forty lines that no shipper will reassemble. But Monday's operator
is a human at a terminal, and a format nobody can read at a glance is its own kind
of invisible — so text is the default, and the container image's entrypoint is where
`--log-format json` belongs (EPIC-86). Both stamp **UTC**: `logging` defaults to
local time, which on a container is whatever the image was built with, and every
other timestamp this bench emits — bars, the equity curve, `result.json` — is UTC.

The CLI logs the session's *lifecycle* only — start with its parameters, the warmup
line, the signal, the finish with the artifact directory. Not per bar: those stay on
stdout exactly as they are, because drowning the operator's own report would trade
one invisibility for another.

## What this cost, and what it found

A real bug, found by insisting on the subprocess test rather than an in-process one.
`cli.py` used `logging.getLogger(__name__)`, which is `"trading.cli"` when the module
is imported — as it is under Typer's `CliRunner` — and `"__main__"` when the CLI is
run as `python -m trading.cli`. That name is outside the `trading` tree, so it fell
back to the root's WARNING threshold and **every session lifecycle record vanished**
in exactly the deployment shape this ADR is about. The logger is now named
explicitly, with a comment saying why, and the subprocess test asserts the three
lifecycle records appear in a real process's output.

## Consequences

- **Monday's command is unchanged where it counts.** Both new options default to
  today's behaviour. Run on this branch and on `origin/main`, `paper --once
  --divergence` and `backtest --benchmark` over synthetic data produce identical
  **stdout, exit code, and every written artifact** (`diff -r`). The single
  difference anywhere is two new INFO lines on **stderr** for `paper` — the session
  start and finish records — which is the observability half of this slice arriving,
  not a behaviour change: `backtest`'s stderr is byte-identical, and nothing on
  stdout or on disk moves. What else changes for Monday is that `kill` no longer
  destroys the run. `docs/monday-divergence-run.md` is updated: it previously told
  the operator not to `kill` the process.
- **`RESULT_SCHEMA_VERSION` stays 1.** Nothing about the artifacts changed.
- **The guard is proved by watching it fail.** With the `signal.signal` call
  replaced by a `getsignal` that installs nothing, 8 of 13 tests in
  `tests/unit/test_cli_signals.py` go red — including `returncode == -15` and every
  missing artifact — and go green again when it is restored.
- **Still open.** SIGHUP (a closed terminal on a session started without `nohup`)
  and SIGINT-during-finalization are **not** handled: SIGHUP still kills the process
  outright, and a Ctrl-C while the artifacts are being written still propagates,
  because only SIGTERM's disposition is touched. Both are deliberate — this slice
  fixes the deployment signal, and widening the set is a decision about how many
  ways an operator may be prevented from stopping a process, not a mechanical
  extension.
- **Still open.** The engine itself has no logging: `Engine._step`, the guardrails,
  and `AlpacaBroker` all communicate through return values and `typer.echo`, so an
  unattended session's log records what the CLI knows and no more. A halt, a clamp,
  or a venue refusal reaches the log only via the per-bar stdout line. Adding
  loggers there means touching modules this slice does not own; the configuration
  they would need is now in place and waiting.
- **Still open.** Nothing supervises or restarts a session; SIGTERM handling makes a
  restart *safe*, it does not make one *happen*. That is EPIC-86's job.
