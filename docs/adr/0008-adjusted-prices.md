# ADR-0008: Backtest on split/dividend-adjusted (total-return) prices

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

Raw historical prices misrepresent returns in two ways beginners rarely catch: a
stock split shows up as a sudden large drop (a 2:1 split looks like a −50% day),
and dividends simply vanish from a price series, understating the return a holder
actually earned. For a bench meant to measure whether a strategy has real edge,
that distortion is disqualifying.

## Decision

Backtests run on **split/dividend-adjusted** series end-to-end, so reported
returns are total return and corporate actions don't create phantom moves. The
`DataAdapter` returns adjusted bars; the cache records the adjustment so a run is
unambiguous. The (future) paper/live path will instead trade on actual quotes —
adjustment is a backtest-accounting choice, not a claim about the literal price
paid on a given day.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Raw prices | Truest to "what would I have paid," but splits distort history and dividends silently disappear — easy to fool yourself over multi-year tests. |
| Keep both, choose per run | Most correct, but doubles cache/plumbing complexity for a nuance the MVP doesn't need yet; can be added when the live path lands. |

## Consequences

- Buys: honest total-return measurement, no phantom split crashes, dividends
  included — numbers you can compare across strategies and against a benchmark.
- Costs: an adjusted "price" isn't the exact dollar amount tradable on that
  historical day, so fill prices in backtest are total-return-consistent rather
  than literal; the paper/live path will need actual quotes and thus a second
  price notion later.
- Forecloses: nothing; storing raw alongside adjusted is an additive change when
  the real-broker path (ADR-0004) arrives.
- Now true: a test must run buy-and-hold across a known split date and assert no
  phantom crash and dividend inclusion.
