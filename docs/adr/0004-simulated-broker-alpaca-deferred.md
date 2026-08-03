# ADR-0004: Simulated broker in the MVP; real-broker paper API deferred

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

"Paper trading" can mean a broker we simulate ourselves or a real broker's
paper-trading endpoint (e.g. Alpaca). The owner wants both, in that order: a
self-contained simulator now, and Alpaca's paper API on the roadmap. The MVP
must work offline, with no account or key, and must never risk real money.

## Decision

Define a `Broker` interface (`submit`, `on_bar`, `positions`, `cash`) and ship
`SimulatedBroker` as the only implementation this milestone. It fills market
orders at the next bar's open adjusted by configurable slippage, applies a
configurable commission, updates cash and positions, and rejects orders that
exceed available cash. `AlpacaBroker` is a planned future implementation of the
same interface and is out of scope for the MVP.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Alpaca paper API in the MVP | Adds an API key, network dependency, and account setup to the critical path; delays the core backtest goal and can't run offline. |
| Only ever simulate | Rejected as a permanent stance — real-broker parity is a stated goal; the interface exists precisely to admit Alpaca later. |
| Fill at signal bar's close | Reintroduces look-ahead (deciding and filling on the same known price); rejected in favor of next-open fills. |

## Consequences

- Buys: an offline, keyless, deterministic paper/backtest broker, and a seam that
  admits Alpaca without engine changes (upholds ADR-0002).
- Costs: simulated fills are an approximation — real slippage, partial fills, and
  queue position are not modeled, so paper results are optimistic until a real
  broker is wired in.
- Forecloses: nothing; `AlpacaBroker` is additive.
- Now true: the fill model (next-open ± slippage, see PLAN Q14) is an assumption
  worth its own ADR if it proves too crude, and paper-mode results must be
  labeled as simulator estimates, not broker-confirmed.
