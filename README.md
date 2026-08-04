# algorithmic-trading

A test bench for **backtesting**, **paper trading**, and algorithmic trading
strategies on US equities. One event-driven engine runs a strategy over price
bars two ways — `backtest` over history and `paper` on recent data — with the
same execution, sizing, costs, and risk limits in both, so a backtest result is
one you can carry toward real capital.

> Status: **the MVP is built and green.** The engine, simulated broker, sizing,
> enforced risk guardrails, six strategies, metrics, paper mode, a parameter
> sweep, an Alpaca paper-trading path, intraday bars, and a web dashboard all
> ship and are tested. The fast gate runs offline in seconds.
>
> Not built: tick frequency, non-equity asset classes, and a
> survivorship-bias-free historical universe (see
> [`docs/adr/0027-survivorship-bias.md`](docs/adr/0027-survivorship-bias.md)).
> `alpaca-py` is deliberately not in the lockfile — install it separately for
> real paper trading. [`CLAUDE.md`](CLAUDE.md) carries the honest, detailed build
> status; when it disagrees with the code, the code wins.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```bash
make setup   # install locked deps + the pre-push hook
make check   # lint + type-check + fast tests (no network)

# Backtest with no network at all, using the deterministic synthetic generator:
uv run trading backtest --strategy equal_weight --source synthetic \
  --symbols AAA,BBB,CCC --from 2021-01-01 --to 2022-12-31

# A cross-sectional strategy over a curated 20-name basket, with a sector cap:
uv run trading backtest --strategy cross_sectional --source synthetic \
  --symbols @blue20 --sector-map @blue20 --max-sector-exposure 0.30 \
  --from 2018-01-01 --to 2024-12-31
```

Drop `--source synthetic` (the default is `yfinance`) to backtest real
split/dividend-adjusted data once you have network access.

## What you can run

| Command | What it does |
|---------|--------------|
| `trading backtest` | Run a strategy over a historical range; prints metrics, writes `equity_curve.csv` and `result.json` |
| `trading paper` | Same engine on recent bars — `--once` replays offline, `--live` follows the wall clock |
| `trading sweep` | Parameter grid and walk-forward validation as an outer loop over backtests |
| `trading dashboard` | Render a run's `result.json` — `--static` (self-contained HTML, stdlib only) or `--serve` |
| `trading gen-data` | Write deterministic synthetic OHLCV bars to CSV |

**Strategies:** `buy_and_hold` (correctness baseline), `sma_crossover`,
`equal_weight`, `momentum`, `mean_reversion`, `cross_sectional`.

**Data sources:** `yfinance` (cached, adjusted), `synthetic` (offline,
deterministic), `csv` (bring your own), `alpaca` (real bars, daily or intraday).

**Optional extras:** `plot` (matplotlib PNG), `dashboard` (FastAPI server) —
`uv sync --extra dashboard`.

## What this bench refuses to do

It is built as a stepping stone toward real capital, so it favors honest numbers
over flattering ones:

- **No look-ahead.** An order submitted on bar *t* fills no earlier than *t+1*,
  structurally — the strategy context cannot see a future bar, and a cheat
  strategy proves it in the test suite (ADR-0001).
- **Pessimistic fills.** Next-open fills with configurable slippage and
  commission; an order that outruns cash is rejected (ADR-0004).
- **Total-return prices in backtests**, so a stock split is never misread as a
  50% crash (ADR-0008) — and **raw quotes in paper/live**, so a raw broker
  account is never marked on adjusted prices (ADR-0021).
- **Guardrails that veto, not warn.** Position, gross-exposure, and per-sector
  caps clamp orders, and a drawdown kill switch halts new entries while still
  allowing exits (ADR-0009, ADR-0013, ADR-0019).
- **Named biases.** Known limitations are written down rather than left implicit
  — survivorship bias (ADR-0027) and broker-verified tradability (ADR-0028).
- **No real-money orders.** The Alpaca path targets the paper endpoint.

## Docs

- [`PLAN.md`](PLAN.md) — problem, scope, requirements, and the shape of the build.
- [`SLICES.md`](SLICES.md) — vertical, demoable slices, riskiest first.
- [`docs/adr/`](docs/adr/) — one architectural decision per file.
- [`QUESTIONS.md`](QUESTIONS.md) — the decision register.
- [`CLAUDE.md`](CLAUDE.md) — agent/developer brief: build status, commands,
  conventions, and domain invariants.

## Development

```bash
make check             # fast gate: lint + type-check + no-infra tests
make test-integration  # integration layer (needs network / yfinance)
make audit             # dependency vulnerability scan
make ci-local          # everything CI runs, locally
```

Branch per slice off fresh `main`, PR only. The pre-push hook runs the fast gate;
bypass a single push with `git push --no-verify` and a scoped reason.
