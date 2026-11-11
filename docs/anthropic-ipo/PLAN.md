# Anthropic IPO post-listing fade: Plan

Status: draft, pre-registration · Milestone: one decision, one trade at most

Not financial advice. The agent that helped write this is made by Anthropic; its
view of ANT's prospects is conflicted and is deliberately not part of this plan.
Never act on non-public information about Anthropic.

## Problem

Hot tech IPOs often pop, then sell off. The owner wants to express that with
options. The risk is not that the thesis is wrong; it is that the open-day story
overrides discipline, and that options on a new IPO are priced for the fade.

## Solution

A written, date-stamped rulebook committed before ANT's first print, plus a
manual checklist on day 3+. Outputs one of: a bear put spread within a $500 cap,
or an explicit no-trade.

## Scope

**In.** The comp measurement (done); pre-registered rules; a day-3 chain
snapshot; one manual trade or none; a journal entry.

**Out.** Buying or shorting shares; short calls; any structure with loss above
the debit; automating or backtesting options in the bench (ADR-0011); trading
before options list; "peripheral" stock plays; any use of non-public information.

## Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| R0 | Never lose more than $500 on this trade | Core goal |
| R1 | Rules committed before ANT's first trade prints | Must-have |
| R2 | A no-trade outcome is recorded as a valid result | Must-have |
| R3 | Day-3 chain snapshot saved for later analysis | Must-have |
| R4 | Comp set reproducible by one command | Must-have |
| R5 | Verify comp offer prices against prospectuses | Nice-to-have |

## Shape

| Part | Mechanism | ADR |
|------|-----------|-----|
| S1 | `research/ipo-fade/ipo_comps.py`: yfinance pull, day-2-close-forward returns, bucket stats | 0077 |
| S2 | Bear put spread only, debit <= $500 | 0076 |
| S3 | Gate rules below, committed before listing | 0077 |
| S4 | Manual execution at the owner's broker (Alpaca is a candidate, Q12; the bench itself has no options calls); journal in `docs/anthropic-ipo/journal.md` | 0076 |

## Pre-registered rules (ASSUMED numbers; edit only before ANT's first print)

1. **No entry before options list** and not before the **close of day 2**. Never
   trade the first print.
2. **Hype gate:** day-2 close is at least 25% above the offer price. Otherwise
   no trade.
3. **Structure:** buy the put nearest ATM, sell the put about 20% below spot;
   expiry 28-35 calendar days out (covers ~20 trading days).
4. **Price gate:** skip if debit exceeds **40% of the spread width**, or if the
   debit exceeds **$5.00 per share ($500 total, one contract)**.
5. **Take profit:** close when the spread is worth **60% of its width**.
6. **Time stop:** close by the end of trading day 22 after listing, or at 7 days
   to expiry, whichever is first. No stop-loss: the debit is the stop.
7. **No averaging, no re-entry, no roll.** One shot.

## Testing approach

Rules are checkable by reading the snapshot: each gate is a number compared to
a number. Comp script is checked by re-running it. No automated tests for the
trade; the bench has no options model.

## Assumed defaults

| ID | Assumed | Cost if wrong |
|----|---------|---------------|
| Q8 | 25% hype gate | May skip a fade or take a weak one |
| Q9 | 40% width / 60% take-profit / day 22 | Different payoff shape |
| Q10 | Comp offer prices from memory/press | Pop buckets shift |
| Q11 | 100% IV for the illustration | Edge sign could flip |

## Open risks

- Comp set is hindsight-picked and incomplete (21 names); fade is not larger for
  bigger pops; right tail is violent (CRWV, CRCL).
- ANT is far larger than any comp except SPCX/SKHY; base rates may not transfer.
- IV at entry may make the gate fail, so the likely outcome is no trade.
- Pricing/listing dates and ANT details are press-sourced and shifting.
