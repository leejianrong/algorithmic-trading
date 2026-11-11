# ADR-0086: `paper --divergence` puts its summary in result.json and the static dashboard

- **Status:** accepted
- **Date:** 2026-11-12
- **Context ADRs:** 0023, 0038, 0048, 0060, 0061, 0066/0067 (omitted-key convention), 0075

## Context

ADR-0038's report existed only as printed text and `fill_divergence.csv`, so the
dashboard (which reads `result.json`) could not show the one measurement the bench
exists to make. ADR-0075's live view already shows a CSV-derived divergence panel.

## Decision

`divergence.divergence_to_dict(summary, records)` serialises what `render_report`
prints: the `DivergenceSummary` fields, `_verdict` verbatim, a buy/sell split over
the same comparable rows, a `reference_staleness` block only when the tape skipped,
and `rows` = `divergence_rows` (the CSV's flat strings). `result_to_dict(divergence=)`
adds it as a top-level `divergence` key, **omitted entirely** when absent (the
`regimes`/`monte_carlo` convention), so no run without `--divergence` moves a byte.
`RESULT_SCHEMA_VERSION` stays 1. The CLI computes it from the same
`shadow.summary`/`shadow.divergences` the CSV and printed block use.

The static export renders a "Fill divergence" panel only when the key exists: tiles,
the verdict (so a thin sample reads "neither confirmed nor refuted"), the
`NOT MEASURED BY THIS REPORT` fee caveat when the cost model carries a fee or the
market is not `us_equity` (ADR-0060), a staleness note, a side table and the rows.

## Consequences

- `test_cli_divergence`'s byte-identity test now compares `result.json` minus the
  one new key (the CSV stays byte-identical); the flag-off file is unchanged.
- Not changed: `ShadowBroker`, `fill_divergence.csv`, `engine.py`. The live view
  keeps its CSV-derived panel (no verdict wording or fee caveat there yet).
