# ADR-0052: The 5 bps slippage assumption, measured

- **Status:** accepted
- **Date:** 2026-08-11
- **Supersedes:** `docs/monday-divergence-run.md` (the runbook for this run, now deleted)
- **Builds on:** ADR-0038 (the divergence mechanism), ADR-0004 (the cost model),
  ADR-0034 (the IEX feed), ADR-0042/0047/0048/0049 (what made the session survivable)

## Context

Every backtest this bench has produced assumes each fill lands 0.05% worse than the
reference price — `CostConfig.slippage_bps = 5.0`, described in the code as
"deliberately pessimistic". Nobody had checked it, and no backtest can: a backtest
*is* the assumption. Turnover multiplies it, so on a run showing 600%+ turnover the
assumption is applied many times over a single result.

ADR-0038 built the measurement: a `ShadowBroker` decorator replays each live order
through a throwaway `SimulatedBroker` seeded from a copy of the pre-bar live book, so
both the realized and the modelled fill divide by the **same** reference price (the
next bar's open). Until now it had produced exactly one live row, and that one was an
order parked at a shut venue.

## The run

2026-08-10, `sma_crossover` over `@blue20` at `--interval 5m`, `--source alpaca
--broker alpaca --live --data-feed iex --divergence`, against the Alpaca paper
account. Started 12:05 ET — 2h35m later than planned — and ran to the 16:00 close,
self-terminating at 17:55 after ADR-0049's 60 minutes of silence.

53 bars processed, 63 orders, **60 paired fills**, above `MIN_PAIRED_FILLS = 30`.
Zero guardrail rejections, zero clamps, zero venue refusals, zero absent symbols, and
no warnings or errors in the console. Median order notional $4,748.

## Decision

**Record the measurement. Do not change `slippage_bps` yet.**

```
realized slippage   mean  +0.51 bps   median +0.59   stdev 3.75
                    range -10.20 .. +11.69
modelled                  +5.00 bps
error                     -4.49 bps
better than model         54 / 60
by side             buy   +0.02 bps (n=26 of the paired set)
                    sell  +1.20 bps
```

The model is **conservative by ~4.5 bps**. With n=60 and stdev 3.75, the standard
error is 0.48 bps: an approximate 95% interval is **-0.44 to +1.46 bps**, and 5.00
sits **9.3 standard errors** from the mean. So "well below 5" is robust; the interval
straddles zero, so the *level* is not resolved — we cannot distinguish 0 from 1.5 bps.

We are not changing the constant, for three reasons.

**The measurement is close to its own systematic error.** The reference price comes
from IEX bars while the fill comes from Alpaca's execution engine. IEX carries a few
percent of US volume, and on a $250 mega-cap a one-cent print difference is ~0.4 bps —
the same order as the 0.51 bps measured. The comparison is sound for *bounding* the
model well below 5; it is not precise enough to set a new constant from.

**These are paper fills.** Alpaca's paper engine simulates execution against market
data; it does not route an order to a venue. What was measured is our cost model
against **Alpaca's fill model**. That is the best evidence this bench has ever had and
it is not real-money slippage. Nothing here has been validated against a real
execution.

**One afternoon, one venue, twenty mega-caps, ~$4,700 orders.** It says nothing about
small caps, where spreads are far wider, nothing about size, nothing about stressed
conditions, and nothing about crypto.

## Consequences

- Backtest results have been **understating** returns, most for high-turnover
  strategies. That is the safe direction: no conclusion drawn so far was flattered by
  this assumption. The correction is real but it is not a correctness bug.
- `CostConfig.slippage_bps` stays at 5.0. The honest intermediate step is a
  cost-sensitivity sweep (KAN-618) showing how a conclusion moves between 0.5 and 5
  bps, rather than a single re-tuned constant carrying more precision than the
  evidence supports.
- Pooling further sessions is the cheap way to narrow the interval; the mechanism
  writes one CSV per run and rows are durable as they settle (ADR-0048).

## What the run also demonstrated

Every guard built for this over the preceding weekend was exercised, and two were
observed doing real work after the session ended:

- The **parked-order** case (ADR-0036) is real and persistent: the session's NVDA sell
  was still working at the venue hours later, and it is the single `outcome mismatch`
  in the report (`live pending | model filled`). Flattening the account afterwards hit
  it — Alpaca refused a duplicate sell with `held_for_orders: 22.892754` — and the
  refusal arrived as a classified `OrderRejectedError` carrying the venue's own words
  (ADR-0041), not a traceback.
- The session left **two working BUY orders** (HON, V) that would have filled at the
  next open and rebuilt positions. Nothing in the bench cancels them; that is an
  operator step. See "Known gaps".
- Observation latency was mean 251s, max 310s — an upper bound, since a polling broker
  only notices a fill when it polls.

## Known gaps this run exposed

1. **No market-calendar awareness (KAN-687).** The feed served 6 extended-hours bars
   after the 16:00 close and `_step` processed them, marking equity. No orders were
   submitted on them — `sma_crossover` produced no transitions — so the divergence
   sample is uncontaminated, but that was luck rather than design.
2. **A session ends holding its book.** Nothing liquidates, which is correct for a
   strategy but leaves the account dirty for the next run, and leaves working orders
   that fill at the next open. Flattening is currently manual.
3. **The headline metrics of a part-day run are meaningless and printed anyway.**
   `Sharpe 10.04`, `Annualized +35.25%`, `Turnover 107,106%` — four hours annualized as
   a year. The `Trades/param: 11.7` warning fires correctly, but nothing says "this
   run is too short to annualize". Related to KAN-705.
4. **Real-money execution remains unmeasured**, and cannot be measured on a paper
   account by construction.
