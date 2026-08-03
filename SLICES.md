# Algorithmic Trading Test Bench: Slices

Vertical increments. Each ends in something you can demonstrate. Slice 1
confronts the riskiest unknown: whether the event-driven engine + data adapter +
simulated broker + portfolio produce a correct, look-ahead-free equity curve.

## V1: End-to-end backtest of buy-and-hold on one symbol

**Delivers:** R0, R2 (partial), R3 (partial)

**Build plan**

1. Define core types: `Bar`, `Instrument`, `Order`, `Position`.
2. Implement `DataAdapter` interface, `YFinanceAdapter` with read-through
   parquet/CSV cache, and a `FakeAdapter` yielding a synthetic bar series.
3. Implement `SimulatedBroker`: next-open fills ± slippage, commission, cash and
   position updates, underfunded-order rejection.
4. Implement the engine loop + simulated clock (advance immediately) marking the
   portfolio to each bar's close, and a `buy_and_hold` strategy.
5. Wire `trading backtest --strategy buy_and_hold --symbol AAPL --from … --to …`
   to print final equity and write an equity-curve CSV.

**Demo:** run the command on a cached range; see final equity and total return
printed, and confirm they match a hand-computed `initial_cash / entry_price ×
final_price` within commission/slippage. Re-run with the network off — it works
from cache and prints identical numbers.

**Rests on assumptions:** Q14 (fills at next open ± slippage), Q17 (equity marked
at close). If wrong, the numbers shift but the mechanism stands.

### Test plan

#### End-to-end
- Buy-and-hold on a 5-bar synthetic series yields the exact hand-computed equity
  curve and final return (the acceptance criterion).
- Cached real-data buy-and-hold matches `shares × final_close` within fees.
- Second run with no network reproduces the first run bit-for-bit.

#### Integration
- `YFinanceAdapter` cache miss writes the cache; cache hit reads it and skips the
  network (asserted with a stubbed fetch).
- `SimulatedBroker` rejects an order exceeding available cash and leaves state
  unchanged.

#### Unit
- Next-open fill price = open × (1 + slippage) for a buy; commission deducted.
- Portfolio equity = cash + Σ(position × close).

## V2: Pluggable strategy API + SMA-crossover, with look-ahead guard

**Delivers:** R1, R6

**Build plan**

1. Finalize `Strategy.on_bar(bar, context) -> list[Order]`; `context` exposes
   positions, cash, and a rolling history window with no access to future bars.
2. Implement the strategy loader (discover by name from a strategies package).
3. Implement `sma_crossover` (fast/slow windows from config).
4. Add a `context`-based indicator helper for rolling means.

**Demo:** `trading backtest --strategy sma_crossover --symbol SPY …` prints a
trade blotter (entries/exits) and final metrics; changing the fast/slow windows
in config visibly changes the trades.

**Rests on assumptions:** Q1 (single strategy per run), Q13 (config via TOML +
flags). Low cost if wrong.

### Test plan

#### End-to-end
- SMA crossover on a crafted series produces exactly the expected buy/sell bars.
- A "cheating" strategy that tries to read bar *t+1* cannot — the `context` API
  exposes no future data (the look-ahead guard; acceptance criterion).

#### Integration
- Strategy loader resolves a strategy by name and rejects an unknown name with a
  clear error.

#### Unit
- Rolling-mean helper matches a reference computation and never includes the
  current unseen future.

## V3: Performance report and equity-curve output

**Delivers:** R4

**Build plan**

1. Compute metrics from the equity curve: total & annualized return, Sharpe
   (daily returns, rf = 0), max drawdown, win rate.
2. Render a text report (summary + trade blotter) to stdout.
3. Write `equity_curve.csv` and, if `matplotlib` is available, an optional
   `equity_curve.png`.

**Demo:** any backtest ends with a readable summary table and writes the CSV;
open the CSV/PNG to see the curve.

**Rests on assumptions:** Q17 (Sharpe basis and rf = 0). If wrong, one constant
changes.

### Test plan

#### End-to-end
- A known monotonic-up equity curve reports positive return, Sharpe > 0, and zero
  drawdown; a known dip reports the exact max drawdown (acceptance criteria).

#### Integration
- Report writer emits a well-formed CSV with one row per trading day.

#### Unit
- Max-drawdown, Sharpe, and win-rate functions match hand-computed values on
  small fixtures.

## V4: Paper mode on recent daily data (same engine, wall-clock)

**Delivers:** R5, R7 (interface readiness)

**Build plan**

1. Implement a wall-clock clock and a `RecentDataAdapter` (or yfinance in
   recent-window mode) that yields only **completed** daily bars.
2. Add `trading paper --strategy … --symbol …` reusing the engine, broker, and
   portfolio unchanged; append each bar's state to a session log.
3. Ensure a paper session can start, persist its running state to the result dir,
   and print status per new completed bar.

**Demo:** run `trading paper …`; on each new completed daily bar the session logs
the strategy's decision and simulated fills, and the equity updates — visibly the
same accounting as backtest, only paced by the calendar.

**Rests on assumptions:** the "latest" daily bar is only acted on once complete
(avoids acting on a partial/forming bar). If wrong, decisions use unfinished data.

### Test plan

#### End-to-end
- Fed a scripted sequence of "newly completed" bars via a fake clock/feed, paper
  mode places the same orders the backtest would for the same bars (parity —
  acceptance criterion).

#### Integration
- The wall-clock clock waits for and only emits completed bars (driven by a fake
  clock, no real waiting in tests).

#### Unit
- A forming/partial latest bar is excluded until marked complete.

## Roadmap (out of this milestone)

- `AlpacaBroker` (real paper API) and an Alpaca data adapter — same interfaces
  (ADR-0004, ADR-0003).
- Intraday/tick frequency; other asset classes (each its own ADR).
- Parameter optimization / walk-forward as an outer sweep over runs.
- Web dashboard for live equity/positions.
