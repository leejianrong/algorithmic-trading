# ADR-0084: A paired win rate against the diversified baseline

Status: accepted. Closes the gap ADR-0071 recorded: the ADR-0039 paired
bootstrap only ever read the `--benchmark` curve.

## Context

`--diversified-baseline` (ADR-0071) is the harder of the two bars, yet "beats it in
X% of resamples" was only available against a single buy-and-hold symbol.

## Decision

With **both** `--bootstrap` and `--diversified-baseline`, `cli.backtest` calls the
existing `metrics.paired_bootstrap` on (strategy curve, baseline curve) with the
same resamples, seed and default block length as the benchmark path. No new
statistics: `paired_bootstrap` already aligns on timestamps and draws one index
sequence applied to both series. The result is a `baseline_paired` argument to
`summarize` / `write_result_json`, printed as `Beats baseline:` under the baseline
block and nested as `diversified_baseline.paired` in `result.json`, omitted when
absent (`RESULT_SCHEMA_VERSION` stays 1). Caveats mirror the benchmark's: a note when
the curves share too few periods, a note when the block length was cut, and a
warning when the win rate is not decisive (between 5% and 95%).

Without `--bootstrap` or without `--diversified-baseline` nothing is computed or
printed; a run without the new combination is byte-identical (hashed before/after).
The paired figure is not folded into `SignificanceReport`, which is one
strategy-vs-one-benchmark record.
