# ADR-0047: A paper poll asks for a window the provider will answer

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

`RecentWindowFeed.poll` asked its adapter for `[datetime.min, now]` — year 1 to
now — and kept the newest `lookback` completed bars. The reasoning was that a wide
net cannot miss anything.

**Alpaca answers that request with an empty response.** Not an error, not a
partial answer: zero bars. Measured against the live paper API on 2026-08-09
(AAPL, IEX, raw), and reproduced independently twice before a line of this was
written:

| start | 1d bars | 5m bars |
|---|---|---|
| `datetime.min` | **0** | **0** |
| `1900-01-01` | 1,516 | 121,662 |
| `now - 20d` | 15 | 1,313 |
| `now - 5d` | 4 | 348 |

So the window was not too *wide* for the free data plan — a year-1900 start is
answered in full. `datetime.min` specifically is what the provider refuses, and it
refuses it at **daily** as well as intraday.

At feed level, the exact object graph the Monday divergence run builds
(`RealAlpacaClient` → `AlpacaAdapter(interval=5m)` → `RecentWindowFeed(WallClock,
interval_is_complete(5m), adjusted=False)`), `poll(["AAAPL","MSFT","NVDA"], 512)`:

```
poll() returned 0 timestamp groups
feed.absent = AAPL, MSFT, NVDA — all reason='no_bars_in_range'
```

Every symbol read absent on every poll. ADR-0035 then does exactly what it was
built to do — records a per-symbol absence and survives — and ADR-0032 classifies
a clean empty answer as `REASON_NO_BARS`, a legitimate delisting-shaped absence.
So the session prints absence warnings that read as a venue data outage, returns
an empty feed, and stops on `max_empty_polls = 2` inside two poll intervals,
having primed no warmup history and submitted no orders. A dry run of the real
Monday command confirmed it end to end: 0 completed bars, `Warmup: no completed
bars were available`, exit after 2 empty polls, all artifacts correctly saying
nothing could be concluded.

**It is not a regression.** `git log -L 52,52:src/trading/data/recent_window.py`
puts `_FAR_PAST = datetime.min` at `805a668` — V5, PR #6, the original paper mode.
It has been latent since the day paper mode existed and only bites through Alpaca.

**It has a second head.** With `--source synthetic --interval 1m`, the same
request makes `SyntheticAdapter` fabricate the whole canonical series from its
1990 epoch. Measured here: **3,724,500 bars per symbol in 27–49 s**, and a
three-symbol `poll(lookback=512)` taking **158.6 s** — a twenty-symbol basket
would spend seventeen minutes per poll at a five-minute cadence, i.e. hang. One
bounded window fixes both.

### Why the whole fast layer was green throughout

`SyntheticAdapter` **clips** a `datetime.min` start to its 1990 epoch, and
ADR-0030 documents that clipping as deliberate — it exists precisely so the paper
feed's `datetime.min` poll stays coherent. `FakeAdapter` filters a range without
caring how absurd it is. Both stand-ins are *more forgiving than the provider*, so
every paper-feed test exercised a start the adapter silently rewrote and the bug
could not appear.

This is ADR-0040's finding again, in a different place: the offline stand-in did
not discriminate, so the guard never did either. The practical consequence is
recorded in the test module's own docstring — **a regression test written against
`SyntheticAdapter` here passes whether or not this is fixed, and is worthless.**

## Decision

### The window is bounded, and sized from `lookback` and the bar interval

`poll` asks for `[self.window_start(now, lookback), now]`, where the span comes
from `fetch_span(lookback, interval)`. A poll discards everything older than the
newest `lookback` bars regardless, so the unbounded request bought nothing and
cost everything.

`window_start` is **public** on purpose. The request is what was broken; a test
asserting on a returned bar count would have passed throughout the outage, so the
regression tests assert on the range the feed actually asked for (the spy pattern
ADR-0029's look-ahead test uses).

### Deriving the multiplier

`lookback × interval` of *wall-clock* time contains far fewer than `lookback`
*trading* bars, and getting this wrong silently truncates the ADR-0042 warmup —
the exact failure this bench spent the previous slice fixing. Two conversions are
paid, in order:

1. **The closed part of the day.** A regular US equity session is 6.5 hours, so a
   5-minute bar is one of ~78 in a day, not one of 288. `512 × 5min` is 42.7 hours
   of wall clock but **6.6 trading sessions**. Skipping this conversion under-sizes
   an intraday window by ~3.7×.
2. **The closed days.** 365 calendar days hold ~252 trading sessions — weekends
   plus the ~9 market holidays a year are the whole difference — so calendar time
   is `365/252 = 1.4484×` session time. Skipping this under-sizes by a further
   ~45%.

`WINDOW_SLACK = 4.0` then sits on top of that *exact* conversion, so a window
would have to be a quarter as dense as a normal market calendar before it
truncated a lookback. Four rather than two because over-asking is nearly free here
and under-asking is not: at every supported interval the result lands near
`4 × lookback` bars, which is one page from any provider (Alpaca's limit is
10,000), while a short window quietly shortens the history a strategy warms up on
and says nothing.

Worked, at the `lookback = 512` default:

| interval | bars/session | span | bars in it |
|---|---|---|---|
| 1d | 1 | ~2,967 days (~8 y) | ~2,050 |
| 1h | 6.5 | ~456 days | ~2,960 |
| 5m | 78 | ~38 days | ~2,960 |
| 1m | 390 | ~7.6 days | ~2,000 |

A `MIN_FETCH_SPAN` of 5 days floors the arithmetic so a tiny lookback at a fine
interval still spans a weekend.

The slack is asserted rather than described: four tests build a realistic calendar
(weekdays only, one session in 25 dropped as a holiday — stricter than the real
one in 29) at 1d/1h/5m/1m and assert the poll returns a **full 512** bars.

### The interval comes from the completeness policy

`RecentWindowFeed` never knew how long a bar was; only its `is_complete` policy
did. Rather than thread a second constructor argument through every caller — one
more thing to keep in sync with the first, and a change to `cli.py`, which another
lane is holding — `interval_is_complete` now returns an `IntervalCompleteness`,
a callable that behaves exactly as ADR-0022 specified and additionally *states*
its `interval`. The feed asks it.

A policy that does not state one — `default_is_complete`, or a market-calendar
policy someone injects later — is read as **daily**, the widest window of the
supported set. The fallback errs wide deliberately: erring wide costs a larger
fetch, erring narrow costs history the strategy needed. A caller who knows better
passes `interval=` to the feed, which also wins over the policy.

This is sizing only. `is_complete` remains the sole judge of which bars a poll
yields, so the interval cannot change what gets traded.

### A floor of 1900-01-01, measured rather than assumed

`window_start` clamps at `EARLIEST_START = 1900-01-01`. That is a bound the
provider **demonstrably answers** (1,516 daily AAPL bars in the table above), no
US equity series predates it, and clamping there means an absurd `lookback` can
never arrive back at `datetime.min` or overflow `now - span`. The span itself is
capped before it becomes a `timedelta` for the same reason; `poll(lookback=10**9)`
asks from 1900 and is asserted to.

### A whole universe going quiet is now loud

The question this ticket raises is whether an adapter returning zero bars for a
range that plainly should contain some deserves louder classification than
`REASON_NO_BARS`. That silence is what hid this for months.

**Implemented, at the poll level.** When *every* requested symbol comes back
absent with `REASON_NO_BARS` — a clean answer containing nothing, no fetch having
failed — the feed logs one ERROR naming **the window it asked for**. Twenty
mega-caps do not delist on the same poll, so the request is the likelier suspect,
and the request is the thing the operator could not see. It fires once per outage
rather than once per poll, on the same state-change discipline ADR-0035 already
uses for absences, and re-arms if the universe recovers and goes quiet again.

**Not implemented: a third reason code.** "The source answered with nothing, and I
do not believe it" is a per-adapter judgement, not a feed one — the feed cannot
know which ranges a given provider considers reasonable. The precedent is
ADR-0040: the yfinance adapter classifies a provider refusal at the boundary,
by exception type, and hands the engine `REASON_FETCH_FAILED`. The equivalent here
is for `AlpacaAdapter`/`AlpacaClient` to refuse an unanswerable range at the seam,
which belongs in the Alpaca data layer another lane is holding. Recorded as
follow-up. Adding a third code to `AbsentSymbol` would also ripple through
`report.py`, `result_to_dict` and the dashboard for a distinction the poll-level
alarm already surfaces.

**Nothing is quieter.** Every per-symbol absence is still recorded, still
retried on every poll, still escalated at three consecutive misses, still exported
on `feed.absent`. Two tests pin it: a genuinely missing symbol is still
`REASON_NO_BARS`, and a symbol whose bars all predate the bounded window is
reported absent rather than silently reached for.

### `--once` is byte-identical, proved rather than argued

`trading paper --once` builds the same feed over an in-memory `FakeAdapter` and a
`FakeClock` parked just past the range, so a bounded start could in principle clip
a replay. It does not, because the CLI already sets `lookback = max(lookback,
distinct_timestamps + 1)`: the window is ~2× the calendar span that many bars
implies, whatever the range. Four `paper --once` invocations were run on this
branch and again with `origin/main`'s `recent_window.py` restored — the ADR-0042
golden invocation, an intraday one, a nine-year daily one, and one with an
explicit small `--lookback 8`:

```
50946899eca0d84d43a65dd096a3a58cd32a1ecad28dc3aff1334bee3f252eaf  daily/equity_curve.csv
f0f5de0990beae2dc28fe49b2c496bf542054522d0c7f98558737fec2321c3a1  intraday/equity_curve.csv
e12248116bffd17b4a677498e66fa3e2a1e83d3791881dca9c0090c86c647229  long/equity_curve.csv
50946899eca0d84d43a65dd096a3a58cd32a1ecad28dc3aff1334bee3f252eaf  lookback8/equity_curve.csv
```

Identical before and after, along with every `result.json`, `paper_state.json` and
(path-normalised) stdout. The first hash is the golden ADR-0042 pinned in
`tests/unit/test_paper_warmup.py`, which still passes untouched.

## Consequences

- **The Monday divergence run is unblocked.** Verified live against the paper
  account on 2026-08-08 18:41 UTC with the venue shut — the real command, twenty
  `@blue20` symbols, 5m, IEX, `--live --divergence`:

  ```
  Warmup: primed 645 completed bar(s) 2026-07-30 14:40..2026-08-07 20:00 as
  history; no orders submitted for them (ADR-0042).
  ```

  645 bars where the same command reported *none* hours earlier, primed in 10
  seconds across the whole basket. **No symbol was reported absent** — the
  summary's `Symbols:` line carries no `contributed no bars` caveat and the
  session log carries no feed warning. The session then did exactly what a shut
  venue should produce: `Processed 0 completed bar(s)`, no orders, all five
  artifacts written, and a divergence verdict correctly refusing to conclude
  anything. Account left as found: $100,000.06, no positions, no working orders.
- **ADR-0042 holds and is now reachable.** The warmup priming has been correct
  since it shipped; it had nothing to prime because the request underneath it was
  impossible.
- **ADR-0035 holds.** Absence classification, retry-forever and escalation are
  untouched; the universe-wide alarm is additive and only ever adds a line.
- **ADR-0022 holds.** `DataAdapter.get_bars` and `Engine._step` still never learn
  the interval; it reaches the feed through the completeness policy that already
  carried it, and is used for sizing a fetch, not for deciding completeness.
- **If the window is still not enough**, the poll returns fewer than `lookback`
  bars and the session warms up short. It is not silent — ADR-0042's warmup line
  reports the count and span it actually primed, so `primed 40 completed bar(s)`
  instead of 512 is visible on stdout and in `paper_session.log` at startup — but
  it is not *checked* either. That check is **KAN-702** ("nothing verifies the
  primed history covers the configured strategy's lookback"), which this slice
  deliberately does not fix and does not make harder: KAN-702 wants to compare
  primed bars against a strategy's declared lookback, and it gets a truthful,
  bounded number to compare either way. The one case that would defeat the sizing
  is a source far sparser than a market calendar — a range holding under a quarter
  of the bars its span implies — which no supported source produces today.
- **Live fetch cost rises from nothing to something.** Twenty symbols × ~2,960
  5-minute bars per poll, once per five minutes, is a page per symbol and well
  inside Alpaca's rate limit. A narrower steady-state window after warmup (the
  first poll needs `lookback`; later ones need a handful of bars) would cut it
  further and is deliberately not done: one window means one code path, and a
  session recovering from a ten-minute gap needs more than the last bar.
