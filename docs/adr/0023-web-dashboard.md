# ADR-0023: A web dashboard that visualizes a run's `result.json`

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

A completed run already emits a canonical, machine-readable artifact —
`report.result_to_dict` / `write_result_json`, versioned by
`RESULT_SCHEMA_VERSION` — carrying the equity curve, an optional benchmark curve,
the full `PerformanceMetrics`, the fills blotter, and the guardrail record
(clamps, rejections, halt state). Until now the only way to *read* a run was the
text summary and the equity CSV. A visual view — the equity curve with its
benchmark overlay, the metrics block, the fills, and what the guardrails did — is
the natural next consumer of that artifact.

The design questions this slice settles: (1) what does the dashboard read, (2)
does it need a server at all, and (3) where does the load-bearing logic live given
this repo's hard constraints — the build sandbox is offline and the dependency
lock is frozen, so a heavyweight web framework cannot simply be added to the
default install.

## Decision

**The dashboard reads exactly the canonical `result.json`, nothing else.** It is
a pure consumer of the ADR-documented schema: it validates `schema_version`
against the `RESULT_SCHEMA_VERSION` it was built for and refuses a mismatch with a
clear error, rather than silently mis-parsing. It never touches the engine, the
broker, or a data adapter, so it inherits every domain invariant for free and
cannot perturb a run.

**Two output modes over one page.** A **static export** emits a single,
fully self-contained `.html` file with the run data inlined — no external or CDN
references of any kind — that opens over `file://`, offline, with no install. This
is the load-bearing artifact. An optional **interactive server** (FastAPI) serves
the *same* page plus a `GET /api/result` JSON endpoint; it re-reads the file per
request so a re-run shows fresh numbers on refresh. The two share one renderer, so
the page cannot fork between them.

**The render/payload core is pure standard library and fast-gate tested.**
`dashboard/payload.py` (load + schema check + SVG geometry) and
`dashboard/static_export.py` (the self-contained HTML, an inline `<svg>` chart,
the embedded run JSON) import nothing third-party. The equity chart is inline SVG
computed by small pure functions on plain floats, so the whole visual path is unit
tested offline with no browser and no network.

**FastAPI is a lazily imported optional extra — the same pattern as `alpaca-py`
(ADR-0018).** `dashboard/server.py` imports `fastapi`/`uvicorn` *inside* its
functions, behind a guard that raises a clear `ImportError` naming the `dashboard`
extra when they are absent. Importing the dashboard package therefore never
requires FastAPI, and the fast gate proves the stdlib core with FastAPI *not*
installed; the server test `importorskip`s it and skips cleanly. Declaring the
`dashboard` optional-dependency extra (and regenerating the lock) and wiring a
`trading dashboard` CLI command are a separate single-owner integration change, so
this slice adds neither — the lazy guard makes the server usable the moment the
extra is installed, without the default install carrying a web framework.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Server-only design, no offline artifact | Fails the offline/frozen-lock reality: with no FastAPI in the sandbox there'd be nothing to look at. A self-contained HTML file is the honest baseline; the server is a convenience on top. |
| Streamlit / Dash / a plotting-heavy stack | Large, transitively deep dependencies that can't be added under the frozen lock, and they'd own the page instead of consuming our schema. Overkill for one run's static view. |
| A JS charting library (Chart.js/D3) from a CDN | Violates the strict offline, zero-external-reference requirement; the file must render over `file://` with the network off. Inline SVG needs no dependency. |
| Add `fastapi` to the default dependencies | Puts a web server in every install of a backtest bench and forces a lock change this slice can't make offline. Optional, lazily-imported extra keeps the core lean. |
| Re-derive metrics/curves inside the dashboard | Duplicates engine/metrics logic and invites drift. The dashboard reads the one canonical artifact and computes only presentation geometry. |

## Consequences

- A run is now viewable with zero install: `write_html` produces a portable file
  that opens anywhere, offline. The server is strictly additive.
- The dashboard is only as trustworthy as the `result.json` it reads; it launders
  nothing and adds no numbers of its own beyond chart geometry and formatting.
- The schema-version check makes the coupling explicit: a future schema bump forces
  a matching dashboard update instead of a silent misread, and `metrics` is rendered
  generically so new metric fields surface without a code change here.
- New consumers (a CI badge, a comparison view) can read the same artifact; the
  server layer stays thin because all substance lives in the pure-stdlib core.
- Forecloses nothing: the intraday/other-frequency work forward-fits, since the
  dashboard treats `frequency` as an opaque label and plots whatever curve it is given.
