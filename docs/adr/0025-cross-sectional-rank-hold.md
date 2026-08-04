# ADR-0025: Cross-sectional rank-and-hold-top-K strategy

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

Every strategy in the pack so far scores each symbol **in isolation**: SMA
crossover, momentum, and RSI mean-reversion each look at one name's own price
history and decide long-or-flat for that name independently of the rest of the
universe. That is an *absolute* signal — "is AAPL trending up?" — and it says
nothing about how AAPL is doing *relative to* MSFT, NVDA, or the other twenty
candidates.

A large body of equity research is instead **cross-sectional**: rank the whole
universe by a signal (classically trailing return — "relative strength") and hold
the strongest, funding them by staying out of the weakest. This is a different
question — "which names are winning right now?" — and it needs a strategy that
sees the universe at once, not one symbol at a time. The curated `blue20` basket
(ADR-0024) is a natural candidate set for exactly this.

Two forces shape the design. First, a rank recomputed every bar **thrashes**: a
name near the K/K+1 boundary flips in and out daily, generating churn and paying
the cost model (ADR-0007) on every flip for no edge. Second, our sizing is
long-only with fractional shares (ADR-0011) and guardrail-capped (ADR-0009), so
per-name weight must stay under the position cap or the guardrails clamp every
entry.

## Decision

Add `src/trading/strategies/cross_sectional.py` — `CrossSectional`, a normal
`Strategy` registered as `cross_sectional`. **No engine or interface change**: it
reads only `context.history` (past+present, never the future, ADR-0001) and emits
`TargetWeight`s that the V2 sizing layer resolves, exactly like momentum and
mean-reversion.

Behavior, parameterized (all four sweepable via `trading sweep --param`):

- **Score.** On each rebalance, score every symbol present this bar by trailing
  total return over `lookback` bars (default 120): `close[-1] / close[0] - 1`
  from `context.history(symbol, lookback + 1)`. A name without a full lookback
  window is skipped (still warming up); a degenerate non-positive first close is
  skipped rather than dividing by zero.
- **Rank & hold.** Rank scored names by return descending (ties broken by symbol
  for determinism), hold the top `top_k` (default 8) at **equal weight**
  `weight / top_k` (`weight` default 0.9), and emit `TargetWeight(sym, 0.0)` for
  every other symbol present — so a name that drops out of the top-K is exited.
  Long-or-flat only; no shorting.
- **Cadence.** Rebalance only every `rebalance_days` bars (default 21 ≈ monthly):
  the first bar the universe is warm, then every `rebalance_days` bars thereafter.
  Between rebalances the strategy returns no intents and the book is held
  untouched. This is the turnover control — ranks are re-read monthly, not daily.
- **Warmup.** Until at least one symbol has `lookback + 1` closes, stay flat.

The **K↔position-cap interaction** is a documented constraint, not an enforced
one: `weight / top_k` must stay under the per-symbol position cap (ADR-0009) or
the guardrails clamp each entry. The defaults (`0.9 / 8 ≈ 0.1125`) sit safely
under the 0.25 default cap; a small `top_k` with a large `weight` would be
clamped (correct, but wastes the intent). The constructor validates `lookback`,
`top_k`, `weight ∈ (0, 1]`, and `rebalance_days` are positive; it does not know
the cap, so it cannot reject the interaction — the module docstring and this ADR
state it.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Rebalance every bar | Ranks thrash at the K/K+1 boundary; churn pays the cost model daily for no edge. A cadence (or hysteresis) is the standard turnover control; a fixed `rebalance_days` is the simplest, most legible form and is sweepable. |
| Hysteresis band (hold until a name falls below rank K+band) instead of a cadence | Also valid and can be layered later, but it is stateful per-name and harder to reason about than "re-rank monthly". Start with the simpler cadence; a band is a future parameter. |
| Weight by score (more to the strongest) | Concentrates risk in one name and interacts badly with the per-symbol cap. Equal weight across the top-K is the honest, diversified default and keeps the cap math trivial (`weight / K`). |
| A new engine hook so the strategy "sees the whole universe" | Unnecessary — `context.history` already exposes every symbol's past bars, and `on_bar` receives all present bars. Cross-sectional ranking fits the existing seam with zero engine change, which is the whole point (ADR-0002: one execution path). |
| Long/short (short the bottom-K) | Out of scope: the bench disallows implicit shorting (ADR-0011). This is long-only, so it is a relative-strength tilt, **not** market-neutral — it still carries full market exposure. |

## Consequences

- A first **cross-equity** strategy joins the pack: the older strategies answer
  "is this name trending?"; this one answers "which names are winning relative to
  the rest?". It is long-only, so it is not market-neutral — it holds the
  strongest K names and is fully exposed to the market's direction.
- Turnover is controlled by construction: monthly re-ranking (default) means one
  rebalance per ~21 bars, not daily churn. `rebalance_days`, `lookback`, `top_k`,
  and `weight` are all sweepable, so the turnover/responsiveness trade-off is
  tunable via `trading sweep` and walk-forward (ADR-0016).
- No structural risk added: it emits `TargetWeight`s through the same sizing and
  guardrail path as every other strategy, so the position cap, gross-exposure cap,
  sector caps (ADR-0019), and kill switch apply unchanged. The K↔cap constraint is
  a documented tuning caveat, enforced centrally by the guardrails as a clamp.
- Fits `@blue20` (ADR-0024) directly as a candidate universe; verified end to end
  offline on the synthetic adapter.
