# ADR-0049: A live session's stop condition is a duration, not a count of polls

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

`PaperSession.run` ends a session after `max_empty_polls` consecutive polls that
reveal no *new* bar. The parameter defaults to `2`, and `trading paper` overrode it
on exactly one path:

```python
run_kwargs: dict[str, int] = {}
if live:
    ...  # nothing passed -> the default 2
else:
    ...
    run_kwargs = {"max_empty_polls": 1}  # --once
```

So the mode that runs for weeks unattended inherited a default written for a bounded
offline replay, and there was no way for an operator to say otherwise (KAN-671).

**A count is the wrong unit.** `max_empty_polls` counts *polls*, and a session polls
on its bar boundary (ADR-0022), so what `2` means depends entirely on `--interval`:
ten minutes at `5m`, two days at `1d`. That is why the card carried two symptoms that
look unrelated — a daily session dying over a weekend and an intraday session dying on
a brief data gap. They are the same bug seen at two cadences.

Both were measured on the real live wiring assembled offline (`RecentWindowFeed` +
`interval_is_complete`/`default_is_complete` + `PaperSession` + a `FakeClock` that
advances on `sleep_until`), against `main` at `e1192e4`:

```
5m live session, bars stop at 20:00 UTC (16:00 ET)
    -> exited 20:10 UTC, 77 live bars traded
5m live session, 20-minute data gap at 15:00 UTC (11:00 ET)
    -> exited 15:10 UTC, 17 live bars of a 77-bar day
1d live session started Thu 2026-08-06
    -> exited Mon 2026-08-10 00:00 UTC, before Monday's session happened at all
```

The middle line is the one that matters. The runbook for the Monday divergence run
already warned "if it exits at 11:00, that is a feed problem" — it was, but the feed
problem only had to last ten minutes, and the session threw away the remaining 60 bars
of the day rather than waiting it out.

The session stops *cleanly*: ADR-0033 and ADR-0043 mean every exit finalizes and
writes the artifacts. Nothing is corrupt. What is lost is the rest of the day, and a
live session's whole reason to exist is the measurement it collects over a full day
(ADR-0038) — the one survivorship-free evidence this bench has (ADR-0027).

## Decision

**Express the tolerance as wall-clock silence and convert it at the session's poll
interval, floored at a small number of polls. Choose it at the CLI, where the
live/replay distinction already lives.**

```python
LIVE_SILENCE_TOLERANCE = timedelta(minutes=60)
MIN_LIVE_EMPTY_POLLS = 4


def silence_tolerance_polls(poll_interval, *, tolerance=..., minimum=...) -> int:
    if poll_interval <= timedelta(0):
        return minimum
    return max(minimum, ceil(tolerance / poll_interval))
```

| `--interval` | polls | silence tolerated | what binds |
|---|---|---|---|
| `1m`  | 60 | 60 minutes | the duration |
| `5m`  | 12 | 60 minutes | the duration |
| `30m` |  4 |  2 hours   | the floor |
| `1h`  |  4 |  4 hours   | the floor |
| `1d`  |  4 |  4 days    | the floor |

`trading paper --live` passes the derived value; `--once` keeps its explicit `1`; a
new `--max-empty-polls N` overrides either. `max_polls = 100_000` remains the outer
backstop, and the loop itself is unchanged.

### Why generously, and toward stopping late

The two errors are not symmetric. Stopping late costs a handful of extra polls against
a venue that is shut — twelve polls of twenty symbols on Monday, against a rate limit
of 200 requests a minute. Stopping early costs the whole day's measurement and there
is no way to get it back. When one error is cheap and the other is total, tune toward
the cheap one.

### Why a floor as well as a duration

At `30m` and coarser, 60 minutes converts to fewer polls than the old default of 2.
Without a floor this change would have been a *regression* at exactly the cadence the
weekend bug lives at. The floor of 4 was checked against the calendar rather than
assumed:

- A normal weekend is **2** quiet daily polls (no Saturday bar, no Sunday bar).
- A three-day weekend is **3** — the Monday holiday adds one, and Wednesday's poll
  reveals Tuesday's bar and resets the count.

So 4 clears both, correcting the estimate this slice started from (which expected a
three-day weekend to end the session). Four consecutive non-trading days would still
end a daily session; that is a market closure, not a calendar routine, and it is
documented rather than handled.

### Why not the market calendar

A calendar that knows the venue is shut is the right long-term answer to "sleep until
the next open", and it is **KAN-687**. It is the wrong thing to put on the critical
path two days before the Monday run: it needs a new provider dependency, and a
calendar is a thing that can be *wrong*. A half-day it does not know about
(Thanksgiving Friday, July 3) would end the session early — the exact failure being
fixed — with more machinery available to be wrong. The feed's completeness policy is
already injectable (`CompletenessPolicy`), so the seam is waiting.

### Why the CLI chooses, not the loop

`PaperSession.run`'s own `max_empty_polls: int = 2` default is deliberately unchanged.
Deriving it inside the loop from `self._poll_interval` would retune every existing
caller — every fast test, every offline demo — for a scheduling reason, and it would
hide the policy from the place the live/replay decision is actually made. The engine
gains a pure function and nothing else.

### The operator override

`--max-empty-polls N` exists because the card's complaint was that there was **no live
override at all**. It makes the policy inspectable (`--help` states the derivation) and
lets an operator who knows their venue choose. Values below 1 are refused with exit 2:
`0` would break on the first quiet poll, which is a silent way to do nothing.

The live path also *announces* the policy on startup, on stdout and in the ADR-0043
log:

```
Stops after 12 consecutive poll(s) with no new bar — 1 hour of silence at 5m.
```

An unattended session that ends by policy must be distinguishable from one that hung
or crashed, and the operator needs to know that before it happens, not after.

## Consequences

**Monday's run ends about an hour later.** `docs/monday-divergence-run.md` documented
a self-termination at 16:10–16:15; it is now about 17:00–17:05, and the runbook says
so, says the session is silent by design in between, and says an exit before 17:00 is
now a real feed problem rather than a ten-minute hiccup. Nothing else about the run
changes: same bars, same fills, same artifacts, same order flow.

**`--once` is byte-identical**, proved rather than argued. The same invocation run
against `origin/main` at `e1192e4` and against this branch produces five artifacts with
identical SHA-256 digests (`diff -r` clean) and identical stdout. The equity curve is
still `50946899eca0d84d43a65dd096a3a58cd32a1ecad28dc3aff1334bee3f252eaf`, the golden
ADR-0042 pinned from `dbb845f`, and that assertion is now made in two test files.
One deliberate exception, disclosed: the ADR-0043 lifecycle line on **stderr** gains a
`max_empty_polls=1` field. It is a log record, not an artifact, and it already carried
a timestamp and a path, so it was never byte-stable; hiding a now-configurable value
from the replay path would be worse than printing it.

**The backtest path is untouched.** `Engine.run` has no empty-poll concept — `grep -n
max_empty_polls src/trading/engine.py` hits only `PaperSession.run` and its docstring.
The diff to `engine.py` is two hunks, both below `class Engine`: a module-level
constant pair plus `silence_tolerance_polls`, and five lines of docstring on
`PaperSession`. No executable line inside `Engine`, `_step`, `Engine.run`, `_finalize`
or `_RunState` changed.

**This is not ADR-0035.** An *empty poll* (the feed revealed no new timestamp) and an
*absent symbol* (a symbol returned no bars at all) are different conditions with
different handling, and this slice touches only the first. `data/recent_window.py` is
unmodified by this slice (ADR-0047 landed in the same batch and owns that file); a
genuinely absent symbol is still escalated exactly as loudly as before,
still never quarantined, and a poll where every symbol fails still returns an empty
feed — which now buys an hour rather than ten minutes before the session gives up on
it. That is the intended interaction: ADR-0035's premise was that "`max_empty_polls`
still stops a real outage cleanly", and it still does, just after a duration that means
the same thing at every cadence.

**A dead feed still ends the session.** The backstop is a backstop: a daily session
whose bars stop on a Friday exits the following Wednesday, and an intraday one an hour
after its last bar. Both are pinned by tests, so this cannot quietly become a run that
never terminates.

**Known gaps.**

- Nothing tells the operator *why* the session stopped. `PaperSession.run` has no
  logger and does not distinguish "silence" from "`max_polls`" at the exit; the CLI's
  closing line reports only how many bars were processed. The startup announcement is
  the compensating measure.
- The floor is a floor on *polls*, so at `1m` the tolerance is 60 minutes while at
  `1h` it is 4 hours. That is deliberate but it means the policy is not one duration
  everywhere — it is a duration with a minimum sample count.
- Four consecutive non-trading days end a daily live session. KAN-687.
