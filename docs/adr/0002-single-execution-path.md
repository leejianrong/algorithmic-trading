# ADR-0002: One execution path shared by backtest and paper trading

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

A recurring failure of home-grown trading tools is that the backtester and the
live/paper runner are separate codebases, so a strategy that looked good in
backtest behaves differently in paper because fills, sizing, or accounting drift
apart. The whole value of a test bench is that the number you see backtesting is
the number you can trust going forward.

## Decision

Backtest and paper modes run the **same** engine, strategy API, broker, and
portfolio. They differ in exactly two injected components: the **data feed**
(historical range vs. recent/live bars) and the **clock** (advance immediately
vs. wait for wall-clock time). The engine depends only on the `DataAdapter`,
`Strategy`, and `Broker` interfaces, never on concrete implementations.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Separate backtest and paper codebases | The exact drift this project exists to eliminate. |
| Shared strategy code but separate runners | Runners re-implement order routing and accounting, so drift reappears in the parts that matter most. |

## Consequences

- Buys: parity by construction — a strategy is written and tested once; paper
  trading reuses proven code.
- Costs: the abstractions (feed, clock, broker) must be defined up front, before
  paper mode exists, adding a little early design cost and indirection.
- Forecloses: nothing — a real broker (ADR-0004) or a live intraday feed slots in
  as a new implementation of an existing interface.
- Now true: the interfaces are load-bearing contracts; changing them is a
  breaking change for every mode and every user strategy.
