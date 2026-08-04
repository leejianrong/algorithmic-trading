# ADR-0022: Intraday / bar-frequency abstraction

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

The bench began US-equities-daily-only (ADR-0005), which deliberately left the
door open: `Bar` has always carried a full tz-aware timestamp, and `Engine._step`
was already frequency-agnostic. Intraday was named as its own future ADR. Adding
higher-frequency bars must not disturb the two invariants that make results
trustworthy — no look-ahead (ADR-0001) and one execution path shared by backtest
and paper (ADR-0002) — and daily runs must stay byte-identical.

The daily assumption actually lived in four narrow places: the paper feed's
completeness rule (a *date* comparison), the paper clock's cadence (a whole-day
poll), the synthetic generator (one midnight bar per weekday), and metrics
annualization (a hard-coded 252 / √252). Nothing in the engine's backtest loop or
the `DataAdapter` protocol needed to change.

## Decision

**Interval is a property of the adapter, not a call argument.** A new frozen
`Frequency` value type (`frequency.py`) names one cadence: a `label` (`"1d"`,
`"1h"`, `"30m"`, `"5m"`, `"1m"`), a `delta` (bar length), and `periods_per_year`
(the annualization factor). Adapters take a construction-time `Frequency`;
`DataAdapter.get_bars` and the engine's backtest loop are untouched — the engine
just iterates whatever bars the feed yields. `Frequency.parse` resolves the
standard labels and errors clearly on anything else.

**Bar-start timestamp + `ts + interval` completeness.** A bar's `ts` is its START
time; the bar covers `[ts, ts + interval)` and is complete at `ts + interval`.
This matches how daily bars already behaved (a bar dated `D` completes once the
clock passes `D`), so `default_is_complete` and `RecentWindowFeed` are unchanged.
A new `interval_is_complete(interval)` policy factory expresses the sub-daily rule
(`now >= ts + interval`) and is passed to the same injectable feed seam — the
forming intra-session bar is never shown to the strategy, exactly as the daily
gate never showed today's forming bar (ADR-0014).

**Paper cadence generalized, daily unchanged.** `PaperSession._next_due` now
returns the first `poll_interval` boundary strictly after `now`, anchored to the
start of the UTC day. For the daily default (`poll_interval == 1 day`) this is the
start of the next day — byte-for-byte what V5 computed. An optional
`frequency` argument sets the poll interval when no explicit `poll_interval` is
given. `_step`, `Engine.run`, and the order-of-operations are not touched.

**Annualization by frequency.** `metrics.compute` (and every helper that scaled by
252/√252 — Sharpe, annualized return, Sortino, Calmar, turnover) takes a
`periods_per_year: float = 252.0` keyword. The default reproduces every existing
daily number exactly; an intraday run threads its
`Frequency.periods_per_year`. Daily is `252.0`; intraday is
`252.0 * (390 / interval_minutes)` for a 6.5-hour / 390-minute regular session
(9:30–16:00 ET), an explicit modeling assumption documented in `frequency.py`.

**Synthetic intraday generation.** `SyntheticAdapter` takes a construction-time
`frequency` (default `DAILY`). Daily generation is byte-identical to before
(verified bar-for-bar). Sub-daily generation emits bars spaced by the interval
across a nominal session (13:30–20:00 UTC = 9:30–16:00 ET) for each trading
weekday, stamped at each bar's start, with GBM drift/vol scaled to the bar via
`periods_per_year`, deterministically per seed+symbol.

**Real intraday behind integration.** `AlpacaAdapter` gains a construction-time
`interval`; a daily interval keeps routing through `get_daily_bars` (unchanged),
a sub-daily one routes through a new `get_bars(..., interval)` on the
`AlpacaClient` seam (Fake serves whatever bars it holds; Real maps the interval to
`TimeFrame.Minute/Hour/Day`). The offline core is proven in the fast layer; the
real-API path is a network/SDK-gated integration test that skips without creds.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Add `interval`/`frequency` to `DataAdapter.get_bars` | Ripples through every adapter, the engine, and callers; the interval is a fixed property of a data source, so construction-time is the honest home. |
| Bar `ts` = close time | Complicates "history so far" and the no-look-ahead gate; start-time keeps daily semantics (a bar dated `D`) intact and completeness a single `ts + interval`. |
| Gate the forming intraday bar inside the engine | Couples the engine to a wall-clock notion of "now"; the feed already owns completeness (ADR-0014), so a new policy is all that's needed. |
| Keep 252 hard-coded, post-scale Sharpe | A hidden constant that silently misannualizes intraday; a `periods_per_year` parameter is explicit and defaults to today's behavior. |
| A general duration parser (`"2h"`, `"15m"`, …) | More surface than the MVP needs; a small labeled registry covers the intended cadences and fails loudly on the rest. |

## Consequences

- Buys: the whole stack runs intraday offline today (synthetic 1h/30m/5m/1m),
  verified end to end via `Engine.run`; paper mode gains a correct intra-session
  cadence and completeness gate reusing the existing seams; metrics annualize to
  the bar. Real Alpaca intraday slots in behind the same construction-time
  interval when credentials and the SDK are present.
- Costs: `periods_per_year` is now a parameter callers should thread for honest
  intraday figures (the report/CLI wiring is a follow-up); the synthetic session
  window and the 390-minute year are modeling assumptions, not a market calendar
  (no half-days, no auctions).
- Forecloses nothing: a real trading calendar can replace the completeness policy
  and the synthetic session window; tick/other-asset frequencies and the
  `--interval` CLI flag are additive follow-ups on this seam.
- Now true: daily backtests are byte-identical (proven bar-for-bar and by the
  unchanged daily test suite); the four former daily assumptions each live behind
  a named, tested seam.
