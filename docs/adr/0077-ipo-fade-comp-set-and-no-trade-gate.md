# ADR-0077: Trade only if pre-registered gates pass; the comp evidence is thin

- **Status:** accepted
- **Date:** 2026-11-12
- **Deciders:** project owner (pre-register), agent (numbers)
- **Plan:** [`docs/anthropic-ipo/PLAN.md`](../anthropic-ipo/PLAN.md)

## Context

`research/ipo-fade/ipo_comps.py` measures 21 US IPOs (2023-09 to 2026-07; every
name the agent could verify, not a complete >$1B list, which no free source gave)
from the **day-2 close**, the first point options can trade (roughly the second
business day after listing). Offer prices are UNVERIFIED against prospectuses.

Result: down at +20 trading days in 16/21 (median -11.7%, mean -8.0%). But the
effect is **not stronger for bigger pops**: pop of 50% or more, down 3/5; 25-50%,
down 4/4 (n=4); under 25%, down 9/12. Right tail: CRCL +90%, CRWV +326% by day 60.
The four largest offerings (ARM, CBRS, SPCX, SKHY) were all down at +20.
Illustratively, with a guessed 100% IV the 20-day ATM put costs ~11% of spot,
about the median fall: edge over the premium is thin and rests on the IV guess.

## Decision

Pre-register before ANT's first print (rules in PLAN.md): enter only if the
hype gate and the price gate both pass on day 3 or later, otherwise **no trade**.
Skipping is a valid, expected outcome. The rules are not changed after the first
print. The result is recorded as one observation, never as evidence the thesis
works or fails.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Trade ANT regardless of price | That is trading the open-day story, the thing being protected against |
| Tune gates on the comp table | n=21 and hindsight-selected; would overfit |
| Wait for a bigger sample first | ANT lists now; the point is a disciplined single decision |

## Consequences

Likely zero trades if IV is rich, which is the honest outcome. The comp set stays
incomplete and its offer prices unverified until someone checks the prospectuses.
