# ADR-0053: A 24/7 daily bar closes at UTC midnight

- Status: Accepted
- Date: 2026-08-11
- Deciders: strategy developer (project owner)

## Context

`RecentWindowFeed`'s default completeness policy is `default_is_complete`, which
calls a daily bar finished once the clock's UTC **date** is strictly later than the
bar's:

```python
return now.astimezone(UTC).date() > bar.ts.astimezone(UTC).date()
```

That is a **session** rule. It asks whether the venue's calendar day has turned
over, and it is coherent precisely because the venue closes: a US session ends at
20:00/21:00 UTC, so the date rollover always lands *after* the real close and the
rule errs late, never early. A market that never closes has no session to ask
about. Its daily bar is just a rolling 24-hour window, and which instant that
window closes on is a convention someone has to choose — so choosing it by
inheriting the equity path's date comparison is choosing by accident.

`interval_is_complete` (ADR-0022) needs no calendar at all: a bar with START `ts`
covers `[ts, ts + interval)` and is complete at `ts + interval`. That is already
correct for any market at any sub-daily interval, whether or not the venue closes.
So the whole question is the daily case.

### The first thing measured: is a new policy needed at all?

No. `interval_is_complete(timedelta(days=1))` expresses "a rolling 24-hour window
closing at UTC midnight" exactly, and for a bar stamped at UTC midnight it is
**indistinguishable** from `default_is_complete`. Swept minute by minute across
three days (4,320 evaluations, both sides of the boundary):

| daily bar stamped at | minutes the two rules disagree |
|---|---|
| 00:00 UTC | **0** |
| 04:00 UTC | 240 |
| 08:00 UTC | 480 |
| 13:00 UTC | 780 |

And the disagreement runs in **one direction only**: where they differ,
`default_is_complete` says complete and `interval_is_complete(1d)` does not. The
count is exactly the stamp's offset from midnight in minutes. So the session rule
declares an off-midnight daily bar complete *early*, by that offset — which for a
24/7 venue means handing the strategy a bar that is still forming, the thing the
completeness gate exists to prevent (ADR-0014).

That makes the choice free rather than a trade-off: under the convention the two
rules are the same function, and where the convention is violated the rolling-day
rule is the safe one.

### The second thing measured: `fetch_span`'s equity calendar

`fetch_span` (ADR-0047) hardcodes `RTH_SESSION = 6.5h` and
`CALENDAR_DAYS_PER_SESSION = 365/252`. Both are wrong for a market with no closed
hours and no closed days. Measured at `lookback = 512` against the `512 × interval`
a continuous source actually needs:

| interval | span asked | 24/7 need | over-ask | bars in the window (24/7) |
|---|---|---|---|---|
| 1d | 2,966.4 d | 512 d | **5.79×** | 2,966 |
| 1h | 456.4 d | 21.33 d | **21.39×** | 10,953 |
| 5m | 38.03 d | 1.78 d | **21.39×** | 10,953 |
| 1m | 7.61 d | 0.356 d | **21.39×** | 10,953 |

Wide, at every interval — which is the direction ADR-0047 deliberately tuned
toward, because a short window silently truncates the ADR-0042 warmup while an
over-wide one costs a fetch. The sub-daily factor is exactly
`(24/6.5) × (365/252) × 4`.

## Decision

### The convention: a 24/7 daily bar is a rolling 24-hour window closing at UTC midnight

Stated, documented in `recent_window.py`, and pinned by tests. UTC midnight because
it is the only anchor that is not somebody's local business day, and because it is
the anchor under which the existing daily rule and the interval rule coincide — so
adopting it costs no behaviour change anywhere.

### No new policy. A continuous market drops the daily special case

The 24/7 daily rule is `interval_is_complete(timedelta(days=1))`, which already
exists. Nothing is added to `recent_window.py`'s API — no `continuous_is_complete`,
no market enum, no branch.

This is the reuse rule this repo already follows (ADR-0035 reused `AbsentSymbol`
and its two reason codes rather than declaring new ones). A second name for
`ts + interval` would be a second thing to keep correct, and the ADR-0047 sizing
seam would then have two callables to recognise instead of one.

Concretely, the CLI today reads:

```python
is_complete = interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete
```

A continuous market does not need a third arm on that expression; it needs **no
arms** — `interval_is_complete(freq.delta)` for every interval, daily included.
The equity path keeps `default_is_complete` for daily, and must: for a daily bar
stamped at the session open, `interval_is_complete(1d)` would withhold it until
13.5 hours after the real close, i.e. into the following session's morning. The
right rule genuinely depends on the market, which is why the policy is injectable.

### `fetch_span` is assessed and left alone

No 24/7 branch. Its equity constants err **wide** for a continuous market at every
supported interval (5.79× / 21.39× above), so a continuous lookback cannot be
truncated — the failure mode ADR-0047 exists to prevent — and correctness does not
depend on the calendar it assumes. Changing it would buy a smaller fetch and risk
the expensive error.

One cost is named rather than fixed: at ~10,953 bars per symbol per poll, a 24/7
sub-daily window is **two** provider pages, not the one ADR-0047's slack reasoning
assumed (Alpaca's limit is 10,000 bars). That is a fetch cost on a path nothing
runs yet.

### The axis is completeness, and it stays independent

Nothing in `recent_window.py` learns what kind of market a symbol trades on, and
nothing tries to infer it from the bars. Two reasons. First, it cannot be inferred
safely: an off-midnight daily stamp is normal for US equities (the provider sends
whatever it sends; this repo does not normalise it), so a feed that warned on that
shape would warn on every live daily equity session. Second, the market calendar is
a different lane's type. Joining "which market" to "which completeness rule" is a
later card's job.

### Not wired to the CLI, deliberately

`trading paper` cannot select a 24/7 market today, so no daily feed built through
the CLI reaches the rolling-day rule; a crypto daily feed built through the CLI
would still inherit the session rule. That wiring is a later integration PR, and
this slice deliberately does not touch `cli.py`. What ships is the decision, the
documentation at the seam, and the tests that pin both.

## Alternatives considered

| Option | Why not |
|--------|---------|
| A new `continuous_is_complete` / `crypto_is_complete` policy | It would be a one-line delegation to `interval_is_complete`, i.e. a second name for one mechanism, and a second thing `_policy_interval` must recognise. Measured to be behaviourally identical, so it would add surface and no behaviour. |
| Generalise `default_is_complete` to take an anchor hour | Puts a market-specific parameter on the *equity* rule, and every value other than "the venue closes before the anchor" is unsafe. `ts + interval` is the general rule and it already exists. |
| Warn when a session policy judges an off-midnight daily bar | Would fire on every live daily equity session, where the shape is normal and the rule is correct. A guard that cries wolf on the working path is worse than the documented convention. |
| Anchor the 24/7 day at 00:00 ET (or the venue's own "trading day") | Ties a market with no venue timezone to one exchange's local calendar, and reintroduces DST. UTC midnight is the anchor with no owner. |
| Give `fetch_span` a 24/7 branch | It already errs wide for 24/7, so the branch would only tighten a window whose looseness is the safety property. No measured failure to justify it. |
| Import Lane B's `MarketCalendar` and switch on it | Couples two axes that are genuinely independent — completeness is a bar-length question, not a calendar one — and would make this file depend on a type that does not exist yet. |

## Consequences

- **No behaviour change anywhere.** The production diff is docstrings and comments
  in `recent_window.py`; there is no executable-line change. Verified with hashes
  rather than asserted: `paper --once` (`equity_curve.csv`, `result.json`,
  `paper_state.json`) and `backtest` (`equity_curve.csv`, `result.json`) match the
  `main` @ `cfb4d85` baselines byte for byte.
- **ADR-0022 holds and is where the 24/7 answer came from.** `ts + interval` needed
  no calendar, which is exactly why the sub-daily case was already right for a
  market that never closes. `DataAdapter.get_bars` and `Engine._step` still never
  learn the interval.
- **ADR-0047 holds.** The two daily policies size the fetch window identically
  (`_policy_interval` reads one day from either), so a continuous feed swapping onto
  the rolling-day rule changes which bars are complete and leaves the *request*
  unchanged. Pinned by a test comparing the two requests.
- **ADR-0040's lesson, third sighting.** `SyntheticAdapter` emits weekday-only bars
  inside a 13:30–20:00 UTC session and clips an absurd start; `FakeAdapter` filters
  any range. Neither can represent a market that never closes, so nothing in
  `tests/unit/test_completeness_247.py` uses them — the 24/7 series and clock states
  are built explicitly, and its `_ContinuousAdapter` reproduces the measured Alpaca
  refusal (an unanswerably early start returns **empty**) so the bounded-window
  guard cannot regress on this path either. Both stand-ins' equity-shaped limits are
  pinned by tests in that file, so nobody can "simplify" it back onto them.
- **Known gaps, all deliberate.**
  - **Not wired.** No CLI flag selects a 24/7 market; the seam is proved, not
    reachable. A crypto daily feed built through today's CLI still gets the session
    rule.
  - **Nothing verifies a provider honours the convention.** If a 24/7 source stamps
    its daily bars at 08:00 UTC, the rolling-day rule stays *safe* (it waits for the
    full window) but the bench's "day" is then that source's day, not UTC's. The
    divergence is documented and tested; it is not detected at runtime.
  - **`EARLIEST_START = 1900-01-01` is measured against the equity API only**
    (ADR-0047). Whether Alpaca's crypto bars endpoint answers a 1900 start is
    unmeasured; a clamp there for a symbol whose history begins in 2021 is plausible
    but unverified.
  - **`fetch_span` over-asks by 5.79×/21.39× for 24/7**, and a sub-daily poll costs
    two provider pages rather than one. Assessed above, not changed.
  - **No 24/7 data source exists in this repo yet**, so every number here is
    arithmetic and constructed bars, not a live continuous feed. Nothing is claimed
    to have been observed against a real crypto provider.
