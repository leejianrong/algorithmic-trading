# ADR-0011: Fractional-share quantities

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)
- Amends: ADR-0007 (which sized to whole shares)

## Context

The default account is ~$1,000 (Q22). Under whole-share sizing (ADR-0007's
original rule), target-percent sizing breaks down at that scale: a 20% target on
$1,000 is $200, so any stock above ~$200 — SPY, QQQ, most blue-chips — is
unbuyable, and mid-priced names round off so far that realized weights drift well
away from target, defeating the exposure guardrails (ADR-0009). Restricting the
universe to low-priced stocks "fixes" this only by introducing a quality/liquidity
bias (low nominal price skews small-cap, volatile, thinly traded), which is a
strategy decision smuggled in as a sizing detail. Crucially, a real $500–$1,000
account at Alpaca/Robinhood already trades fractionally — so fractional shares are
the *more* faithful rehearsal for real capital, not a departure from it.

## Decision

Share quantities are fractional throughout: `Order.qty`, `Fill.qty`, and
`Position.qty` are floats. Because fractional quantities carry floating-point
residue, "flat" and "over-sell" are tolerance comparisons against a shared
`SHARE_EPS` (a position within `SHARE_EPS` of zero is closed; a sell within
`SHARE_EPS` of the held size is allowed), not exact-zero checks. Target-percent
sizing (ADR-0007) therefore hits its mark rather than flooring to whole shares,
and the full equity universe is usable at small capital.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Whole shares + low-priced universe | No code change, but bakes in a quality/liquidity bias and makes weights/exposure caps approximate exactly when the account is smallest — flattering, biased results. |
| Whole shares, larger default capital | Contradicts the owner's real situation (Q22); the bench should rehearse the account they actually have. |
| Decimal-based quantities | More exact than float, but heavier and awkward across pandas/numpy; float with an explicit epsilon and a defined rounding precision is sufficient for shares. |

## Consequences

- Buys: target weights and exposure limits that actually hold at $1,000, the full
  US-equity universe (SPY/QQQ/blue-chips), honest diversification, and parity with
  how the eventual live account trades.
- Costs: float-precision discipline — tolerance comparisons instead of `== 0`, and
  a defined share-rounding precision when the sizing layer lands (V2). A real
  broker later (ADR-0004) must handle non-fractionable symbols and the fact that
  fractional orders are typically notional market orders (no fractional limit
  orders); the `SimulatedBroker` need not model that yet, but the design
  acknowledges it.
- Forecloses: nothing; a whole-share mode remains expressible as a rounding policy
  in the sizing layer if a strategy ever wants it.
- Now true: tests assert fractional buys, tolerance-based flattening, and
  over-sell rejection just beyond tolerance; the "whole-share only" invariant is
  removed.
