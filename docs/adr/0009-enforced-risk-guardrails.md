# ADR-0009: Risk guardrails enforced in-engine

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The destination is real capital, where the difference between a bad day and a
blown account is whether limits are *enforced* or merely *reported*. Building the
habit and the machinery now — in paper and backtest, where mistakes are free —
is far cheaper than discovering a missing kill switch live. The guardrails must
sit on the shared execution path (ADR-0002) so they protect every mode.

## Decision

Risk guardrails are enforced inside the engine, on by default, configurable. Two
layers:

1. **Pre-trade check** on every order: sufficient cash, per-symbol max position
   size (% of equity), and max gross exposure. A breaching order is rejected (or
   clamped, for target weights over the cap) with a logged reason.
2. **Portfolio monitor** each bar: if drawdown from the equity peak, or a single
   day's loss, exceeds configured thresholds, halt new entries (and optionally
   flatten) for the session.

Limits live in config with sane defaults (e.g. 25% max position, 100% max gross
exposure, 20% drawdown halt) and are overridable per run.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Basic checks only (reject nonsensical orders, limits later) | Leaves you paper-trading without the rails you'll need live — the habit isn't built where mistakes are free. |
| Report risk, never enforce | A runaway strategy isn't stopped; unacceptable as a real-capital rehearsal. |

## Consequences

- Buys: safe-by-default behavior, the same protection in backtest and paper, and
  a realistic picture of how limits shape returns (they will sometimes block a
  trade the strategy wanted).
- Costs: guardrails can clamp or reject legitimate orders and complicate
  sizing/accounting; thresholds are judgement calls that need tuning, and the
  halt logic adds state to the engine.
- Forecloses: nothing; more sophisticated risk models (volatility targeting,
  per-sector caps) are additive later.
- Now true: tests must prove the exposure cap clamps an over-target order and the
  kill switch halts entries on a scripted drawdown; the report must surface which
  orders were rejected/clamped and whether a halt fired.
