# ADR-0012: Synthetic data generator for offline backtesting

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The only real data source so far is yfinance, which needs the network — and some
environments (CI sandboxes, the project owner's current setup) can't reach it.
That blocks running the engine, strategies, and CLI end to end where it matters
most. Tests already use a hand-built `FakeAdapter`, but there was no way to
exercise the full stack over a realistic, multi-symbol, multi-month series
without a provider.

## Decision

Add a `SyntheticAdapter` implementing the `DataAdapter` seam that fabricates
deterministic geometric-Brownian-motion daily bars: per-symbol seeded RNG
(hashlib-derived, independent of `PYTHONHASHSEED`), weekdays only, valid OHLCV,
prices treated as already adjusted (ADR-0008). Same seed + symbol + range →
byte-identical bars, so synthetic backtests are reproducible.

Wire it into the CLI two ways: `backtest --source synthetic --seed N` runs the
whole pipeline offline, and a `gen-data` command writes synthetic bars to CSV
using the yfinance cache's naming, so `backtest --source yfinance --cache-dir …`
can read them back offline too.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Only `FakeAdapter` in tests | Fine for tiny fixtures, but tedious to author a realistic multi-symbol year, and not runnable from the CLI. |
| Ship a bundled real dataset | Licensing and size concerns; still one fixed dataset rather than parameterizable, seedable scenarios. |
| Record/replay real yfinance responses | Needs the network at least once to record, and ties fixtures to a provider's exact payloads. |

## Consequences

- Buys: the full engine/strategy/CLI stack is runnable and testable with no
  network; reproducible, parameterizable scenarios (drift/vol) for demos and
  future stress tests; a fast-layer e2e that proves every strategy runs.
- Costs: synthetic prices are a GBM toy — no fat tails, gaps, regimes, or
  microstructure — so results validate the *plumbing*, not a strategy's real
  edge. It must never be mistaken for a backtest on real data.
- Forecloses: nothing; it's an additive adapter behind the existing seam, and a
  richer generator (jumps, correlation, regimes) can replace the model later.
- Now true: strategies that over-leverage show up here as broker rejections
  (surfaced and reduced for SMA), foreshadowing the V3 exposure cap.
