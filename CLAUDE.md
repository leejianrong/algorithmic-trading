# Agent brief — algorithmic trading test bench

A test bench where one **event-driven engine** runs a strategy over US-equity
**daily** bars two ways — `backtest` and `paper` — with the same execution,
sizing, costs, and risk limits in both. Built as a stepping stone toward real
capital, so it favors honest numbers over flattering ones. Full context:
[`PLAN.md`](PLAN.md), slices in [`SLICES.md`](SLICES.md), decisions in
[`docs/adr/`](docs/adr/), decision register in [`QUESTIONS.md`](QUESTIONS.md).

## Build status — trust the code over these docs

As of this writing:

- **Done and tested:** the scaffold and quality gates; core value types
  (`types.py`) and DI seams (`interfaces.py`); **V1 — a working backtest**: the
  event-driven `Engine` (`engine.py`), `SimulatedBroker` (`broker.py`, next-open
  fills + slippage/commission + funding rejection), the cached `YFinanceAdapter`
  (`data/yfinance_adapter.py`) and in-memory `FakeAdapter`, a minimal report
  (`report.py`), and the `trading backtest` CLI (`cli.py`); and **V2 — target-weight
  sizing** (`sizing.py`, resolves `TargetWeight` → fractional-share rebalance
  orders), the `sma_crossover` and `equal_weight` strategies plus `buy_and_hold`
  baseline (`strategies/`), and a fill blotter on `BacktestResult`. Fast tests
  green; `trading backtest` runs end to end for all three strategies.
- **Offline data:** a deterministic **synthetic** GBM adapter (`data/synthetic.py`,
  ADR-0012) drives the whole stack without a network — `trading backtest --source
  synthetic` and `trading gen-data`. All three strategies verified end to end on it.
- **V3 — enforced risk guardrails:** `RiskConfig` + a stateful `Guardrails`
  (`risk.py`, ADR-0013) sit on the engine's order path, enforced by default
  (`--no-guardrails` opts out): a per-symbol position cap and a gross-exposure cap
  clamp over-cap buys (net of same-bar committed exposure), and a latching
  drawdown / daily-loss kill switch blocks new entries while still allowing exits.
  `BacktestResult` carries `clamps` + merged `rejections` + halt state; the report
  surfaces them. Fast tests green.
- **V4 — performance report, exposure & benchmark:** `metrics.py` computes total
  & annualized return, Sharpe (Q17), max drawdown, win rate, and avg/peak exposure
  (`PerformanceMetrics` via `compute`). The engine records per-bar gross exposure
  on each `EquityPoint`; the report renders the full metrics block, writes an
  `exposure` column (+ aligned `benchmark_equity` when enabled) to the CSV, and an
  optional lazy-matplotlib PNG. The CLI gains `--benchmark SYMBOL` (unconstrained
  buy-and-hold, offline-capable) and `--plot/--no-plot`. Fast tests green.
- **V5 — paper mode:** the per-bar loop body is a shared `Engine._step` (with
  `_RunState`/`_finalize`) that both backtest (`Engine.run`) and paper
  (`PaperSession`, `engine.py`) drive, so the modes can't fork (ADR-0002/0014);
  backtest stays byte-identical. A `Clock` seam (`clock.py`: `WallClock` /
  `ImmediateClock` / `FakeClock`) and a completed-bars `RecentWindowFeed`
  (`data/recent_window.py`) feed paper; the loop processes each newly completed
  bar once (idempotent), logs a `BarOutcome`, and sleeps until the next is due. The
  `trading paper` CLI mirrors `backtest` plus `--live/--once` (default `--once`
  replays offline and terminates; persists a session log, `paper_state.json`, and
  the equity CSV). Fast tests green (parity, completed-bars-only, halt parity).
- **NOT yet built:** the **Alpaca** data/broker adapters (next milestone). **That
  Alpaca milestone (see the roadmap at the end of `SLICES.md`) is the next work.**

If code and prose disagree, the code wins — update the prose.

## Commands

```bash
make setup          # uv sync --frozen + install the pre-push hook (run once)
make check          # FAST GATE: lint + type-check + no-infra tests (what pre-push runs)
make test           # fast test layer only (no network)
make test-integration  # integration layer (needs network / yfinance)
make test-all       # every layer
make audit          # dependency vulnerability scan
make ci-local       # everything CI runs, locally
```

Run one test: `uv run pytest tests/unit/test_types.py::TestPortfolioAccounting`.

## How work is done here (conventions)

- **Branch per slice off fresh `main`; PR-only; `main` is protected.** No direct
  pushes to `main`. (This session's designated dev branch is
  `claude/plan-new-project-l4n535`.)
- **Fast gate before every push.** `make check` must pass; the pre-push hook runs
  it. Bypass only with a scoped reason via `git push --no-verify`.
- **Layer tests by cost.** Fast layer = no infra, runs everywhere. Integration
  (`@pytest.mark.integration`) and e2e are CI-only; never let them gate a push.
- **Every bug and flake becomes a failing test first, then a fix.** A regression
  seen twice is a missing test.
- **Prove a guard by watching it fail.** Break what a safety/compat test protects,
  see it go red, then restore — non-destructively (commit or `git stash` first;
  never `git checkout --` over uncommitted work).
- **Adversarial review before merge.** Read the diff to break it; prefer a public
  seam over a private reach.

## Domain invariants (do not regress)

- **No look-ahead:** an order submitted on bar *t* fills no earlier than *t+1*;
  `StrategyContext` never exposes future bars (ADR-0001).
- **Adjusted prices** for backtests — total return, no phantom split crashes
  (ADR-0008).
- **Guardrails are enforced, not advisory:** position/exposure caps and the
  drawdown kill switch can veto or clamp orders (ADR-0009).
- **One execution path:** backtest and paper differ only in feed and clock;
  never fork strategy/broker/portfolio logic between them (ADR-0002).
- **No implicit shorting; fractional-share quantities allowed** (ADR-0011).

## Layout

```
src/trading/
  types.py                 # core value types (implemented, tested)
  interfaces.py            # DI seams: DataAdapter, Broker, Strategy, RiskGuardrails
  config.py                # BacktestConfig, CostConfig (defaults: $1,000, 5 bps)
  engine.py                # shared per-bar step + Engine.run (backtest) + PaperSession (V5)
  broker.py                # SimulatedBroker + CostModel
  report.py                # text summary + equity_curve.csv
  cli.py                   # `trading backtest / paper / gen-data`
  sizing.py                # target-weight → fractional-share orders (V2)
  clock.py                 # Clock seam: WallClock / ImmediateClock / FakeClock (V5)
  data/fake.py             # in-memory adapter for the fast test layer
  data/yfinance_adapter.py # cached, adjusted yfinance adapter (injectable fetcher)
  data/synthetic.py        # deterministic GBM adapter — offline backtests (ADR-0012)
  data/recent_window.py    # completed-bars feed for paper mode (V5)
  strategies/              # buy_and_hold, sma_crossover, equal_weight + registry
  metrics.py               # pure performance metrics over the equity curve (V4)
tests/
  unit/           # fast, no infra
  integration/    # marked; needs network/yfinance (CI-only)
docs/adr/         # one decision per file
```
