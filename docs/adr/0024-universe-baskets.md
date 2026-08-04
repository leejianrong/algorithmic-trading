# ADR-0024: Curated stock-universe baskets

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

Every run so far names its symbols as an inline comma list (`--symbols AAPL,MSFT`),
and any sector-cap run (ADR-0019) repeats those names again as a `SYM:sector` map.
That is fine for a two-name demo and error-prone for a diversified book: the two
lists drift, sectors get mistyped, and there is no shared, reviewable definition of
"the universe we trade". A cross-sectional strategy (rank-and-hold-top-K, planned)
needs a real candidate set, not a hand-typed list, and it wants a matching sector
map for the guardrail without transcribing twenty pairs on the command line.

Underneath sits an honesty problem this bench exists to respect. A backtest
universe is only meaningful if paper/live can actually hold it: on Alpaca, a name
must be **tradable** and — for our fractional-share sizing (ADR-0011) —
**fractionable**. Those are per-asset facts owned by the broker, exposed by
Alpaca's `get_asset` (`tradable` / `fractionable` flags). The
`AlpacaClient` seam (ADR-0017/0018) does **not** yet expose `get_asset`, so today
any in-repo list is a human judgement call, unverified against the venue. We want
the convenience of named universes now without pretending the list is
broker-authoritative.

## Decision

Add `src/trading/universe.py`: a small, dependency-free registry of curated,
named baskets kept in the repo.

- A frozen `Basket(name, symbols, sectors)` value and a `BASKETS: dict[str, Basket]`
  registry as the single source of truth.
- `get_universe(name) -> list[str]` and `get_sector_map(name) -> dict[str, str]`,
  each returning a fresh copy and raising a `KeyError` that names the known baskets
  on a miss.
- Seed with one basket, `blue20`: 20 mega-cap, highly liquid US names across 8
  sectors, curated as high-confidence Alpaca-fractionable large-caps.

Wire it into the CLI with a `@name` sigil, additive and backward compatible:

- `--symbols @blue20` expands via `get_universe` (unknown name -> clean exit 2
  naming the known baskets); a plain comma list is unchanged.
- `--sector-map @blue20` returns `get_sector_map` (unknown -> the existing
  ValueError path, surfaced as a clean CLI error); the `SYM:sector,...` form is
  unchanged.

Crucially, the module states the caveat in its own docstring: `blue20`'s
fractionability is a **curation, not a broker fact**. Fractionability +
tradability are authoritative only via `get_asset` at connect-time; the backtest
universe must mirror the broker's tradable + fractionable set, and this list must
be verified against the broker before any live use, not assumed. The `get_asset`
seam extension and a universe builder that filters a candidate basket to
`tradable & fractionable & liquid` are **deferred** (planned as a follow-on
slice); `universe.py` is the seed for it.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Keep hand-typed comma lists only | Drifts against the sector map, invites typos, and gives a cross-sectional strategy no shared candidate set. A named basket is defined once and reviewed in one place. |
| Fetch the universe live from Alpaca at run time | Adds a network/credential dependency to every run and makes offline/synthetic backtests impossible. The `get_asset` filter belongs in a separate, opt-in builder that validates a curated seed, not in the run's hot path. |
| Block the feature until `get_asset` lands | The convenience (named symbols + matching sector map) is independent of broker verification and useful offline today. Shipping the seed now, with the caveat stated loudly, does not foreclose the verified builder. |
| Assert the list *is* fractionable | Dishonest — exactly the flattering shortcut this bench avoids. Fractionability is the broker's fact, and the seam that reads it does not exist yet. The docstring and this ADR say so plainly. |
| A data file (CSV/JSON) instead of a Python module | A typed frozen dataclass gives mypy-checked structure, keeps symbols and sectors in sync by construction, and needs no parser or file I/O. A file source can populate the same registry later. |

## Consequences

- Convenience: `--symbols @blue20 --sector-map @blue20` names a diversified,
  sector-mapped book in two tokens; the sector map can no longer silently drift
  from the symbol list because both come from one basket. Offline-friendly — the
  synthetic adapter generates bars for any ticker, so `@blue20` runs with no
  network.
- Honesty preserved: nothing here claims broker verification. The caveat lives in
  the module docstring (where a reader meets it first) and in this ADR; a stale or
  optimistic curation under-delivers loudly rather than silently.
- Forecloses nothing: the deferred `get_asset` seam and the
  `tradable & fractionable & liquid` universe builder both layer on top of this
  registry — `blue20` becomes their candidate seed, validated live before use.
  More baskets are additive registry entries behind the same two getters and the
  same `@name` CLI sigil.
