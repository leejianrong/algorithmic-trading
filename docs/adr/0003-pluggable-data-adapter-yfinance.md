# ADR-0003: Pluggable data adapter, yfinance first, with a local cache

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The MVP needs free, keyless US-equity daily history to get moving, but must not
be welded to one provider — Alpaca and CSV sources are already foreseen. Network
data also makes tests slow and flaky and makes runs non-reproducible if the
provider revises history between runs.

## Decision

Define a `DataAdapter` interface that returns a normalized, tz-aware `Bar`
series for `(symbol, interval, range)`. Ship `YFinanceAdapter` as the first
implementation and `CSVAdapter` for supplied files. `YFinanceAdapter` is
read-through cached: on a miss it fetches and writes local parquet/CSV keyed by
`(symbol, interval, range)`; on a hit it reads the cache, so re-runs are offline
and deterministic.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Call yfinance directly in the engine | Welds the engine to one provider and to the network; breaks ADR-0002's clean feed seam. |
| Alpaca as the first source | Requires an API key on day one; yfinance is keyless and lower-friction for the MVP. |
| No cache (always fetch) | Slow, flaky tests and non-reproducible runs; defeats determinism. |

## Consequences

- Buys: zero-friction start, offline/deterministic re-runs, and a clean seam for
  future providers (Alpaca, CSV, others).
- Costs: a normalization layer and a cache with its own correctness concerns
  (invalidation, adjusted vs. raw prices, split/dividend handling).
- Forecloses: nothing — new adapters are additive.
- Now true: the cache must record whether prices are split/dividend adjusted, and
  the adapter must normalize timezones and column names so downstream code never
  branches on provider.
