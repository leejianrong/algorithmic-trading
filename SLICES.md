# Algorithmic Trading Test Bench: Slices

Vertical increments. Each ends in something you can demonstrate. Slice 1
confronts the riskiest unknown: whether the event-driven engine + multi-symbol
portfolio + simulated broker on adjusted data produce a correct, look-ahead-free
equity curve.

**Tooling (DONE, between V2 and V3):** a deterministic synthetic GBM data adapter
(`data/synthetic.py`, ADR-0012) plus `trading backtest --source synthetic` and
`trading gen-data`, so the whole stack runs and is tested offline. All three
strategies verified end to end on synthetic data.

## V1: End-to-end backtest of buy-and-hold across two symbols (adjusted data)

**Status: DONE** — engine, simulated broker, cached yfinance adapter, buy-and-hold,
report, and `trading backtest` CLI are implemented; fast tests green and the CLI
runs end to end. (Buy-and-hold leaves a small cost buffer so realistic slippage
doesn't reject its initial allocation; proper cost-aware sizing lands in V2.)

**Delivers:** R0, R2, R3 (partial)

**Build plan**

1. Define core types: tz-aware `Bar` (adjusted), `Instrument`, `Order`,
   `Position`, and a multi-symbol `Portfolio` (positions keyed by symbol + cash).
2. Implement `DataAdapter` + `YFinanceAdapter` (fetch **adjusted**, read-through
   parquet/CSV cache keyed by symbol/interval/range/adjustment) + a `FakeAdapter`
   yielding a synthetic multi-symbol series.
3. Implement `SimulatedBroker`: next-open fills ± slippage, commission, cash and
   per-symbol position updates, underfunded-order rejection.
4. Implement the engine loop + immediate clock: per timestamp, assemble the day's
   bars across the universe, call the strategy, route orders, mark the portfolio
   to each symbol's adjusted close. Ship a `buy_and_hold` strategy.
5. Wire `trading backtest --strategy buy_and_hold --symbols AAPL,MSFT --from … --to …`
   to print final equity and write an equity-curve CSV.

**Demo:** run on a cached range; final equity and total return match a
hand-computed split of capital across the two symbols within costs. Re-run with
the network off — works from cache, identical numbers. Run buy-and-hold across a
known split date and confirm no phantom crash appears in the curve.

**Rests on assumptions:** Q14 (next-open fills), Q17 (equity at adjusted close),
Q22 (starting capital). Wrong → numbers shift, mechanism stands.

### Test plan

#### End-to-end
- Buy-and-hold on a 5-bar, 2-symbol synthetic series yields the exact
  hand-computed equity curve (acceptance criterion).
- Cached real-data buy-and-hold matches Σ(shares × final adjusted close) within
  fees; a run spanning a real split shows no phantom crash (ADR-0008).
- Second offline run reproduces the first bit-for-bit.

#### Integration
- `YFinanceAdapter` requests adjusted data; cache miss writes, cache hit skips the
  network (stubbed fetch).
- Broker rejects an order exceeding cash and leaves state unchanged.

#### Unit
- Next-open buy fill = open × (1 + slippage); commission deducted.
- Portfolio equity = cash + Σ(position × adjusted close) across symbols.

## V2: Strategy API + target-weight sizing + SMA crossover, with look-ahead guard

**Status: DONE** — the sizing layer resolves `TargetWeight` → fractional-share
rebalance orders; `buy_and_hold` (now target-weight), `sma_crossover`, and a
multi-symbol `equal_weight` strategy ship; a fill blotter on `BacktestResult`
makes entry/exit bars observable; and the look-ahead guard is a test. Fast tests
green; all three strategies run end to end.

**Delivers:** R1, R4

**Build plan**

1. Finalize `Strategy.on_bar(ts, bars_by_symbol, context) -> list[TargetWeight | Order]`;
   `context` exposes positions, cash, equity, and a rolling per-symbol history —
   no future bars.
2. Implement the sizing layer: target weight × equity ÷ latest price as a
   fractional-share quantity (ADR-0011); reconcile against the current position
   (rebalance delta).
3. Implement the strategy loader (discover by name) and `sma_crossover`
   (fast/slow from config), plus one simple multi-symbol allocation example.

**Demo:** `trading backtest --strategy sma_crossover --symbols SPY,QQQ …` prints a
trade blotter and realized-vs-target weights; changing the windows in config
visibly changes trades.

**Rests on assumptions:** Q13 (config), fractional quantities rounded to a defined
share precision. Low cost if wrong.

### Test plan

#### End-to-end
- SMA crossover on a crafted series produces exactly the expected buy/sell bars.
- A "cheating" strategy cannot read bar *t+1* — `context` exposes no future data
  (look-ahead guard; acceptance criterion).
- A 0.20 target weight on a known equity/price yields the expected fractional-share
  quantity so the realized weight matches the target.

#### Integration
- Strategy loader resolves by name; unknown name → clear error.

#### Unit
- Rolling-mean helper matches a reference and never includes the unseen future.
- Sizing computes the fractional quantity and the rebalance delta from current
  holdings.

## V3: Enforced risk guardrails

**Status: DONE** — `RiskConfig` + a stateful `Guardrails` (`risk.py`) enforce a
per-symbol position cap and a gross-exposure cap (clamping over-cap buys, net of
same-bar committed exposure) plus a latching drawdown / daily-loss kill switch
that blocks new entries while still allowing exits. Wired into the engine's order
path (enforced by default, `--no-guardrails` to opt out); `BacktestResult`
carries `clamps`, merged `rejections`, and the halt state, and the report
surfaces them. ADR-0013 records the enforcement semantics. Fast tests green
(incl. the 200%→cap clamp, the scripted-drawdown halt, and the multi-order
gross-cap guard).

**Delivers:** R5

**Build plan**

1. Implement the pre-trade checker: cash, per-symbol max position %, max gross
   exposure — reject or clamp with a logged reason.
2. Implement the portfolio monitor: max-drawdown and daily-loss thresholds that
   halt new entries (optionally flatten) for the session.
3. Wire limits into config with defaults; thread guardrails into the engine's
   order path so every mode is protected.

**Demo:** run a strategy that requests 200% of equity and watch the exposure cap
clamp it; run one on a scripted crash and watch the kill switch halt new entries;
the report lists rejected/clamped orders and whether a halt fired.

**Rests on assumptions:** Q22 (default limits). Overridable per run.

### Test plan

#### End-to-end
- A strategy targeting 200% equity is clamped to the exposure cap (acceptance
  criterion).
- A scripted drawdown past the threshold halts new entries; existing positions
  behave per config (acceptance criterion).

#### Integration
- Guardrails sit on the shared order path, so an over-limit order is blocked
  identically whether invoked in backtest or paper wiring.

#### Unit
- Pre-trade checker accepts an in-limit order and rejects/clamps an over-limit one
  with the right reason.
- Drawdown monitor fires exactly at the configured threshold, not before.

## V4: Performance report, exposure, and benchmark

**Status: DONE** — pure metrics (`metrics.py`: total & annualized return, Sharpe
per Q17, max drawdown, win rate, avg/peak exposure, assembled into
`PerformanceMetrics` via `compute`) run over the equity curve. The engine now
records per-bar gross exposure on each `EquityPoint` (guarded to 0.0 on a flat
book / non-positive equity). The report (`report.py`) renders the full metrics
block beside the V3 guardrail lines, `write_equity_csv` adds an `exposure` column
(+ a timestamp-aligned `benchmark_equity` column when a benchmark is supplied),
and an optional `write_equity_png` plots the curve (matplotlib imported lazily).
The CLI adds `--benchmark SYMBOL` (an unconstrained buy-and-hold benchmark over
the same dates/source, works offline under `--source synthetic`) and
`--plot/--no-plot`. Fast tests green.

**Delivers:** R6

**Build plan**

1. Compute metrics from the equity curve: total & annualized return, Sharpe
   (daily returns, rf = 0), max drawdown, average/peak exposure, win rate.
2. Optionally compute a buy-and-hold SPY benchmark for side-by-side comparison.
3. Render a text report (summary + trade blotter + rejected/clamped orders) and
   write `equity_curve.csv` (+ optional `equity_curve.png`).

**Demo:** any backtest ends with a readable summary (including exposure and, if
enabled, vs-SPY) and writes the CSV; open it to see the curve.

**Rests on assumptions:** Q17 (Sharpe basis), Q24 (SPY benchmark). One
constant/column each.

### Test plan

#### End-to-end
- A known monotonic-up curve reports positive return, Sharpe > 0, zero drawdown;
  a known dip reports the exact max drawdown (acceptance criteria).

#### Integration
- Report writer emits a well-formed CSV, one row per trading day, and a benchmark
  column when enabled.

#### Unit
- Max-drawdown, Sharpe, exposure, and win-rate functions match hand-computed
  values on small fixtures.

## V5: Paper mode on recent daily data (same engine + guardrails, wall-clock)

**Status: DONE** — the per-bar loop body is extracted into a shared
`Engine._step` (with `_RunState` + `_finalize`) that BOTH backtest (`Engine.run`)
and paper (`PaperSession`) drive, so the two modes cannot fork (ADR-0002/0014);
backtest results are byte-identical (every prior test stays green). `PaperSession`
polls a `RecentWindowFeed` on an injected `Clock`, processes each newly completed
bar exactly once (idempotent via a seen-timestamp set), records a per-bar
`BarOutcome`, and sleeps until the next bar is due — bounded by `max_new_bars` /
consecutive-empty-polls for tests and offline demos. The `trading paper` CLI
mirrors `backtest`'s options plus `--live/--once`: `--once` (default) replays
`[from, to]` with a `FakeClock` offline and terminates, printing each bar's
decision/fills/guardrail actions/equity, appending to a session log, persisting
running state (`paper_state.json`), and writing the equity CSV; `--live` runs on
the wall clock until interrupted. ADR-0014 records the loop mechanism. Fast tests
green (parity, completed-bars-only, idempotency, guardrail-halt parity).

**Delivers:** R7, R8 (interface readiness)

**Build plan**

1. Implement a wall-clock clock and a recent-window feed that yields only
   **completed** daily bars.
2. Add `trading paper --strategy … --symbols …` reusing engine, sizing, broker,
   portfolio, and guardrails unchanged; append each bar's state to a session log
   and persist running state to the result dir.
3. Print status per new completed bar; verify guardrails apply identically.

**Demo:** run `trading paper …`; on each new completed daily bar it logs the
strategy's decision, simulated fills, guardrail actions, and updated equity —
visibly the same accounting and limits as backtest, only paced by the calendar.

**Rests on assumptions:** act on a daily bar only once complete (avoid a forming
bar). Wrong → decisions use unfinished data.

### Test plan

#### End-to-end
- Fed a scripted sequence of "newly completed" bars via a fake clock/feed, paper
  mode places the same orders the backtest would for the same bars (parity;
  acceptance criterion).

#### Integration
- The wall-clock clock waits for and emits only completed bars (fake clock, no
  real waiting).
- Guardrails halt a paper session on a scripted drawdown exactly as in backtest.

#### Unit
- A forming/partial latest bar is excluded until marked complete.

## Roadmap (out of this milestone)

- Alpaca live paper trading + data adapter. **Done** — an `AlpacaClient` seam plus
  `AlpacaAdapter` and a submit-then-poll `AlpacaBroker` behind the same
  `DataAdapter`/`Broker` interfaces; API keys via env, optional lazy `alpaca-py`
  (ADR-0017/0018/0020). CLI: `--source alpaca`, `paper --broker alpaca --live`.
- Intraday/tick frequency; other asset classes (each its own ADR).
- Parameter optimization / walk-forward as an outer sweep over runs. **Done** —
  `sweep.py` + `trading sweep` (ADR-0016).
- Volatility targeting **done** (`RiskConfig.target_volatility` / `--target-vol`,
  ADR-0015) and per-sector risk caps **done** (`--max-sector-exposure` /
  `--sector-map`, ADR-0019); a bring-your-own-data CSV source is **done**
  (`--source csv`). A web dashboard remains open.
