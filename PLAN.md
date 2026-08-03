# Algorithmic Trading Test Bench: Plan

Status: draft · Milestone: MVP

## Problem

Trying out a trading idea usually means gluing together data downloads, a
hand-rolled loop over price bars, and a spreadsheet of results — and every idea
starts that glue over again. Worse, the naive loop is easy to get subtly wrong:
peeking at a bar's close to make a decision that fills at that same close is
look-ahead bias, and it silently inflates results until real money disagrees.

There is no single place to write a strategy once and then backtest it on
history, watch it trade on fresh data, and compare the two — with the same
execution rules applied in both, so a result you see in backtesting is a result
you can trust.

## Solution

A Python test bench where you write a strategy as a small class that reacts to
one price bar at a time, then run it two ways from the same command line:
`backtest` over a historical date range, or `paper` against recent/live daily
data with no real money at risk. Both modes drive the **same** engine, strategy
API, and broker, so switching from backtest to paper changes only where the
bars come from and how the clock advances — never how orders fill or how the
portfolio is accounted. Each run ends in a compact report: final equity, return,
Sharpe, max drawdown, trade list, and an equity-curve file.

## Users and actors

- **Primary: the strategy developer** (a single person on their own machine)
  who writes strategies and runs backtests and paper sessions. Wins all ties —
  the tool optimizes for their iteration speed and for trustworthy numbers.
- **The CLI / an automating agent** as a non-human actor: runs are launchable
  non-interactively so a script or agent can sweep parameters or re-run nightly.
- **yfinance** as an external data actor: authoritative for historical prices;
  when it is unavailable or a symbol is missing, the run fails fast rather than
  guessing.

## Scope

**In this milestone.**

- US equities, **daily** bars only.
- Custom **event-driven** engine driving a bar-at-a-time strategy API.
- Historical data via **yfinance**, fetched behind a pluggable adapter and
  cached locally so re-runs are offline and deterministic.
- **Simulated broker**: market/limit orders, configurable commission and
  slippage, cash and position accounting, rejection on insufficient funds.
- `backtest` mode over a date range and `paper` mode over recent daily bars,
  both on the same engine and simulated broker.
- Performance report: final equity, total/annualized return, Sharpe, max
  drawdown, win rate, trade blotter, and an equity-curve CSV.
- One reference strategy shipped (SMA crossover) plus buy-and-hold as the
  correctness baseline.

**Out.**

- **Live real-money trading.** Explicitly excluded so nothing in the MVP can
  place a real order; safety over features.
- **Alpaca (or any real-broker) paper API.** On the roadmap (ADR-0004) — the
  broker interface is designed for it, but it is not built this milestone.
- **Intraday / tick data.** Daily bars only keeps the clock and data model
  simple; the bar model is designed not to preclude it.
- **Crypto, forex, futures, options.** Single asset class avoids a premature
  instrument abstraction.
- **Web UI / dashboard, live streaming charts.** CLI + files only.
- **Parameter optimization / walk-forward.** Runs are scriptable, so a sweep is
  an outer loop, not an engine feature yet.
- **Persistent database.** State lives in memory per run; only the data cache
  and result files touch disk.

## Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| R0 | Run a strategy over historical US-equity daily bars and get a correct, look-ahead-free equity curve and summary metrics | Core goal |
| R1 | Strategies are written once against a bar-driven API and run unchanged in both backtest and paper mode | Must-have |
| R2 | Historical data is fetched via a pluggable adapter (yfinance first) and cached for offline, deterministic re-runs | Must-have |
| R3 | Simulated broker models fills, commission, slippage, cash and positions, and rejects invalid orders | Must-have |
| R4 | A run produces a report: return, Sharpe, max drawdown, win rate, trade blotter, equity-curve CSV | Must-have |
| R5 | `paper` mode runs the same engine against recent daily data on a wall-clock cadence | Must-have |
| R6 | Ship SMA-crossover and buy-and-hold reference strategies | Nice-to-have |
| R7 | Broker and data interfaces are shaped so an Alpaca adapter can be added without touching the engine | Nice-to-have |

## Shape

| Part | Mechanism | ADR |
|------|-----------|-----|
| S1 | **Event-driven engine**: a single loop advances a clock, pulls the next `Bar` from the feed, calls `Strategy.on_bar`, routes returned orders to the broker, then marks the portfolio to that bar's close. Orders submitted on bar *t* fill no earlier than bar *t+1*, which structurally prevents look-ahead. | ADR-0001 |
| S2 | **One execution path, two feeds/clocks**: backtest = historical feed + as-fast-as-possible simulated clock; paper = recent/live feed + wall-clock clock. Strategy, broker, and portfolio code are identical across modes. | ADR-0002 |
| S3 | **DataAdapter interface** returning normalized `Bar` series; `YFinanceAdapter` fetches then caches to local parquet/CSV; `CSVAdapter` reads a supplied file. Cache is read-through and keyed by (symbol, interval, range). | ADR-0003 |
| S4 | **Broker interface** (`submit`, `positions`, `cash`, `on_bar`); `SimulatedBroker` fills market orders at next open (± slippage), applies commission, updates cash/positions, rejects underfunded orders. Alpaca broker is a future implementation of the same interface. | ADR-0004 |
| S5 | **Instrument & market model**: `Instrument(symbol)`, `Bar(ts, open, high, low, close, volume)`, a US market calendar used only to iterate valid trading days. | ADR-0005 |
| S6 | **Portfolio & metrics**: positions + cash → equity per bar → equity curve → derived metrics (return, Sharpe, max drawdown, win rate). |  |
| S7 | **CLI + strategy loader**: `trading backtest|paper` with symbol(s), range, strategy name, and config; strategies discovered by name from a strategies package. |  |

## Affordances

**Non-UI.**

| Affordance | Kind | Wires to |
|------------|------|----------|
| `trading backtest` | CLI command | engine (S1) in historical mode, report writer |
| `trading paper` | CLI command | engine (S1) in wall-clock mode |
| `Strategy` base class | extension point | user code; called by engine per bar |
| `DataAdapter` (`YFinanceAdapter`, `CSVAdapter`) | data source | engine feed |
| `SimulatedBroker` | order handler | engine order routing |
| local data cache | store (parquet/CSV files) | `YFinanceAdapter` |
| report writer | output (stdout + CSV) | portfolio/metrics |

## Implementation decisions

- **Language/stack:** Python 3.11+, `pandas`/`numpy` for series and metrics,
  `yfinance` for data, `matplotlib` optional for an equity-curve image, `pytest`
  for tests, `typer` (or argparse) for the CLI. Packaged with `uv`/`pip`.
- **Module boundaries:** `engine` (loop + clock), `data` (adapters + cache),
  `broker` (interface + simulator), `strategy` (base + references), `portfolio`
  (accounting + metrics), `cli`, `report`. The engine depends on the `Strategy`,
  `DataAdapter`, and `Broker` interfaces only — never on concrete
  implementations — which is what lets ADR-0002 and ADR-0004 hold.
- **Core contracts:**
  - `Bar`: `ts: datetime (tz-aware, UTC), open/high/low/close: float, volume: int`.
  - `Order`: `symbol, side (buy|sell), qty, type (market|limit), limit_price?`.
  - `Strategy.on_bar(bar, context) -> list[Order]`; `context` exposes positions,
    cash, and a rolling history window — never future bars.
  - `Broker.submit(order)`, `.on_bar(bar)` (triggers fills), `.positions()`,
    `.cash()`.
- **Determinism:** given the same cached data, config, and strategy, a backtest
  is bit-for-bit reproducible; no wall-clock or RNG in the backtest path.
- **Config:** a small config file (TOML) plus CLI flags; flags override file.

## Testing approach

Test at the highest seams first. The load-bearing seam is the **engine +
simulated broker + portfolio** producing a known equity curve; drive it
end-to-end with a tiny synthetic bar series so the expected numbers are
hand-computable, and separately with buy-and-hold on cached real data checked
against a closed-form `shares × price` result. Guard look-ahead explicitly with
a strategy that would only profit by cheating and asserting it does not. Test
adapters against the interface (a fake feed) rather than against yfinance's
network. Per-slice test plans live in `SLICES.md`.

## Assumed defaults

| ID | Assumed | Cost if wrong |
|----|---------|---------------|
| Q9 | Single-user, single-run, single-threaded; no concurrency | Re-add a run scheduler later; engine stays as-is |
| Q10 | In-memory state per run; only cache + results on disk (no DB) | Add persistence layer if run history must be queryable |
| Q13 | TOML config + CLI flags; no config UI | Cheap to change; localized to `cli` |
| Q14 | Fills at next bar's open ± slippage (not close) | Alters reported returns; isolated to `SimulatedBroker`, revisit with a fill-model ADR |
| Q17 | Metrics use daily returns, risk-free = 0 for Sharpe | Sharpe shifts; one constant to change |

## Open risks

- **yfinance reliability / silent data gaps** (splits, missing days) could
  corrupt results. Revealed earliest in **V1**, which caches and re-runs offline
  and cross-checks buy-and-hold against a hand computation.
- **Look-ahead creeping in** through the `context` window API. Revealed in
  **V2** by the dedicated cheat-strategy test.
- **Paper-mode clock/feed drift** (a "live" daily bar isn't final until close).
  Revealed in **V4**; mitigated by only acting on completed daily bars.
