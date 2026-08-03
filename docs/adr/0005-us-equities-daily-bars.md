# ADR-0005: MVP scope is US equities, daily bars

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

Asset class and bar frequency drive the data model, the calendar logic, and the
data sources that are viable. Committing to one keeps the MVP small; committing
to too much forces a premature instrument abstraction and a harder clock.

## Decision

The MVP targets **US equities** at **daily** bar frequency. The instrument model
is `Instrument(symbol)`; the market model is a US trading calendar used only to
enumerate valid trading days. Intraday/tick frequency and other asset classes
(crypto, forex, futures, options) are out of scope, but `Bar` carries a full
tz-aware timestamp so intraday is not precluded.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Crypto | Simpler calendar (24/7), but the owner's target is US equities; keyless daily equity data via yfinance is readily available. |
| Multi-asset from day one | Forces an instrument/contract abstraction (rollover, margin, lot sizes) before any strategy runs; highest-risk MVP. |
| Intraday from day one | A wall-clock/session clock and much larger, flakier data volumes; disproportionate for a first cut. |

## Consequences

- Buys: a trivial instrument model, a simple day-stepped clock, and free keyless
  data; fastest path to a working backtest.
- Costs: single-asset assumptions (e.g. "one symbol = one instrument") may leak
  into code and need refactoring for futures/options later.
- Forecloses: nothing structurally — the `Bar` timestamp and adapter seam leave
  room — but multi-asset and intraday will each warrant their own ADR.
- Now true: the calendar component must handle US market holidays and half-days
  so day iteration doesn't invent non-trading days.
