# ADR-0035: A paper poll survives a bad symbol — retry forever, escalate, never quarantine

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

ADR-0032 made a *backtest* tolerate a symbol that yields nothing: `load_series`
fetches per symbol inside a guard and reports each miss as an `AbsentSymbol`. It
also recorded, in its own consequences, the gap it left behind —
`RecentWindowFeed.poll` still fetched in an unguarded loop, so one bad symbol
aborted a whole paper poll.

The asymmetry matters more than the shared bug does. A backtest that dies gets
re-run: the data is historical, the run is reproducible, nothing is lost but
minutes. A **live paper session that dies is gone** — it is a forward,
wall-clock-paced experiment, and the whole point of running it is that its results
are survivorship-free precisely because you cannot re-run it (ADR-0027). Losing a
multi-hour session to one ticker's transport error destroys the one kind of
evidence this bench treats as more trustworthy than a backtest.

The live path also makes failure much more likely than the backtest path does. A
paper poll hits a live broker API on every interval boundary — every day, or every
minute at `--interval 1m` — over a session that may run for hours. Over that many
round trips, a transient 500, a socket timeout, or a rate limit is not an edge
case; it is a certainty. ADR-0034 already found one live-only failure of exactly
this shape (a data-plan 403 on the SIP tape).

That is what makes the *duration* the interesting design question. A backtest
fetches each symbol once, so "tolerate the failure" is the whole decision. A
session polls the same symbol hundreds of times, so a second question appears:
after a symbol fails, **what does the next poll do?** A symbol that times out once
and a symbol that 404s forever look identical at the moment of failure and are
completely different facts about the session.

## Decision

**`RecentWindowFeed.poll` guards each symbol, mirroring `load_series` exactly.**
Same `try`/`except Exception` around the single `get_bars` call, same two reason
codes (`no_bars_in_range` / `fetch_failed`), same frozen `AbsentSymbol` record,
same never-catch-`BaseException` rule, same collapse-duplicates-preserve-order
behaviour. The types are *imported from* `trading.engine`, not re-declared —
one convention for one problem, because two ways of reporting a missing symbol
would be worse than the bug this fixes.

**A symbol is never permanently dropped. Every poll retries every requested
symbol.** This is the load-bearing choice. The alternative — quarantine a symbol
after it fails, and stop asking — silently shrinks the traded universe for the
rest of a multi-hour session, which is exactly the failure mode ADR-0032 exists to
prevent. Worse, in a session the shrink is invisible in the result: the equity
curve just quietly stops containing that name, and there is no re-run to compare
against. Retrying costs one API call per symbol per poll, which the poll was going
to make anyway.

**Persistence changes the loudness, not the membership.** The feed counts
consecutive absences per symbol (`absence_streaks`). At
`PERSISTENT_ABSENCE_POLLS = 3` consecutive misses the symbol is *escalated*: it
appears in `persistently_absent`, its `AbsentSymbol.detail` carries the streak, and
the log line moves from `WARNING` to `ERROR`. It is still fetched on the next poll.
So "transient" and "permanent" are distinguished — which is what the operator
needs — without the bench deciding on the operator's behalf that a symbol is dead.
A streak resets to nothing the moment the symbol returns bars, so a blip cannot
accumulate into a false verdict.

**A still-forming bar is not an absence.** A symbol that fetched fine but has no
*complete* bar yet is the normal state at every interval boundary (ADR-0022), not a
missing symbol. Only a raised lookup or a genuinely empty source response counts.
Conflating them would make every intraday poll report the whole universe as absent.

**Total absence is not fatal here, unlike in `Engine.run`.** A backtest where no
symbol yields a bar raises `EmptyUniverseError`, because a run over nothing is a
lie dressed as a flat result. A *poll* where every symbol fails returns an empty
feed, because the paper loop already treats an empty poll as "nothing new yet" and
the next poll may well succeed. Killing a live session on one bad network minute is
the bug, not the fix.

**Reporting is via the feed object plus the log, not via `BarOutcome`.** A poll can
fail with zero new bars — in fact a failed poll usually produces no bar at all — so
a per-bar record structurally cannot carry it. `absent` / `absence_streaks` /
`persistently_absent` are the machine-readable channel (rebuilt every poll), and a
module logger is the human one. Logging is emitted on **state change only**: a
warning the poll a symbol goes missing, an error the poll it turns persistent, an
info the poll it comes back. Repeating one line for a dead ticker on every poll for
six hours is not reporting, it is drowning the signal.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Quarantine after N failures; stop fetching | Silently shrinks a live universe with no re-run to catch it. The exact harm ADR-0032 names. |
| Drop for this poll only, no memory at all | Cannot tell a one-off timeout from a ticker that has been dead for three hours — the whole question this ADR exists to answer. |
| Exponential backoff per symbol | Real value only under rate limits, and it makes *which* bar a symbol is present for depend on failure history. Bars would differ between two runs over the same data. Revisit if a live session actually hits limits. |
| Raise `EmptyUniverseError` when every symbol fails | Turns a total-outage poll into a dead session — the failure being fixed. |
| Surface absences through `BarOutcome` | A failed poll often produces no bar, so the record would be dropped exactly when it matters. Needs an engine change too. |
| Re-declare feed-local absence types | Two conventions for one problem. `AbsentSymbol` already round-trips through a report. |
| Log every absence on every poll | A 1m session with one dead ticker writes ~400 identical lines. The structured record already covers every poll. |

## Consequences

- One flaky symbol can no longer end a paper session. The healthy remainder of the
  universe keeps trading and the session keeps its artifacts.
- A permanently dead ticker costs one wasted API call per poll for the life of the
  session. Accepted deliberately: that call is the retry that lets a recovered
  symbol come back, and it is one call against a broker the poll is hitting anyway.
- The happy path is untouched. A clean poll builds the identical feed it built
  before — same completeness gate, same `lookback` slice, same merge (ADR-0022's
  byte-identical-daily invariant holds, and a test pins the exact feed).
- **The CLI does not print absences yet.** `trading paper` reports per-bar via
  `BarOutcome` and never inspects the feed, so today a dropped symbol reaches the
  operator through the log record only. Wiring `feed.absent` and
  `persistently_absent` into the session summary and `result.json` is a follow-up,
  and it lands in the same place as ADR-0032's still-open "the CLI does not yet
  print `absent`".
- The bench has no logging configuration at all, so these records land on the root
  handler's defaults (`WARNING` and above to stderr) until someone configures one.
  The escalation is visible by default; the recovery `INFO` is not.
- Unchanged: a live session still ends after `max_empty_polls` consecutive polls
  that reveal no new bar, so a *total* outage longer than two polls still stops the
  session — just cleanly, through the existing stop condition, with its artifacts
  finalized (ADR-0033) rather than via a traceback.
