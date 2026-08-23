# ADR-0072: Broaden the cross-sectional universe to the point-in-time S&P 500

- Status: Accepted
- Date: 2026-08-23
- Deciders: strategy developer (project owner)
- Ticket: KAN-639, rescoped 2026-08-16 from "Russell 2000 / S&P 1500" to "S&P 500
  on free data" (no free vendor publishes point-in-time membership for the
  broader indices — ADR-0064 already ruled that out). Builds directly on
  ADR-0064 (`sp500_membership.py`) and ADR-0025 (`cross_sectional`).

## Context

`cross_sectional` (ADR-0025) is this bench's only relative-strength,
cross-equity strategy — it ranks a universe by trailing return and holds the
top-K. Until now its only universe was `@blue20`: twenty of *today's* mega-caps,
picked with full 2026 hindsight. Ranking twenty of the most liquid,
most-analyst-covered names on earth is close to the opposite of where
`docs/algo-trading-notes.md` says a systematic edge is likeliest to survive —
and ADR-0027 already documents that any curated basket like `blue20` is a
survivorship-biased universe by construction: the losers, delistings, and
renames that a real historical universe contained are simply absent.

Broadening to 500 point-in-time-correct names is a materially better
cross-section for a rank-and-hold strategy even though it is not the small-cap
inefficiency the ticket originally wanted (rescoped away, per ADR-0064,
because no free vendor publishes point-in-time Russell 2000/S&P 1500
membership). Priority is medium, not urgent: cross-sectional alpha within the
S&P 500 specifically is expected to be thin and heavily arbitraged, and this
ADR reports what is actually measured rather than reaching for a flattering
number.

**The constraint that makes this worth its own ADR rather than a one-line
basket addition:** ranking *today's* S&P 500 over history is exactly the
survivorship trap ADR-0027 describes, one level up from `blue20` — the removed
names are disproportionately the losers, and excluding them inflates the
result. `sp500_membership.py` (ADR-0064) already solved the underlying
question ("who was actually in the index on a historical date"); this ADR is
about wiring that into the CLI's `@name` universe-selection surface so a
backtest can actually use it, and about being explicit that this is a
**static, point-in-time snapshot resolved once at the run's own start date**,
not a membership that mutates mid-backtest as names are added or removed
(that needs an engine-level mutable universe — KAN-633, a separate, explicitly
deferred card).

## Decision

### `@sp500`: a sigil, not a basket

`universe.py`'s `BASKETS` registry is a `dict[str, Basket]` — a fixed symbol
list with no date context, which is the right shape for `blue20`/`core10`/
`crypto10`/`trend_etfs` (a curated list is supposed to be constant) and the
wrong shape for a query whose answer depends on *when* you ask. So `@sp500` is
**not** added to `BASKETS`; it is special-cased in `cli._parse_symbols`,
resolved via `PointInTimeSP500.from_fixture().members_as_of(as_of)` before
falling through to `universe.get_universe`. `universe.py` is untouched by this
ADR — no import cycle, no new dependency between the curated-basket module and
the PIT-membership module.

`_parse_symbols` grows one optional keyword, `as_of: datetime | None = None`.
Every command that already parses `--from` into a `start` datetime
(`backtest`, `paper`, `sweep`, `gen-data`, and `backtest`'s own
`--baseline-basket`) threads `as_of=start` through; every pre-existing call
is otherwise byte-identical, since the default is `None` and a plain comma
list or an existing `@name` basket never looks at it. `verify-universe` has
no date in scope at all (it only asks the broker what it will trade today,
not what it traded on some past date) and is left un-threaded on purpose — it
gets a clear, dedicated error (below) rather than a silent "as of right now"
fallback, which would have been the exact survivorship mistake this sigil
exists to prevent.

This was a genuine design choice, not the only option: an alternative was
special-casing `@sp500` at each of the four call sites *before* calling
`_parse_symbols`, which would have kept `_parse_symbols`'s signature
untouched at the cost of four near-identical `if symbols == "@sp500":` blocks
instead of one. Generalizing the shared function won on the usual
one-clear-owner grounds — the CLI reference already agreed generalizing was
worth it once more than one strategy could reasonably want the sigil (the
ticket asked for `backtest`/`sweep` explicitly, but `paper` and `gen-data`
share the identical `start = _parse_date(...); tickers = _parse_symbols(...)`
shape, so refusing them the sigil would have been arbitrary rather than
principled).

### No date, no universe: a dedicated error

```
$ trading verify-universe --symbols @sp500
error: @sp500 needs a start date to resolve point-in-time membership
(ADR-0064/0072) and this command has none in scope -- use
backtest/paper/sweep/gen-data, or pass a plain comma list here.
```

A pre-1996 date is refused too, surfacing `PointInTimeSP500.members_as_of`'s
own `ValueError` (ADR-0064's documented coverage floor) as the same clean
exit-2 CLI error every other malformed input gets.

### `--sector-map @sp500` fails the existing way, deliberately

There is no committed sector map for 500 names — `blue20`/`core10`'s hand-built
maps are exactly that, hand-built, and fabricating an approximate GICS mapping
for 500 tickers to make the flag merely *accept* `@sp500` would be worse than
refusing it. `_parse_sector_map`'s `@name` branch calls
`universe.get_sector_map`, which raises `KeyError: unknown basket 'sp500'`
because `sp500` is deliberately never added to `BASKETS` — the exact error a
typo'd basket name already produces, so this needed **no new code**:

```
$ trading backtest ... --sector-map @sp500 --max-sector-exposure 0.3
error: "unknown basket 'sp500'; known baskets: blue20, core10, crypto10, trend_etfs"
```

### Scope cut, stated plainly

This is a **static snapshot**: `@sp500`'s membership is resolved once, at
`_parse_symbols` time, from the run's `--from` date, and held fixed for the
whole backtest — exactly like `@blue20` is fixed for the whole backtest, just
resolved from history instead of from a hard-coded list. A *dynamic* universe
that adds/removes names mid-run as the real index reconstitutes is explicitly
**out of scope** here: the engine has no concept of a universe that changes
size partway through a run (every symbol in `context.history`/`bars` is
assumed tradable for the run's whole span), and building that is an
engine-level change, not a CLI-level one. That is KAN-633, tracked separately
and deliberately not attempted in this card.

## Measurement

Real `--source yfinance` run, no synthetic substitute for the headline
numbers (the CLI-level sigil-resolution mechanism is unit-tested offline
against a fake two-change fixture in
`tests/unit/test_cli_sp500_universe.py`, per the ticket's guidance that a fast
test does not need 500 real symbols to prove the mechanism):

```
trading backtest --strategy cross_sectional --symbols @sp500 \
  --source yfinance --from 2015-01-01 --to 2023-01-01 \
  --benchmark SPY --diversified-baseline
```

`cross_sectional` defaults throughout (`lookback=120`, `top_k=8`, `weight=0.9`,
`rebalance_days=21`), default $1,000 cash and cost model, real yfinance data,
2015-01-01..2023-01-01 (eight years spanning the 2018 selloff, 2020 COVID
crash, and 2022 drawdown).

### `@sp500` resolves a real, historical, non-hindsight universe

`@sp500` as of the run's own `--from` (2015-01-01) resolves to **499**
constituents — a mix of names still trading today (AAPL, JPM, HD) and names
long gone from any current list (MON — Monsanto, acquired by Bayer 2018; RAI —
Reynolds American, acquired by BAT 2017; AGN — Allergan, acquired by AbbVie
2020; dozens more). Compare
against the fixture's own "now" (its last change date, 2026-06-30): **503**
current constituents, of which only **311 (61.8%)** were also in the index on
2015-01-01 — 188 names have left the 2015 list and 192 names not present in
2015 have joined. That turnover *is* the survivorship trap this ADR closes:
a `--symbols` list built from today's constituents and backtested over
2015-2023 would trade a universe selected with 11 years of hindsight about
who would still be standing.

### The absence rate — the free-data cost of point-in-time correctness

Of the 499 PIT-2015 names, **122 (24.4%) contributed no bars** on
`--source yfinance` over 2015-2023 — the residual, unfixed half of ADR-0027
(`sp500_membership.py`'s docstring names this exactly: it fixes *selection*,
not *price history for a name yfinance no longer serves*). Every one manually
spot-checked from the absent list is a real acquisition/merger/rename (PETM —
PetSmart LBO'd private 2015; RHT — Red Hat, acquired by IBM 2019; WFM — Whole
Foods, acquired by Amazon 2017; SNI — Scripps Networks, acquired by Discovery
2018; TWC — Time Warner Cable, acquired by Charter 2016; XLNX — Xilinx,
acquired by AMD 2022 — the full 122-name list is in `run1.log`, reproducible
from this exact command), consistent with ADR-0064's own finding that S&P 500
removals are overwhelmingly acquisitions, not bankruptcies. Running the
*same* strategy over *today's* 503-name membership instead, same date range:
only **11 (2.2%)** contributed no bars — an **11x** lower absence rate,
because most of today's constituents have traded under the same ticker the
whole time. This direction and rough magnitude matches ADR-0064's own
2007 measurement (34-48% PIT absence vs. 8-18% for today's membership,
sampled 50-at-a-time) on a full, non-sampled, different-era universe — the
finding replicates.

Two honest caveats this run surfaced that ADR-0064's sampled 50-name draws
did not happen to hit.

**A real ticker-notation mismatch, quantified.** Two of the 122 absent
names — `BRK.B` (Berkshire Hathaway B) and `BF.B` (Brown-Forman B) — are
*not* actually absent from yfinance at all: they are share-class tickers the
Wikipedia-derived fixture spells with a dot (`sp500_membership.py`'s source
convention, ADR-0064), while yfinance's own symbol convention uses a hyphen
(`BRK-B`, `BF-B`). Confirmed directly: `yf.download("BRK-B", ...)` returns 20
rows for the same window `yf.download("BRK.B", ...)` returns zero for.
`universe.py`'s own curated baskets already use the hyphen spelling (its
module docstring calls out `BRK-B`/`BF-B` by name for exactly this reason),
so this is specific to names resolved through `sp500_membership.py`'s fixture
format, not a defect this card introduces into the CLI's existing baskets.
That is **2 of 499 (0.4%)** of the requested universe, and **2 of 122
(1.6%)** of the reported absences, misclassified as "no bars" when the real
cause is a spelling convention mismatch between two data sources — a small
but concrete, non-hypothetical gap. Fixing it (normalizing share-class dots
to hyphens before handing symbols to `YFinanceAdapter`) is a natural,
scoped follow-up and is deliberately **not built here**: it touches ticker
normalization across the fixture/adapter boundary, a different concern from
wiring the sigil into the CLI, and two names out of 499 does not change any
conclusion in this ADR.

**A provider hiccup, not classified as one.** At least one symbol in the
absent-122 list, `BK` (Bank of New York Mellon — unambiguously a
currently-listed, actively-traded S&P 500 member, unlike `BRK.B`/`BF.B`
above), came back a plain yfinance HTTP 404 ("Quote not found for symbol:
BK") rather than a genuine "not listed in this window" absence, confirmed by
hand with a direct `yfinance` call outside this bench, run twice, both 404.
`probe_refusal`'s classifier (ADR-0032/0040) only recognizes
`YFRateLimitError` as a provider refusal; a plain 404 for a name that
unambiguously has data reads as `REASON_NO_BARS`, identically to a true
historical delisting. This is a pre-existing classification gap, not
something this card introduces or fixes — noted here because it means the
24.4%/2.2% absence figures both have a small, unquantified amount of
"provider had a bad moment" mixed in with genuine historical absence, and
the *direction* is the same for both arms (any name can 404 on a given
session, including a `today`-membership name — the 11/503 count is not
immune), so it does not change the 11x comparison's conclusion.

### The halt-latch confound, again — and the honest headline number

The unmodified default posture (no `--halt-cooldown-bars`) halts on
2018-12-17 (20.4% drawdown ≥ 20.0% max) and — per ADR-0031/0055's already
documented mechanism — **never re-arms for the remaining four years of the
run**, the same failure ADR-0031 first found on this exact strategy over
`@blue20` and ADR-0055/0070 later found on a crypto posture and on
`trend_following`. Here it recurs on `cross_sectional` again, just over a
25x-larger, point-in-time-correct universe instead of twenty mega-caps —
evidence the mechanism is universe-agnostic within a strategy, not only
strategy-agnostic within a universe. Reporting the confounded number as *the*
headline would misattribute a guardrail artifact to the strategy or the
universe:

| Run | Universe | Halt | Total return | Sharpe | Max DD | Absent |
|---|---|---|---|---|---|---|
| 1 | `@sp500` (PIT, as of 2015-01-01) | latched (default) | +38.74% | 0.43 | 23.03% | 122/499 (24.4%) |
| 2 | `@sp500` (PIT, as of 2015-01-01) | `--halt-cooldown-bars 21` | **+621.94%** | **0.88** | 27.25% | 122/499 (24.4%) |
| 3 | today's membership (2026-06-30 snapshot) | `--halt-cooldown-bars 21` | +292.92% | 0.91 | 29.85% | 11/503 (2.2%) |

Run 2 vs. Run 1 is the same halt-latch story ADR-0031/0055/0070 already
established: one guardrail artifact separates a middling +38.74% from a
+621.94% run that clears both comparisons (SPY +115.83%, `core10`
`equal_weight` baseline +64.51%) by a wide margin — 2 halt episodes, both
re-armed, 0 in force at the end. Run 2 vs. Run 3 is the actual
universe-selection comparison this ADR is about, run fairly (same
`--halt-cooldown-bars 21` on both so the halt-latch confound cannot leak into
a universe conclusion): **PIT-2015 (+621.94%, Sharpe 0.88) outperforms
today's-membership (+292.92%, Sharpe 0.91) on total return, essentially ties
it on Sharpe, and both clear SPY and the diversified baseline comfortably.**

**This does not confirm the "expected" direction (today's hindsight-selected
universe should look artificially better), and that is itself the honest
finding — it is the same instability ADR-0064 already reported and refused to
paper over with a single confident number**: at N≈500 over one eight-year
window, idiosyncratic stock-picking outcomes for a rank-and-hold-top-8
strategy swamp whatever systematic survivorship effect exists in the *return*
column, exactly as ADR-0064's two 50-name seed draws disagreed in sign. The
**absence rate**, not the return comparison, is the number this measurement
actually supports with confidence — it is stable, has an obvious causal
mechanism (real corporate history, spot-checked by name), and replicates
ADR-0064's own finding at a different sample size and era. Per ADR-0064's own
stated policy, do not cite "PIT costs/gains N% total return" from this run —
cite the absence-rate finding, and treat the return numbers above as one
noisy draw over one strategy, one universe pair, one date range.

The ticket's rescoped priority is medium precisely because cross-sectional
alpha within the S&P 500 is expected to be thin and heavily arbitraged; both
arms comfortably beating SPY and the diversified baseline over this
particular window is a real result, but eight years and one strategy
configuration is not evidence the edge survives a genuinely fair,
out-of-sample test (`sweep --folds` per `docs/research-playbook.md`) — that
is future work this card does not attempt.

## Alternatives considered

| Option | Why not |
|---|---|
| Add `sp500` as a fifth entry in `universe.BASKETS` | Wrong shape: `Basket` is a fixed `tuple[str, ...]` with no date parameter, and the whole point of this card is that the answer is date-dependent. Would need either freezing it at today's membership (the survivorship bug) or bolting a date parameter onto a type every other basket doesn't need. |
| Build a fully dynamic, mid-backtest-reconstituting universe now | Explicitly out of scope per the ticket; needs an engine-level mutable universe (KAN-633), a materially larger change than a CLI universe-selection sigil. |
| Thread `as_of` into every `_parse_symbols` call site by hand instead of a default parameter | Four (five, counting `--baseline-basket`) near-identical call sites re-deriving the same "is this @sp500, do we have a date" branch, instead of one function owning it once. |
| Silently resolve `@sp500` against "now" when no date is in scope (`verify-universe`) | The exact hindsight mistake ADR-0027 describes, just moved one layer down — a broker-verification check for "the universe I'm about to trade historically" quietly substituting today's membership. Refused with a named error instead. |
| Fabricate an approximate sector map for 500 names so `--sector-map @sp500` "works" | Manufactures false precision `blue20`/`core10`'s hand-checked maps do not have. The existing "unknown basket" error is honest; a made-up mapping would not be. |
| Extend to Russell 2000 / S&P 1500 anyway, approximating point-in-time membership from index-fund holdings snapshots | Considered and rejected by ADR-0064 already, for the same reason: no free, point-in-time source exists, and approximating from a single index fund's periodic disclosure (not a continuous PIT record) would be worse than not building it. Not re-litigated here. |

## Consequences

- `cross_sectional` (and, incidentally, any strategy) can now be backtested
  over `@sp500` — a genuinely historical, ~500-name universe resolved at the
  run's own start date — through `backtest`, `paper`, `sweep`, and `gen-data`,
  and as `backtest --baseline-basket @sp500`.
- The survivorship-selection fix ADR-0064 built as a library module is now
  reachable from the CLI a strategy developer actually runs; `universe.py`
  and `sp500_membership.py` remain independent of each other structurally
  (no new import edge either way).
- The residual price-data gap ADR-0064 already measured and documented (free
  yfinance cannot serve delisted-name history, so a PIT universe still loses
  a real, non-trivial fraction of its constituents to "no bars in range") is
  **unchanged** by this card and applies in full to every `@sp500` run — see
  the Measurement section above for what fraction that was on this specific
  run.
- `--sector-map @sp500` is unsupported, by design, with a pre-existing clean
  error rather than a new one.
- A fully dynamic, engine-level mutable universe remains unbuilt (KAN-633).
- Two measured, un-fixed gaps this card's real run surfaced (both pre-existing,
  neither introduced here): a ticker-notation mismatch between
  `sp500_membership.py`'s Wikipedia-derived dot spelling and yfinance's own
  hyphen spelling for share-class tickers (`BRK.B`/`BF.B` vs. `BRK-B`/`BF-B` —
  2 of 499 names on the 2015-01-01 snapshot), and `probe_refusal`
  (ADR-0032/0040) not distinguishing a plain HTTP 404 from a genuine
  historical absence, observed once on `BK`. Both are named, quantified where
  possible, and left for a future card — see the Measurement section.

## See also

- `docs/adr/0064-point-in-time-sp500-universe.md` — the underlying membership
  reconstruction this ADR wires into the CLI; its measured absence-rate
  finding (a 2007 PIT sample lost 34-48% of its names to missing yfinance
  price history, vs. 8-18% for today's membership sampled the same way)
  applies to any `@sp500` run and is not re-derived here except as a direct
  comparison for this ADR's own date range (see Measurement).
- `docs/adr/0025-cross-sectional-rank-hold.md` — the strategy this universe
  primarily targets, unchanged by this card.
- `docs/adr/0027-survivorship-bias.md` — the general mechanism this addresses
  one index further.
