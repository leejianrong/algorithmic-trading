# ADR-0083: A paper session records why it stopped

- **Status:** accepted
- **Date:** 2026-11-12
- **Context ADRs:** 0033, 0043, 0049

## Context

ADR-0049 closed with "nothing tells the operator *why* it stopped". A session ends
for different reasons that read very differently afterwards: silence tolerance
(`max_empty_polls`), Ctrl-C, SIGTERM from a supervisor, a completed `--once` replay,
or a test bound (`max_new_bars`, `max_polls`). A session that exits by policy has to
be told apart from one that was killed.

## Decision

`PaperSession.stop` is a frozen `SessionStop(reason, detail)`, `None` until the
session ends. `reason` is one of `silence_tolerance`, `bar_limit`, `poll_limit`
(set by `run` at its own exits), `interrupted`, `terminated` (set by the CLI via
`record_stop`, because a signal raises out of `run`), or `replay_complete` (the CLI
relabels a `--once` replay's single quiet poll: that is completion, not silence).

Surfaced as `Stopped: <reason> (<detail>)` after the `Processed N completed bar(s)`
line, appended to the `paper session finished` log record, and as a top-level
`stop` object in `result.json` for paper only (omitted when absent, so backtest
bytes do not move; `RESULT_SCHEMA_VERSION` stays 1).

The value is recorded, never read by the loop: when a session stops is unchanged,
and `Engine.run`/`_step` are untouched.

## Consequences

- `--once` is byte-identical in `equity_curve.csv` and `paper_state.json`; stdout
  gains one line, the finished log record gains a suffix, `result.json` gains `stop`.
- Not covered: an unhandled exception (and `kill -9`) propagate with no artifacts, so
  no reason is recorded; the traceback/exit code is the record there.
