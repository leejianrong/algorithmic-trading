# ADR-0019: Per-sector exposure caps

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

The enforced guardrails (ADR-0009, ADR-0013) cap exposure two ways: a per-symbol
position cap (a single-name concentration limit) and a gross-exposure cap (total
leverage). Neither sees *sectors*. A book can sit fully within both caps — no one
name over 25%, gross under 100% — while quietly holding, say, four tech names at
20% each: 80% of equity riding one macro factor. That is exactly the concentration
a diversified rehearsal for real capital is supposed to avoid, and the existing
caps are structurally blind to it.

ADR-0013 already named per-sector caps as an additive future layer ("richer
monitors ... per-sector caps ... remain additive later"), and the pre-trade
`check()` already computes room under each cap and clamps to the tightest. A
sector cap is the same clamp shape, scoped to the order symbol's sector, so it
drops onto the existing choke point without a new component or a second code path
the two run modes could drift between (ADR-0002).

## Decision

Add two optional, defaulted-off fields to `RiskConfig`:

- `sector_map: Mapping[str, str] | None` — symbol to sector name.
- `max_sector_exposure: float | None` — the fraction of equity allowed per sector,
  validated to `(0, 1]` when set.

Both default `None`. `RiskConfig.unlimited()` leaves them off. The feature is
active only when **both** are set; a map without a cap (or vice versa) is inert.

In `Guardrails.check`, when the feature is on and the order symbol is in the map,
a buy is clamped so its sector's gross exposure — the summed held value of all
same-sector positions, plus same-bar committed exposure in that sector — cannot
exceed `max_sector_exposure * equity`. This is the existing gross clamp restricted
to one sector:

```
allowed_sector = (max_sector_exposure * equity - current_sector - committed_sector) / price
allowed        = min(order.qty, allowed_position, allowed_gross, allowed_sector)
```

Concretely:

- A symbol absent from `sector_map` is unconstrained by the sector cap (room = ∞);
  symbols in different sectors never cross-limit — each sector has its own budget.
- The equity denominator stays the pre-trade snapshot, consistent with the
  position/gross caps and with sizing.
- A same-bar committed-notional tally is kept **per sector** (mirroring the
  existing gross/position tallies) and reset once per bar at the top of `halted()`,
  so sibling orders in one sector during a multi-symbol rebalance see the room a
  prior one already took (ADR-0013 semantic 6).
- `_binding` names the sector cap (with the sector name) when it is the tightest;
  when the feature is off, `allowed_sector` is `∞`, never the minimum, so the
  position/gross wording and every existing path are byte-identical.

Exits, the kill switch, and cash sufficiency (owned by the broker, ADR-0013) are
untouched.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Enforce sector limits in sizing / the strategy | Forks risk logic out of the enforced choke point; a strategy's own sizing could then bypass it. Keeping it in `Guardrails` means one path for backtest and paper (ADR-0002). |
| A separate sector-risk component beside `Guardrails` | Another object observing the book per bar invites drift and duplicated state; the existing clamp already has the portfolio, prices, and per-bar tally hook. |
| Reject over-cap sector buys outright | Inconsistent with the position/gross caps, which *clamp* (ADR-0013 semantic 1); a sliver of overshoot shouldn't drop the trade. |
| One shared cap for all sectors | Defeats the purpose — that is just the gross cap again. Per-sector budgets are the point. |
| Infer sectors automatically (e.g. from a data provider) | Adds a network/data dependency and hidden magic; an explicit injected map is transparent, offline, and hand-checkable in a unit test. A richer source can populate the same `sector_map` later. |
| Cap net rather than gross sector exposure | The bench has no implicit shorting (ADR-0011), so per sector net equals gross; gross keeps it consistent with the existing gross cap and forward-compatible if shorting ever arrives. |

## Consequences

- Buys: a strategy can no longer pile a whole sector's worth of names past the
  budget while dodging the single-name cap; concentration by factor is now bounded,
  with identical behavior in backtest and paper (ADR-0002). Off by default, so
  nothing changes unless a run supplies both a map and a cap.
- Costs: the guardrails carry a little more per-bar state (the per-sector committed
  tally), and the map is a manual input the operator must keep accurate — a stale
  or partial map silently under-constrains (unmapped symbols are free). The cap
  reads gross, pre-trade-snapshot equity, matching the other caps' minor
  denominator lag.
- Forecloses: nothing. A data-sourced sector map, per-sector *different* limits, or
  net-exposure accounting all remain additive behind the same `sector_map` /
  `max_sector_exposure` fields and the `check()` clamp. The CLI flag to expose it
  is wired separately.
