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
- **Offline enhancement batch:** three parallel, offline-verified additions.
  **Strategies** — `momentum` (time-series trailing-return) and `mean_reversion`
  (RSI oversold/recovery) join the registry, with `rsi`/`rolling_std`/`bollinger`
  in `strategies/indicators.py`; both long-or-flat, transition-driven, no look-ahead.
  **Parameter sweep** — `sweep.py` (`run_sweep`) + the `trading sweep` CLI expand a
  param grid (optional per-window walk-forward) over `Engine.run` as a pure outer
  loop, ranked by Sharpe/total return, writing a CSV (ADR-0016). **Vol-target risk +
  richer metrics** — opt-in `RiskConfig.target_volatility` (CLI `--target-vol`) scales
  the effective gross-exposure cap by realized-vs-target vol inside `Guardrails`, off
  by default (ADR-0015); `metrics.py` adds Sortino, Calmar, and turnover to the report.
  Fast tests green.
- **Alpaca milestone — live paper trading + real data (offline-verified):** an
  `AlpacaClient` seam (`data/alpaca_client.py`, ADR-0017/0018) with our-own-types
  DTOs, a `FakeAlpacaClient` for the fast layer, and a lazy `RealAlpacaClient` over
  the optional `alpaca-py` SDK (kept out of the locked deps; a mypy override only).
  On it: `AlpacaAdapter` (`data/alpaca_adapter.py`, adjusted daily bars) and
  `AlpacaBroker` (`brokers/alpaca.py`, ADR-0020) — a submit-then-poll paper broker
  that reconciles its `Portfolio` from the Alpaca account (authoritative, not
  byte-identical to backtest). Wired into the CLI: `--source alpaca`,
  `trading paper --broker alpaca --live`. Shipped alongside a **CSV**
  bring-your-own-data source (`data/csv_adapter.py`, `--source csv`) and **per-sector
  exposure caps** (`RiskConfig.sector_map` + `max_sector_exposure`, CLI
  `--max-sector-exposure`/`--sector-map`, ADR-0019). Integration also fixed a
  pre-existing guardrail-clamp bug (a sub-precision positive allowance rounding to a
  zero-qty order now rejects; regression-tested). Fast tests green.
- **Roadmap milestone — raw/adjusted split, intraday, web dashboard (offline-verified):**
  three parallel lanes off settled foundations, integrated behind one CLI PR.
  **Raw-vs-adjusted price policy** (ADR-0021) — the price notion is now an explicit
  per-mode policy carried by the feed: backtest asks for split/dividend-adjusted
  total-return prices (ADR-0008, unchanged), the paper/live feed asks for RAW actual
  quotes so the strategy decides and marks in the same dollars `AlpacaBroker`
  reconciles from the account. `AlpacaAdapter.get_bars` honors the per-call `adjusted`
  flag; `RecentWindowFeed` defaults to raw; yfinance/csv stay adjusted-only and error
  with guidance. **Intraday / bar-frequency abstraction** (ADR-0022, `frequency.py`) —
  a `Frequency` value (label/delta/`periods_per_year`); the interval is an
  adapter-construction property, so `DataAdapter.get_bars` and `Engine._step` are
  untouched and daily runs stay byte-identical. `SyntheticAdapter` generates sub-daily
  session-window bars (START-stamped), `recent_window.interval_is_complete` gates a
  bar until `ts+interval`, `PaperSession` wakes on the interval boundary, and
  `metrics.compute` annualizes by `periods_per_year`. Real intraday via Alpaca is
  wired behind a creds/SDK-gated integration test. **Web dashboard** (ADR-0023,
  `dashboard/`) — reads a run's canonical machine-readable `result.json`
  (`report.result_to_dict`/`write_result_json`, `RESULT_SCHEMA_VERSION`) and renders
  the equity curve (+benchmark), metrics, fills, and guardrail actions two ways: a
  pure-stdlib self-contained static HTML export (`--static`, zero external refs) and a
  thin, lazily-imported FastAPI server (`--serve`). FastAPI/uvicorn are the optional
  `dashboard` extra (locked additively into `uv.lock`; mypy override), so the fast gate
  stays green without them. CLI: `--interval 1d|1h|30m|5m|1m` on backtest/paper/sweep;
  backtest & paper now emit `result.json`; new `trading dashboard --static|--serve`.
  Fast gate green; the full daily→intraday→result.json→dashboard pipeline verified end
  to end offline on synthetic data.
- **Curated universes (offline-verified):** `universe.py` (ADR-0024) holds named
  stock baskets — a frozen `Basket` + `BASKETS` registry with `get_universe` /
  `get_sector_map`, seeded by `blue20` (20 mega-cap US names across 8 sectors). The
  CLI expands a `@name` sigil on `--symbols` (unknown -> exit 2 naming the baskets)
  and `--sector-map` (unknown -> the ValueError CLI-error path); plain comma lists
  are unchanged. Honesty caveat, stated in the module docstring: `blue20` is a
  *curation, not a broker fact* — fractionability/tradability are authoritative only
  via Alpaca's `get_asset` (a seam extension not yet built), so the universe must be
  verified against the broker before live use. Fast gate green; `backtest --source
  synthetic --symbols @blue20 --sector-map @blue20 --max-sector-exposure 0.30` runs
  end to end offline.
- **NOT yet built:** tick frequency and other asset classes (each its own ADR); and
  locking `alpaca-py` into the dependency set (deferred while the build sandbox is
  offline). Real Alpaca paper/live-quote runs need `pip install alpaca-py` plus
  `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in the environment; the dashboard server
  needs the `dashboard` extra (`uv sync --extra dashboard`).

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
  (ADR-0008). **Raw actual quotes for paper/live** — the price notion is a per-mode
  feed policy, not shared; never mark a raw account on adjusted prices (ADR-0021).
- **Frequency is an adapter property, not a `get_bars` argument** — the protocol and
  the engine per-bar step never learn the interval; act on a bar only once complete
  (`ts+interval`); daily stays byte-identical (ADR-0022).
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
  brokers/alpaca.py        # AlpacaBroker — submit-then-poll paper broker (ADR-0020)
  report.py                # text summary + equity_curve.csv + result.json (result_to_dict, ADR-0023)
  cli.py                   # `trading backtest / paper / gen-data / sweep / dashboard` (--source, --broker, --interval, @basket)
  sizing.py                # target-weight → fractional-share orders (V2)
  clock.py                 # Clock seam: WallClock / ImmediateClock / FakeClock (V5)
  frequency.py             # Frequency value: label/delta/periods_per_year — interval abstraction (ADR-0022)
  dashboard/               # web dashboard (ADR-0023): payload + static_export (stdlib) + server (lazy FastAPI)
  data/fake.py             # in-memory adapter for the fast test layer
  data/yfinance_adapter.py # cached, adjusted yfinance adapter (injectable fetcher)
  data/synthetic.py        # deterministic GBM adapter, daily+intraday — offline (ADR-0012/0022)
  data/csv_adapter.py      # bring-your-own-data OHLCV CSV DataAdapter (--source csv)
  data/alpaca_client.py    # AlpacaClient seam + Fake/Real clients (ADR-0017/0018)
  data/alpaca_adapter.py   # DataAdapter over Alpaca bars; per-call adjusted (ADR-0021) + interval (ADR-0022)
  data/recent_window.py    # completed-bars feed for paper; per-mode raw (ADR-0021) + interval completeness (ADR-0022)
  strategies/              # buy_and_hold, sma_crossover, equal_weight, momentum, mean_reversion + registry
  universe.py              # curated named stock baskets (blue20) + @name CLI expansion (ADR-0024)
  metrics.py               # perf metrics: return, Sharpe, Sortino, Calmar, drawdown, turnover, exposure (periods_per_year)
  sweep.py                 # parameter sweep / walk-forward over Engine.run (ADR-0016)
tests/
  unit/           # fast, no infra
  integration/    # marked; needs network/yfinance (CI-only)
docs/adr/         # one decision per file
```

Optional extras: `plot` (matplotlib PNG), `dashboard` (fastapi/uvicorn — `uv sync --extra dashboard`).
