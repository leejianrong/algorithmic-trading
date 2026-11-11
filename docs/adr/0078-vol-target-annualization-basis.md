# ADR-0078: The vol-target annualizes on the market's year, not 252

Status: accepted. Closes the latent seam ADR-0055 documented and ADR-0057 made
reachable via `--market crypto --target-vol`.

## Context

`Guardrails._compute_vol_scale` annualized the per-bar realized-vol estimate with a
hard-coded `_TRADING_DAYS = 252`. On a 365-day market that understates realized
vol by sqrt(252/365) = 0.8309 and so overstates `target / realized` by 1.2035: a
vol-targeted crypto book was allowed 20.4% more gross than it asked for. The same
constant was wrong for any intraday interval (per-bar sigma times sqrt(252) is not
an annual figure at 5m).

## Decision

`Guardrails(config, *, periods_per_year=252.0)` takes the basis the run already
computes (`Frequency.periods_per_year`, ADR-0054). No second mechanism: the CLI
(`backtest`, `paper`) and `sweep._run_combo` (which already carried
`periods_per_year`, ADR-0059) pass it through. A non-positive value raises. The
default stays the US-equity daily year, so callers that state nothing and every
`RiskConfig` default are unchanged. The unlimited benchmark/baseline guardrails
have no target and are untouched.

## Consequences

- US-equity daily runs are byte-identical (`equity_curve.csv`, `result.json` of a
  synthetic `--target-vol` backtest hashed before and after).
- Opt-in `--target-vol` runs at US-equity *intraday* intervals change: they were
  annualized on the daily year and are now annualized on the interval's year
  (correctly; the old figure was the 8.8x-style error of ADR-0059 one module over).
- `_VOL_WINDOW = 20` is still a bar count, not a duration (unchanged, noted).
