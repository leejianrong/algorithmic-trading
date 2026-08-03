# ADR-0007: Target-percent-of-equity position sizing

- Status: Accepted (sizing granularity amended by ADR-0011)
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

A strategy's "buy" has to become a concrete share quantity somewhere. Where that
happens is a core API contract: put it in the strategy and every strategy re-does
risk math; put it in the engine and strategies stay declarative. The owner is
newer to algo trading and is heading toward real money, so the sizing model
should be hard to misuse and should compose with portfolio-level risk limits.

## Decision

Strategies express intent as **target weights** (a fraction of current equity per
symbol, e.g. 0.20). The engine's sizing layer converts a target weight to an order
using `target_weight × equity ÷ latest_price`, then routes it through the
guardrails (ADR-0009), which clamp anything over the position cap. Raw-quantity
orders remain possible for advanced use but target weights are the default path.

**Amended by ADR-0011:** the quantity is a *fractional* share count, not floored
to whole shares, so target weights hit their mark at small capital. The
paragraphs below that reference whole-share flooring are superseded accordingly.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Fixed cash per trade | Intuitive, but doesn't compound with equity and ignores current exposure — a poor fit for portfolio risk limits. |
| Fixed share count | Maximum control, but pushes all risk math onto the user and scales badly across differently-priced symbols. |

## Consequences

- Buys: declarative strategies, sizing that scales with the account and composes
  directly with exposure caps, and a natural fit for multi-symbol allocation.
- Costs: a target weight must be reconciled against the *current* position each
  bar (rebalancing deltas), and whole-share flooring means small accounts or
  high-priced symbols can't hit a target exactly — the engine must handle the
  rounding and residual cash honestly.
- Forecloses: nothing; raw-quantity orders are still supported for edge cases.
- Now true: the report should show realized weights vs. targets so drift from
  rounding is visible.
