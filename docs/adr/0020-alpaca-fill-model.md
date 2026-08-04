# ADR-0020: Alpaca paper broker — submit-then-poll, reconcile from the account

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

ADR-0004 deferred a real broker; ADR-0002 fixed the ground rule that when one
arrives it must sit behind the same `Broker` seam (`portfolio` / `submit` /
`on_bar`) the engine already drives, so strategy, sizing, and guardrails stay
untouched. The Alpaca client seam (ADR-0017) now exposes exactly what a broker
needs — `submit_order`, `get_order`, `get_account`, `list_positions` — over our
own DTOs. Wiring the paper broker forces choices the simulated broker never faced:

1. A real market order does not fill synchronously. On US-equity **daily** bars,
   an order placed after bar `D` closes fills at the **next session open**,
   possibly hours later. The `Broker.on_bar` contract returns fills *now*.
2. The simulated broker is the accounting authority: it prices the fill, applies
   a `CostModel`, and mutates the `Portfolio`. With a real venue, cash and
   positions are decided *there*, not here.
3. `on_bar` must not block a paper session for hours waiting on a fill.

## Decision

**Submit-then-poll.** `submit(order)` places a market order via
`client.submit_order(...)` and records the returned order id as pending; it does
not wait. `on_bar(bars)` polls each pending order with `client.get_order(id)`
until it reports `filled` / `rejected` or a short, configurable **poll timeout**
elapses (measured and waited via the injected `Clock`, so `FakeAlpacaClient(auto_fill=True)`
settles on the first poll with zero real delay). A filled order becomes a `Fill`
at the venue's `filled_avg_price` / `filled_qty`. An order still working at
timeout **stays pending** and is retried on the next `on_bar`; a rejected order is
dropped with a recorded reason. `bars` is accepted for seam parity but not used
to price — the venue prices.

**Reconcile from the account.** After polling, the broker rebuilds its
`Portfolio` wholesale from `get_account()` (cash) plus `list_positions()`
(positions), every bar, filled or not. It never re-simulates cash and never
applies a `CostModel`: the real fill already carries real costs, and Alpaca paper
commission is zero. The account is the single source of truth. Reconciliation
runs once at construction, so `portfolio` is valid before the first bar.

**No simulated slippage/commission.** Fills are emitted with `commission=0.0` and
the exact venue price; the bench adds nothing on top.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Block `on_bar` until the order fills | A daily fill can be hours out; the paper loop would stall. Timeout + retry across bars keeps the loop live. |
| Re-simulate cash locally and treat the account as a cross-check | Two sources of truth drift; the venue is authoritative anyway. Reconcile-from-account has one. |
| Apply the `CostModel` to Alpaca fills | Double-counts costs — the venue fill already includes them (paper commission is zero). |
| A new `AsyncBroker` seam with callbacks | Forks the execution path ADR-0002 forbids; the poll fits the existing synchronous seam. |
| Track pending orders as full `Order` objects | The order id plus `get_order` already returns symbol/side/qty/price; the id is all we must hold. |

## Consequences

- Buys: a real paper venue drops in behind the untouched `Broker` seam; the engine
  and strategies do not change. Under `FakeAlpacaClient` + `FakeClock` the whole
  path runs offline and deterministically in the fast test layer with no waiting.
- Costs / limitations, explicitly accepted:
  - **Deferred fills.** A market order placed after a completed daily bar fills at
    the next session open. The poll times out on that bar and the fill is
    reconciled on a *later* `on_bar` — fills are not synchronous with submission.
  - **No simulated microstructure.** There is no slippage or commission model;
    fills are whatever the venue reports.
  - **Not byte-identical to backtest.** Because the account is authoritative,
    paper accounting can diverge from a simulated backtest of the same bars. This
    is the honest-numbers trade-off ADR-0002 anticipated: one execution *path*,
    but a live venue's book rather than a re-simulated one.
- Forecloses nothing: a live intraday feed or a different venue slots in as a new
  `Broker`/feed behind the same step; the poll timeout and interval are injectable.
