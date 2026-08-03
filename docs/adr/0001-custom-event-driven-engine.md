# ADR-0001: Build a custom event-driven backtesting engine

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The test bench is the product, not a means to an end. The owner wants to
understand and control exactly how bars turn into decisions, orders, and fills,
and — critically — to guarantee no look-ahead bias. An event-driven loop that
processes one bar at a time and defers fills to the next bar makes that
guarantee structural rather than a matter of discipline. It also has to be the
same machinery that later drives paper trading (ADR-0002), where events genuinely
arrive one at a time.

## Decision

Build a custom event-driven engine: a single loop that advances a clock, pulls
the next `Bar`, invokes `Strategy.on_bar`, routes the returned orders to a
`Broker`, and marks the portfolio to market. Orders submitted while processing
bar *t* are eligible to fill only from bar *t+1* onward.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Wrap `backtesting.py` | Fast to first result, but the engine is a black box; owning the execution semantics is the point, and its live/paper story is weak. |
| Vectorized engine (`vectorbt`) | Extremely fast for parameter sweeps, but vectorized signals model the market as arrays, not events, so the same code cannot drive paper trading (breaks ADR-0002) and look-ahead is easier to introduce accidentally. |
| `zipline` / `backtrader` | Heavy, largely unmaintained or opinionated frameworks; more surface area to learn than to build for a daily-bars MVP. |

## Consequences

- Buys: full control of fill and accounting semantics, a structural no-look-ahead
  guarantee, and one engine reused for paper trading.
- Costs: we build (and must test) mechanics a library would provide; the naive
  loop is slower than a vectorized approach, so large parameter sweeps will be
  comparatively slow.
- Forecloses: nothing hard — a vectorized fast-path could be added later behind
  the same strategy API for sweeps, if speed becomes a real constraint.
- Now true: every result depends on our correctness, so the engine + broker +
  portfolio seam must be the most heavily tested part of the system.
