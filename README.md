# algorithmic-trading

A test bench for **backtesting**, **paper trading**, and algorithmic trading
strategies on US equities. One event-driven engine runs a strategy over daily
bars two ways — `backtest` over history and `paper` on recent data — with the
same execution, sizing, costs, and risk limits in both, so a backtest result is
one you can carry toward real capital.

> Status: **planning complete, scaffold in place.** Core value types and quality
> gates are implemented and tested; the engine, broker, strategies, and CLI are
> the next work (slice V1). See [`CLAUDE.md`](CLAUDE.md) for honest build status.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```bash
make setup   # install locked deps + the pre-push hook
make check   # lint + type-check + fast tests

# Run a backtest with no network, using the deterministic synthetic generator:
uv run trading backtest --strategy equal_weight --source synthetic \
  --symbols AAA,BBB,CCC --from 2021-01-01 --to 2022-12-31
```

Drop `--source synthetic` (the default is `yfinance`) to backtest real
split/dividend-adjusted data once you have network access.

## Docs

- [`PLAN.md`](PLAN.md) — problem, scope, requirements, and the shape of the build.
- [`SLICES.md`](SLICES.md) — vertical, demoable slices, riskiest first.
- [`docs/adr/`](docs/adr/) — one architectural decision per file.
- [`QUESTIONS.md`](QUESTIONS.md) — the decision register.
- [`CLAUDE.md`](CLAUDE.md) — agent/developer brief: build status, commands,
  conventions, and domain invariants.
