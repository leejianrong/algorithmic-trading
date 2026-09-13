# ADR-0075: A live view of a running paper session, read from its own artifacts only

- **Status:** accepted
- **Date:** 2026-09-13
- **Card:** KAN-712 ("Live view of a running paper session in the browser")
- **Builds on:** ADR-0023 (the web dashboard: `payload.py` reader +
  `static_export.py` renderer + a lazy FastAPI `server.py`), ADR-0033/0043 (a
  live session's on-disk artifacts survive being stopped), ADR-0048 (the
  `DivergenceJournal`'s explicit note: "readable while the session is still
  running" is what this card needed)

## Context

The existing dashboard reads a run's `result.json`, written exactly once by
`PaperSession.finalize()` / `Engine._finalize` at the very end of a session. So
it can only ever show a *finished* run — there is no way to watch a live
`trading paper --live` session's trades as they happen, which is exactly the
gap ADR-0048 named ("what KAN-712 needs") when it made
`fill_divergence.csv` readable mid-session.

A live session already writes three durable, growing artifacts into its
`--out` directory while it runs, none of which needed to change for this card:

- `paper_state.json` — the latest snapshot, overwritten atomically every bar
  (`cli._persist_state`, `os.replace`).
- `paper_session.log` — one line per bar, appended and flushed, never
  truncated (`cli._format_bar`).
- `fill_divergence.csv` — settled fill-divergence rows, appended as they close
  when the session ran with `--divergence` (`DivergenceJournal`).

## Decision

**Read-only over the artifact files, and nothing else.** `trading.dashboard.
live_payload` never imports `trading.engine` or `PaperSession`, opens no
socket of its own, and reaches into no running process — it only reads
whatever bytes happen to be on disk when it is called. This is what makes a
bug in the dashboard structurally incapable of touching a live trading run:
the worst it can do is show stale or blank data, never disturb the session
it is reading. None of the three write paths (`_persist_state`, `_format_bar`,
`DivergenceJournal`) changed — this card is a reader of an already-durable
format, not a redesign of one.

**`trading.dashboard.live_payload.build_live_payload(out_dir)`** is the live
counterpart to `payload.build_payload`: it reads `paper_state.json` (`None` if
absent/unreadable/not JSON — a real live session's writes are atomic, so this
is defence for a hand-built fixture, not a race this module expects to hit),
tails `paper_session.log` (`[]` if absent), parses every well-formed line with
`parse_log_line` (dropped, not raised, if a line doesn't match `_format_bar`'s
shape — the log is *appended*, not atomically replaced, so a reader can in
principle catch a torn last line), and summarizes `fill_divergence.csv` when
present (`None` — a normal case, not an error — when the session never ran
with `--divergence`). Every reader degrades to `None`/empty rather than
raising: a dashboard reading a live session must never crash just because it
caught the session between two writes.

**The equity curve comes from the log, not the state file.**
`paper_state.json` only ever holds the *latest* bar's snapshot (it is
overwritten, not appended); the append-only `paper_session.log` is the only
artifact that accumulates a full history, so `parse_log_line` extracts each
bar's timestamp/equity/exposure from it and `build_live_payload` reuses
`payload.axis_bounds`/`polyline_points`/`points_attr` verbatim to compute the
same SVG geometry the finished-run dashboard's chart does — one chart math
implementation, not two. Both the log tail and the chart point count are
bounded (`DEFAULT_LOG_TAIL_LINES = 40`, `DEFAULT_MAX_CHART_POINTS = 2000`) so a
long-running session (weeks at `--interval 1m`) cannot make every poll parse
and ship an ever-growing file.

**The divergence panel reports its own lightweight summary, not
`divergence.summarize`'s full verdict.** `summarize` operates on in-memory
`FillDivergence` objects from a live `ShadowBroker` — reconstructing those from
CSV rows would mean either duplicating `ShadowBroker`'s bookkeeping or reaching
into the running session, both out of scope. Instead `read_divergence_summary`
computes row/paired counts and mean/median realized-vs-modelled slippage
directly from the CSV's own numeric columns, importing only
`divergence.MIN_PAIRED_FILLS` as a constant (not touching the session) to flag
an under-30-paired-fills sample exactly as the finished-run report does.

**One HTML page per view, not a shared renderer.** `trading.dashboard.
live_view.render_live_html` is new rather than an extension of
`static_export.render_html`, for a reason stated in its own docstring: the live
page is fundamentally polling, has no server-side-only fallback (unlike the
finished-run export, which must render fully with JavaScript disabled to open
over `file://`), and both the initial paint and every 5-second poll
(`POLL_INTERVAL_MS`) go through the *same* JavaScript renderer, seeded on load
from an embedded JSON snapshot the same way `static_export` embeds `RUN_DATA`.
CSS is reused verbatim (`from trading.dashboard.static_export import
_STYLE`) — one style sheet, one dark-mode story, and the two dashboards look
like one product — plus a small addition for states only a live page has (a
halted/running badge, a lost-connection banner during a poll failure).

**A second, independent FastAPI app, not new routes bolted onto the existing
one.** `server.create_live_app(live_dir)` / `server.serve_live(...)` mirror
`create_app`/`serve` exactly (`GET /` — the page; `GET /api/live` — the JSON),
but deliberately skip `create_app`'s upfront "load once to fail fast" step:
the entire point of this view is to work correctly *before* a session has
written anything at all, and `live_payload` already treats that state as
normal. A CLI invocation picks exactly one of the two apps — they are never
combined into one running server, so there is no ambiguity about what `/`
means in a given process.

**CLI: `trading dashboard --serve --live-dir DIR`, refused under
`--static`.** `--live-dir` is an alternative to `--result` under `--serve`
only; passing it with `--static` is a dedicated CLI error (exit 2, before any
file is touched) rather than a silent no-op, because a live view has nothing
to export statically — there is no snapshot-in-time to freeze into a single
HTML file that would still mean anything by the time someone opened it.

## Alternatives considered

| Option | Why not |
|---|---|
| Mount `/live` and `/api/live` on the *same* app as the finished-run dashboard | The two modes are mutually exclusive per invocation (`--result` vs. `--live-dir`) and need different startup behavior (`create_app` fails fast on a bad `result.json`; the live app must not fail on one that doesn't exist yet). One app trying to serve both would need to special-case which routes are "live" on every request; two small apps is less code, not more. |
| Reconstruct `FillDivergence` objects from the CSV and call `divergence.summarize` | Would either duplicate `ShadowBroker`'s live bookkeeping or require reaching into the running session — both cross the "read-only over the artifact files" boundary this card is built to respect. A CSV-native summary answers the same question (is the model's 5 bps holding up?) without either. |
| Poll `paper_state.json` for the equity curve too (accumulate client-side history across polls) | Only works from the moment the dashboard is opened — a session watched an hour in would show an hour-late chart with no history before that. The log already has the full history on disk; reading it is strictly more useful and no more code. |
| A no-JavaScript server-rendered fallback, matching the static export | The static export's no-JS requirement exists because it must open over `file://` with no server at all. A live view is inherently a polling client against a running server — JavaScript is not an added constraint here, it is the mechanism, so building a second server-side renderer just to duplicate what the page already needs JS for buys nothing. |
| A CLI flag that also lets `--static` snapshot a live session | Rejected by the card itself: "a live view has no meaning for `--static`, since there is nothing to poll in a static export." A `--static` snapshot of a live directory would be indistinguishable from a stale, misleading finished-run export with no `result.json` to back it. |

## Consequences

- New modules: `src/trading/dashboard/live_payload.py` (reader) and
  `src/trading/dashboard/live_view.py` (renderer). `server.py` gains
  `create_live_app`/`serve_live`; `cli.py`'s `dashboard` command gains
  `--live-dir`. No other module changed — in particular, `engine.py`,
  `cli._persist_state`, `cli._format_bar`, and `divergence.py` are untouched.
- `RESULT_SCHEMA_VERSION` / `result.json`'s schema are untouched — this card
  never reads or writes `result.json` at all.
- Fast, offline unit tests (`tests/unit/test_dashboard_live_payload.py`) cover
  normal mid-session state, a halted session, a session run without
  `--divergence`, a directory with nothing written yet (the race before the
  first completed bar), and a torn/partial log tail. CLI-level `--live-dir`
  validation is fast (`tests/unit/test_cli_interval_dashboard.py`); the actual
  FastAPI routes are integration-only and `importorskip` FastAPI/httpx exactly
  like the existing `test_dashboard_server.py`
  (`tests/integration/test_dashboard_live_server.py`), so the fast gate stays
  green with no `dashboard` extra installed.
- Verified by hand against a real session: `trading paper --once --source
  synthetic --divergence --out DIR`, then `build_live_payload(DIR)` and
  `render_live_html(...)` against the real files it wrote, and
  `trading dashboard --serve --live-dir DIR` served over HTTP and inspected
  with `curl` (`GET /` and `GET /api/live` both 200, correct JSON shape).
- Known gap, named rather than silently skipped: the log-derived equity curve
  has no data before the dashboard first reads the log, but *does* have the
  session's entire history back to bar one (unlike a client-side accumulation
  scheme) — the gap is that a session that ran for a very long time at a very
  fine interval could in principle produce a log large enough that reparsing
  it every poll becomes noticeably slow; `DEFAULT_MAX_CHART_POINTS` bounds the
  rendered series but `read_log_lines` still reads the whole file. Not
  measured to matter at any interval this bench currently runs (a `1m` session
  running for days is already an unusual case per ADR-0049's own silence-
  tolerance discussion), and left as a follow-up rather than adding an
  unverified optimization now.
