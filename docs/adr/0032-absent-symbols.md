# ADR-0032: Absence is data — a universe may outlive or predate its members

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

A real equity universe is not a fixed set. Members list, delist, merge, and get
renamed. The bench could not represent that at all: **one symbol with no bars in
the requested window aborted the entire run.**

`YFinanceAdapter._default_fetch` raised `DataUnavailableError` whenever
`yf.download` came back empty, and `Engine.run` fetched every symbol in a single
unguarded dict comprehension, so the exception propagated straight out.

Measured on the real 2000–2020 yfinance backtest that exposed this:

- A **full-range** `blue20` backtest worked, because the range overlaps every
  symbol's listing — META returns a short series from 2012 rather than nothing.
- A **3-fold walk-forward over the same range aborted outright.** Fold 0's
  in-sample span is `2000-01-01..2005-04-01`, and META (listed 2012), TSLA (2010),
  and V (2008) each return an empty frame for it. One of them raised and killed the
  whole sweep before a single fold completed.

So the bench could backtest a hindsight-selected universe over a range where every
member happened to exist, and nothing else. That is precisely the wrong capability
for a project whose stated next goal (ADR-0027) is a survivorship-bias-free
point-in-time universe — a universe which, *by definition*, contains symbols that
do not span the range.

Two facts made the fix small. First, the engine already handles absence
correctly: `build_feed`'s docstring has always said "a holiday or a late listing is
handled without inventing prices", it keys each timestamp's slice off the bars that
exist, and every strategy iterates that slice rather than the requested universe.
An empty list already flowed through end to end. Only the *raise* was fatal.
Second, the repo already had this exact pattern twice —
`liquidity.screen_by_adv`'s unverified verdicts and `universe.validate_universe`'s
usable/unusable/unverified buckets — so the shape was settled, not open.

The trap: `"yfinance returned no data"` covers both "META did not exist in 2003"
and "AAPl is a typo". Silently returning `[]` for both would turn a mistyped
ticker list into a flat 0% run that looks like a strategy which never traded.

## Decision

**Absence is data; failure is an exception.** `DataAdapter.get_bars`' protocol
docstring now states the contract explicitly: return an **empty list** when the
source has no rows for the symbol in the window; **raise** only when the lookup
itself fails (transport, credentials, unreadable file, malformed response). The
five adapters previously did three different things with no documented rule.

`YFinanceAdapter` follows it: an empty-but-successful `yf.download` returns an
empty frame instead of raising. This distinction is sound rather than assumed —
`yf.download` signals genuine failure *by raising*, so an empty successful response
can only mean the provider has no rows. `DataUnavailableError` is deleted; it was
referenced nowhere else in `src/` or `tests/`.

**Policy lives in the engine, not the adapter.** A new public
`engine.load_series(adapter, symbols, start, end) -> (series, absent)` fetches per
symbol inside a `try`, returns only the symbols that produced bars, and records
every one that did not as a frozen `AbsentSymbol(symbol, reason, detail)`. Two
reason codes, deliberately kept apart:

| Code | Meaning |
|---|---|
| `no_bars_in_range` | The source answered, and had nothing for this window. Not listed yet, delisted, or no history. |
| `fetch_failed` | The lookup itself failed. Something may be broken. |

Conflating them would let a network outage read as a delisting. `BaseException`
is never caught. Duplicates collapse to one fetch; input order is preserved.

**Total absence is fatal; partial absence is reported.** If *no* symbol yields a
bar, `Engine.run` raises `EmptyUniverseError` naming every symbol it could not
find, rather than returning a vacuous flat result. This is what keeps a typo loud
while a late listing stays quiet — the same asymmetry `_apply_liquidity_screen`
already applies when an ADV floor empties the universe.

**The requested universe and the traded universe are both reported.**
`BacktestResult.symbols` still holds what the caller asked for, so every existing
report and `result.json` consumer is unchanged; `BacktestResult.absent` carries the
gaps and a new `traded_symbols` property gives the honest set. A report that quotes
only `symbols` overstates what the run could see.

**A sweep survives a dataless span.** `run_walk_forward` catches
`EmptyUniverseError` per span and records the fold in the existing `unusable_folds`
with a reason; `run_sweep` drops that window's run and reports it in a new
`empty_windows` list. An early anchored fold predating a whole universe's listings
is a fact about the data, not a sweep failure — and this is the case that motivated
the whole ADR.

**Absence is cached.** The raise previously happened *before* `_write_cache`, so
nothing was cached for a failing symbol and — because `cache_filename` keys on
`(symbol, start, end)` and every fold uses a different range — a 6-fold sweep over
a universe with four late-listing names paid two dozen doomed network round trips.
An empty result now caches an empty CSV and is served from disk thereafter.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Adapter returns `[]` for everything, no engine policy | Turns a mistyped ticker into a silent flat run. The loud-typo/quiet-gap asymmetry is the whole point. |
| Keep raising; make callers pre-filter the universe by listing date | Pushes broker/exchange metadata into every caller, needs a listing-date source the bench does not have, and still breaks the moment a source has a gap for another reason. |
| Catch the exception in `Engine.run` inline, no `load_series` | `cli.py`'s `paper --once` path uses the identical comprehension; a shared function keeps one policy rather than two that drift. |
| One `absent` bucket with a free-text reason | A network outage would be indistinguishable from a delisting in a machine-readable report. Two codes cost nothing. |
| Let a dataless fold produce a zero-metric fold, as before | A fabricated fold whose Sharpe is structurally 0.0 reads as a result. An unusable fold with a reason does not. |
| Return `None` instead of raising `EmptyUniverseError` | Pushes an `if result is None` onto every call site and invites it being ignored. A run over nothing is an error. |

## Consequences

- A universe whose members list at different times now backtests, and multi-fold
  walk-forward over 20 years works. `blue20`'s 3-fold walk-forward, which aborted,
  completes.
- **This unblocks the ADR-0027 survivorship work.** A point-in-time
  constituent set requires exactly this tolerance; it was previously impossible.
- The `DataAdapter` contract is now documented where it was silent. `CsvAdapter`
  still raises `FileNotFoundError` for a missing file (a lookup failure — correct
  under the new rule) and returns `[]` for a non-overlapping range (correct too).
- `DataUnavailableError` is gone. Nothing imported it, but it was public-ish.
- A symbol that is *always* absent for a benign reason will be reported on every
  run rather than fixed. The report is the nag; the bench does not prune the
  universe for you.
- The negative cache means a genuinely-empty `(symbol, range)` is never re-fetched.
  If a provider later gains history for that window, the stale empty CSV must be
  deleted by hand — the same staleness the positive cache already has.
- **Amended 2026-08-08:** `RecentWindowFeed.poll` used to fetch without a
  per-symbol guard, so one bad symbol aborted a paper poll. It now applies this
  same treatment, reusing these types and reason codes — plus a retry/escalation
  policy for the long-running case, since a session polls the same symbol
  hundreds of times rather than once. See ADR-0035.
- **Amended 2026-08-08:** the two items this ADR left open are both closed.
  `summarize()` now prints `absent` directly under `Symbols:` — deliberately not
  down with the guardrail counters, because a shrunk universe is not an event that
  happened *during* the run like a clamp, it is a caveat on every figure below it.
  `result.json` carries the same list additively, so `RESULT_SCHEMA_VERSION` stays
  1. And a failing `--benchmark` symbol now warns and continues instead of killing
  a run that had already succeeded: the handler catches `EmptyUniverseError` only,
  which is narrow *because* this ADR funnels every data-shaped failure — bad
  ticker, transport error, not-listed-in-range — into exactly that type, while a
  broken guardrail or sizing crash still aborts, since those make the strategy
  numbers suspect too.
- Still open: `cli.py`'s `paper --once` path fetches through a raw dict
  comprehension and so still dies on one bad symbol — the last place this
  treatment is missing, and the one an operator hits first because `--once` is the
  default. A `paper` session also does not yet surface `feed.absent` /
  `persistently_absent` in its summary or `result.json`, so a symbol dropped
  mid-session reaches the operator through the log only.
