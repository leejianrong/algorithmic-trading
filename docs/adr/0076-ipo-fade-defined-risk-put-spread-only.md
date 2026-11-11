# ADR-0076: The IPO-fade trade uses a defined-risk put spread, capped at $500

- **Status:** accepted
- **Date:** 2026-11-12
- **Deciders:** project owner (put-spread structure, $500 cap); agent (rules)
- **Plan:** [`docs/anthropic-ipo/PLAN.md`](../anthropic-ipo/PLAN.md)

## Context

The owner's thesis: a hyped tech IPO spikes, then sells off, and options can
express that. A wider comp set (ADR-0077) shows the fall is common but not
universal, and that the right tail is violent (CRWV +326% at +60 days, CRCL +90%
at +20). Options on a new IPO also trade at very high implied volatility, so a
fall has to beat the premium, not merely occur.

## Decision

- The only permitted structure is a **bear put spread** (buy a near-ATM put, sell
  a further-OTM put, same expiry). No short calls, no naked puts, no futures or
  shares short.
- **Maximum loss is the debit paid, and the debit is capped at $500 total.** At
  likely ANT prices that is one contract; never size by "expected" loss.
- The trade is **manual**. Nothing in `src/trading/` places or models options.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Outright long put | Highest premium drag at IPO-day IV; the spread's short leg offsets it |
| Short call / short stock | Unbounded loss; the comp set has a +326% name |
| Put spread with margin-funded size | The cap is a risk limit, not a budget to exceed |
| Model it in the bench | Bench is long-or-flat equities (ADR-0011); no historical chains to test against |

## Consequences

Buys a hard loss ceiling. Costs: capped upside, a short leg that can be assigned
early (American-style), and a $500 cap that makes the trade one contract, so
outcome is one binary-ish draw (n=1).
