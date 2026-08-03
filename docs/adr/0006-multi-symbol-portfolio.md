# ADR-0006: Multi-symbol portfolio from the start

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The bench is a stepping stone toward trading real capital, and real capital is
almost always diversified across several positions. Single-symbol accounting is
simpler, but retrofitting a portfolio later would ripple through the broker,
portfolio, metrics, and the strategy API — the most expensive kind of change to
make after strategies already exist.

## Decision

Model a multi-symbol portfolio from day one. The engine advances one timestamp at
a time, assembles the set of that day's `Bar`s across the strategy's universe,
and calls `Strategy.on_bar(ts, bars_by_symbol, context)`. `Portfolio` holds
positions keyed by symbol plus cash; equity, exposure, and all metrics are
computed portfolio-wide.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Single symbol per run | Simplest, but no diversification and a real cross-cutting refactor later — exactly when strategies exist to break. |
| Single now, "designed for multi" | Collections-shaped types without exercising them tend to hide single-symbol assumptions until the day you add the second symbol. Better to run multi from the first test. |

## Consequences

- Buys: diversified strategies, portfolio-level risk (exposure, correlation-aware
  sizing later), and no accounting refactor before going live.
- Costs: the engine must time-align bars across symbols (handling symbols with
  missing days), and sizing/guardrails operate on a portfolio rather than a
  single position — more logic to test up front.
- Forecloses: nothing; a single-symbol run is just a universe of one.
- Now true: the feed must yield a coherent per-timestamp cross-section, and tests
  must cover at least two symbols to exercise portfolio accounting.
