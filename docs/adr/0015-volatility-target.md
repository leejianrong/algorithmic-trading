# ADR-0015: Volatility-target exposure scaling

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The V3 guardrails (ADR-0009, ADR-0013) cap gross exposure at a fixed fraction of
equity. A fixed cap treats a calm market and a violent one identically: the same
100% of equity swings far harder in a crisis than in a quiet drift, so realized
portfolio volatility — the thing that actually determines how much a bad day
hurts — drifts uncontrolled. ADR-0009 explicitly named volatility targeting as an
additive future layer ("more sophisticated risk models ... are additive later"),
and the drawdown monitor already observes per-bar equity, so the raw material for
a realized-volatility estimate is already flowing through `Guardrails.halted`.

We want an opt-in way to hold realized volatility near a chosen annualized target
by leaning gross exposure down when the book is turbulent and up when it is calm,
without forking the engine or adding a second risk component the two run modes
could drift between (ADR-0002).

## Decision

Add an optional `target_volatility` (annualized, e.g. `0.10` for 10%) to
`RiskConfig`, default `None` (off). When set, `Guardrails` maintains a rolling
window of portfolio returns from the same per-bar equity the drawdown monitor
marks, estimates realized annualized volatility as the sample standard deviation
of those returns scaled by `√252` (the Q17 Sharpe basis), and scales the
**effective gross-exposure cap** by

```
scale = clamp(target_vol / max(realized_vol, floor), 0, max_scale)
```

The scale is recomputed once per bar inside `halted()` (the existing once-per-bar
hook) and applied in `check()` to the gross-cap term only. Concretely:

- Window: the last 20 completed-bar returns (`_VOL_WINDOW`).
- `floor = 1e-6` guards against a near-flat book dividing by ~zero and demanding
  infinite leverage; `max_scale = 3.0` caps how far a very calm book can lever the
  cap up.
- Fewer than two returns, or `target_volatility` unset, yields `scale = 1.0` — an
  exact no-op, so every pre-existing path (and `RiskConfig.unlimited()`, whose
  gross cap is already infinite) is byte-identical.

Only the gross cap is scaled. The per-symbol position cap is a concentration limit
with a different job and is left fixed. Exits and the kill switch are untouched.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Scale sizing (target weights) instead of the cap | Would fork risk logic into the sizing layer and let a strategy's own sizing bypass it; keeping it in `Guardrails` means one enforced choke point on the shared path (ADR-0002). |
| A separate volatility component beside `Guardrails` | Two components observing equity per bar invites drift and double state; the drawdown monitor already has the equity marks and the once-per-bar hook. |
| EWMA / GARCH volatility estimate | More faithful, but a plain windowed sample stdev is transparent, hand-checkable in a unit test, and enough to prove the plumbing; a richer estimator can replace `_compute_vol_scale` behind the same seam later. |
| Scale the position cap too | Conflates a concentration limit with a volatility budget; a vol-quiet book shouldn't be allowed to pile into a single name. |
| No clamp on the scale | A near-zero realized vol would demand unbounded leverage; the floor and `max_scale` keep the multiplier sane. |

## Consequences

- Buys: realized volatility is actively steered toward the target — the book
  de-risks into turbulence and re-risks into calm — with the same behavior in
  backtest and paper because it rides the shared `Guardrails` (ADR-0002). Off by
  default, so nothing changes unless a run opts in.
- Costs: the guardrails carry a little more per-bar state (the return window and
  the current scale); the estimate is a lagging, windowed one, so it reacts a few
  bars after a regime shift, and the `20`/`252`/`floor`/`max_scale` constants are
  judgement calls that may need tuning. The scaled gross cap can now exceed the
  configured `max_gross_exposure` on a calm book (up to `max_scale`x) — intended,
  but worth remembering when reading the cap value alone.
- Forecloses: nothing; a richer volatility estimator, a per-run window, or a hard
  ceiling that survives scaling all remain additive behind the same config field
  and `_compute_vol_scale` seam. The CLI flag to expose it (`--target-vol`) is
  wired separately.
