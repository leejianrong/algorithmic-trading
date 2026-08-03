# Algorithmic Trading Test Bench: Plan

Status: agreed · Milestone: MVP

## Problem

Trying out a trading idea usually means gluing together data downloads, a
hand-rolled loop over price bars, and a spreadsheet of results — and every idea
starts that glue over again. Worse, the naive loop is easy to get subtly wrong:
peeking at a bar's close to make a decision that fills at that same close is
look-ahead bias, a raw price series turns an ordinary stock split into a fake
50% crash, and a backtester that fills perfectly and for free flatters every
strategy until real money disagrees.

There is no single place to write a strategy once and then backtest it on
history, watch it trade on fresh data, and eventually rehearse it against a real
broker — with the *same* execution rules, costs, and risk limits applied
throughout, so a result you see in backtesting is a result you can carry toward
real capital.

## Solution

A Python test bench where you write a strategy as a small class that reacts to a
day's worth of price bars and expresses intent as target weights (e.g. "hold 20%
of equity in AAPL"). You run it two ways from the same command line: `backtest`
over a historical date range, or `paper` against recent daily data with no real
money at risk. Both modes drive the **same** engine, strategy API, broker,
portfolio accounting, and risk guardrails, so moving from backtest to paper —
and later to a real-broker paper account — changes only where the bars come from
and how the clock advances, never how orders fill, how risk is capped, or how the
portfolio is accounted. Each run ends in a compact report: final equity, return,
Sharpe, max drawdown, exposure, trade blotter, and an equity-curve file.

This is built as a stepping stone toward trading real capital, so it refuses to
flatter itself: fills are pessimistic, prices are total-return adjusted, risk
limits are enforced (not just reported), and nothing in the MVP can place a real
order.

## Users and actors

- **Primary: the strategy developer** (a single person, relatively new to algo
  trading, on their own machine) who writes strategies and runs backtests and
  paper sessions with an eye toward eventually trading real money. Wins all ties
  — the tool optimizes for their iteration speed *and* for numbers honest enough
  to bet on.
- **The CLI / an automating agent** as a non-human actor: runs are launchable
  non-interactively so a script or agent can sweep parameters or re-run nightly.
- **The risk guardrails** as an internal actor with veto power: an order that
  breaches a limit is rejected or a session halted even if the strategy asked for
  it. Safety outranks the strategy.
- **yfinance** as an external data actor: authoritative for historical
  split/dividend-adjusted prices; when it is unavailable or a symbol is missing,
  the run fails fast rather than guessing.

## Scope

**In this milestone.**

- US equities, **daily** bars only.
- Custom **event-driven** engine driving a **multi-symbol** portfolio; the
  strategy sees a timestamp's bars across its universe and returns orders.
- **Target-percent-of-equity** position sizing: strategies express target
  weights; the engine converts to whole-share quantities.
- Historical data via **yfinance**, **split/dividend adjusted**, fetched behind a
  pluggable adapter and cached locally so re-runs are offline and deterministic.
- **Simulated broker**: market/limit orders, **conservative** next-open fills
  with configurable (realistically pessimistic) slippage and commission, cash and
  position accounting, order rejection.
- **Enforced risk guardrails**: per-order sanity checks, max position size, max
  gross exposure, and a max-drawdown / daily-loss kill switch that halts new
  entries.
- `backtest` mode over a date range and `paper` mode over recent daily bars, both
  on the same engine, broker, and guardrails.
- Performance report: final equity, total/annualized return, Sharpe, max
  drawdown, exposure, win rate, trade blotter, and an equity-curve CSV.
- Reference strategies: buy-and-hold (correctness baseline) and SMA crossover;
  at least one multi-symbol/allocation example.

**Out.**

- **Live real-money trading.** Explicitly excluded so nothing in the MVP can
  place a real order; safety over features.
- **Alpaca (or any real-broker) paper API.** The **next milestone** after the
  offline bench works (ADR-0004) — the broker interface is built for it, but it
  is not wired this milestone.
- **Intraday / tick data.** Daily bars only; the `Bar` model keeps a full
  timestamp so intraday is a later adapter, not a rewrite.
- **Crypto, forex, futures, options.** Single asset class avoids a premature
  instrument abstraction.
- **Web UI / dashboard.** CLI + files only.
- **Parameter optimization / walk-forward.** Runs are scriptable, so a sweep is
  an outer loop, not an engine feature yet.
- **Persistent database.** State lives in memory per run; only the data cache,
  result files, and paper-session log touch disk.

## Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| R0 | Run a strategy over historical US-equity daily (adjusted) bars and get a correct, look-ahead-free equity curve and summary metrics | Core goal |
| R1 | Strategies are written once against a bar-driven API and run unchanged across backtest, paper, and (later) real-broker modes | Must-have |
| R2 | Data is fetched via a pluggable adapter (yfinance first), split/dividend adjusted, and cached for offline, deterministic re-runs | Must-have |
| R3 | Simulated broker models conservative fills, commission, slippage, cash, and positions across a multi-symbol portfolio | Must-have |
| R4 | Position sizing is target-percent-of-equity; the engine converts weights to whole-share orders | Must-have |
| R5 | Risk guardrails are enforced, not just reported: per-order checks, position/exposure caps, drawdown/daily-loss kill switch | Must-have |
| R6 | A run produces a report: return, Sharpe, max drawdown, exposure, win rate, trade blotter, equity-curve CSV | Must-have |
| R7 | `paper` mode runs the same engine, broker, and guardrails against recent daily data on a wall-clock cadence | Must-have |
| R8 | Broker and data interfaces are shaped so an Alpaca paper adapter can be added next without touching the engine | Nice-to-have |

## Shape

| Part | Mechanism | ADR |
|------|-----------|-----|
| S1 | **Event-driven engine**: a single loop advances a clock, assembles the next timestamp's `Bar` set across the universe, calls `Strategy.on_bar`, passes returned target-weight orders through the guardrails to the broker, then marks the portfolio to those bars' closes. Orders submitted on day *t* fill no earlier than day *t+1*, structurally preventing look-ahead. | ADR-0001 |
| S2 | **One execution path, two feeds/clocks**: backtest = historical adjusted feed + immediate clock; paper = recent/live feed + wall-clock clock. Strategy, sizing, broker, portfolio, and guardrail code are identical across modes. | ADR-0002 |
| S3 | **DataAdapter interface** returning normalized, adjusted `Bar` series; `YFinanceAdapter` fetches (adjusted) then caches to local parquet/CSV; `CSVAdapter` reads a supplied file. Cache is read-through, keyed by (symbol, interval, range, adjustment). | ADR-0003 |
| S4 | **Broker interface** (`submit`, `on_bar`, `positions`, `cash`); `SimulatedBroker` fills market orders at next open ± slippage, applies commission, updates cash/positions, rejects invalid orders. Alpaca broker is the next implementation of the same interface. | ADR-0004 |
| S5 | **Multi-symbol portfolio**: `Portfolio` holds positions keyed by symbol plus cash; equity = cash + Σ marked positions. All accounting and metrics are portfolio-level from day one. | ADR-0006 |
| S6 | **Sizing layer**: strategies emit target weights; the engine converts a target weight to a whole-share order using current equity and the latest price, then hands it to the guardrails. | ADR-0007 |
| S7 | **Adjusted-price data**: backtests run on split/dividend-adjusted series end-to-end so returns are total return; a split is not misread as a crash and dividends are not lost. | ADR-0008 |
| S8 | **Risk guardrails**: a pre-trade checker (cash, max position %, max gross exposure) plus a portfolio monitor (max drawdown / daily loss) that can veto orders and halt new entries. Enforced inside the engine, on by default. | ADR-0009 |
| S9 | **Instrument & market model**: `Instrument(symbol)`, tz-aware `Bar(ts, o,h,l,c,v)`, a US calendar used only to enumerate trading days. | ADR-0005 |
| S10 | **Metrics & report**: equity curve → return, annualized return, Sharpe, max drawdown, exposure, win rate; text summary + `equity_curve.csv` (+ optional PNG). |  |
| S11 | **CLI + strategy loader**: `trading backtest|paper` with a symbol universe, range, strategy name, and config; strategies discovered by name. |  |

## Affordances

**Non-UI.**

| Affordance | Kind | Wires to |
|------------|------|----------|
| `trading backtest` | CLI command | engine (S1) in historical mode → report |
| `trading paper` | CLI command | engine (S1) in wall-clock mode → session log |
| `Strategy` base class | extension point | user code; called per timestamp with the universe's bars |
| `DataAdapter` (`YFinanceAdapter`, `CSVAdapter`) | data source (adjusted) | engine feed |
| sizing layer | engine step | converts target weights → share orders |
| risk guardrails | engine gate | vetoes orders / halts session |
| `SimulatedBroker` | order handler | engine order routing |
| local data cache | store (parquet/CSV) | `YFinanceAdapter` |
| report writer | output (stdout + CSV/PNG) | portfolio/metrics |

## Implementation decisions

- **Language/stack:** Python 3.11+, `pandas`/`numpy`, `yfinance` (adjusted
  prices), `matplotlib` optional for the equity-curve image, `pytest`, `typer`
  (or argparse) for the CLI; packaged with `uv`/`pip`.
- **Module boundaries:** `engine` (loop + clock + sizing), `data` (adapters +
  cache), `broker`, `strategy` (base + references), `portfolio` (multi-symbol
  accounting + metrics), `risk` (guardrails), `cli`, `report`. The engine depends
  on the `Strategy`, `DataAdapter`, and `Broker` interfaces only, which is what
  lets ADR-0002 and ADR-0004 hold.
- **Core contracts:**
  - `Bar`: `ts: datetime (tz-aware UTC), open/high/low/close: float (adjusted),
    volume: int`.
  - `Order`: `symbol, side (buy|sell), qty (whole shares), type (market|limit),
    limit_price?` — produced by the sizing layer from a strategy's target weight.
  - `Strategy.on_bar(ts, bars_by_symbol, context) -> list[TargetWeight | Order]`;
    `context` exposes positions, cash, equity, and a rolling per-symbol history
    window — never future bars.
  - `Broker.submit(order)`, `.on_bar(bars)`, `.positions()`, `.cash()`.
  - `RiskGuardrails.check(order, portfolio) -> Accept | Reject(reason)` and
    `.monitor(portfolio) -> Halt?`.
- **Sizing rule:** target weight × equity ÷ latest price, floored to whole
  shares; a weight over the position cap is clamped by the guardrails (ADR-0007,
  ADR-0009).
- **Determinism:** given the same cached adjusted data, config, and strategy, a
  backtest is bit-for-bit reproducible; no wall-clock or RNG in the backtest path.
- **Config:** a TOML file (starting capital, commission, slippage, risk limits,
  universe) plus CLI flags; flags override the file.

## Testing approach

Test at the highest seams first. The load-bearing seam is **engine + sizing +
guardrails + simulated broker + multi-symbol portfolio** producing a known
equity curve; drive it end-to-end with a tiny synthetic bar series across two
symbols so the expected numbers are hand-computable, and separately with
buy-and-hold on cached adjusted data checked against a closed-form result.
Explicitly guard the load-bearing invariants: a cheat strategy proves no
look-ahead; a strategy that requests 200% of equity proves the exposure cap
clamps it; a scripted drawdown proves the kill switch halts entries; an adjusted
series across a known split proves no phantom crash. Test adapters against the
interface with a fake feed, not against yfinance's network. Per-slice test plans
live in `SLICES.md`.

## Assumed defaults

| ID | Assumed | Cost if wrong |
|----|---------|---------------|
| Q12 | Local single-user Python CLI; no server/container in MVP | Add a runner later; engine unchanged |
| Q13 | TOML config + CLI flags | Cheap; localized to `cli` |
| Q14 | Fills at next open ± slippage (not close); default costs set realistically pessimistic | Alters returns; isolated to `SimulatedBroker` |
| Q17 | Sharpe on daily returns, risk-free = 0; equity marked at adjusted close | One constant to change |
| Q22 | Default starting capital $100,000; default limits (e.g. 25% max position, 100% max gross exposure, 20% drawdown halt) live in config | Just config defaults; per-run overridable |
| Q23 | Backtest trades on the adjusted series; the future paper/live path will trade on actual quotes | Revisit when wiring Alpaca; backtest accounting unaffected |
| Q24 | Optional benchmark = buy-and-hold SPY for comparison in the report | Additive report column |

## Open risks

- **yfinance reliability / adjustment quirks** (revised history, bad splits)
  could corrupt results. Revealed earliest in **V1**, which caches, re-runs
  offline, and cross-checks buy-and-hold against a hand computation.
- **Look-ahead creeping in** through the `context` window API. Revealed in **V2**
  by the dedicated cheat-strategy test.
- **Guardrails that are too blunt or too lax** — clamping legitimate trades, or
  failing to halt a runaway. Revealed in **V3** by the exposure-cap and
  kill-switch tests.
- **Paper-mode clock/feed drift** (a forming daily bar isn't final until close).
  Revealed in **V5**; mitigated by acting only on completed daily bars.
