"""Read a *running* paper session's on-disk artifacts into a normalized view (ADR-0075).

The finished-run dashboard (:mod:`trading.dashboard.payload`) reads a single
``result.json`` that is written exactly once, at the very end of a session
(``PaperSession.finalize()``). There is no way to watch a live session with it —
the file does not exist until the session is over. This module is the live
counterpart: it reads the three artifacts a ``trading paper --out DIR`` session
grows *while it runs* (ADR-0033/ADR-0043/ADR-0048) and normalizes them into the
same shape a renderer wants, the way :func:`trading.dashboard.payload.build_payload`
does for a finished run.

**Read-only over the artifact files, and nothing else.** This module never
imports :mod:`trading.engine` or :class:`~trading.engine.PaperSession`, never
reaches into a running process, and opens no socket of its own — it only reads
whatever bytes happen to be on disk at call time. That is what makes a bug here
structurally incapable of touching a live trading run: the worst this code can
do is show stale or blank data, never disturb the session it is reading.

Three files, all optional at the instant this is called:

* ``paper_state.json`` — overwritten atomically every bar (:func:`trading.cli.
  _persist_state`), so a reader never sees a half-written file, only the
  previous bar's state or the latest one. Absent before the first completed bar.
* ``paper_session.log`` — appended every bar, one line per
  :func:`trading.cli._format_bar`, never truncated mid-session. Not written
  atomically, so a reader mid-append could in principle see a torn last line;
  :func:`parse_log_line` simply fails to parse it and it is dropped, exactly the
  way :mod:`trading.divergence`'s crash-safety story treats a torn CSV row.
* ``fill_divergence.csv`` — appended as fills settle (only when the session ran
  with ``--divergence``), via :class:`trading.divergence.DivergenceJournal`.
  Absent entirely on a session that never asked for divergence tracking, which
  is a normal case, not an error.

Every reader below degrades to ``None``/empty on a missing, empty, or malformed
file rather than raising — a dashboard reading a live session must never crash
just because it caught the session between two writes.
"""

from __future__ import annotations

import csv
import io
import json
import re
import statistics
from pathlib import Path
from typing import Any

from trading.dashboard.payload import (
    CHART_HEIGHT,
    CHART_WIDTH,
    axis_bounds,
    points_attr,
    polyline_points,
)
from trading.divergence import MIN_PAIRED_FILLS

STATE_FILENAME = "paper_state.json"
LOG_FILENAME = "paper_session.log"
DIVERGENCE_FILENAME = "fill_divergence.csv"

# How many of the most recent log lines the payload carries for the "recent
# bars" panel, and how many chart points it keeps — both bounded so a
# long-running session (weeks at `--interval 1m`) cannot make every poll parse
# and ship an ever-growing file.
DEFAULT_LOG_TAIL_LINES = 40
DEFAULT_MAX_CHART_POINTS = 2000

# `_format_bar`'s leading stamp is either a bare date (daily) or date + "%H:%M"
# (intraday, ADR-0022/`_format_bar_stamp`), followed by two spaces and
# "decision: ...". Matched verbatim rather than loosely so a genuinely
# unrelated or torn line is dropped instead of mis-parsed.
_STAMP_DECISION_RE = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)  decision: (?P<decision>.*)$"
)

# The trailing segment every `_format_bar` line ends with:
# "equity: $12,345.67  exposure: 12.3%".
_EQUITY_RE = re.compile(r"equity: \$([0-9,]+\.\d+)\s+exposure: ([0-9.]+)%")


def read_state(out_dir: str | Path) -> dict[str, Any] | None:
    """Read ``paper_state.json`` from ``out_dir``, or ``None`` if it says nothing yet.

    ``None`` covers three cases identically, since a caller only needs to know
    "no current state to show": the file does not exist yet (before the
    session's first completed bar), it could not be read, or it is not valid
    JSON. The write side is atomic (``os.replace``, ADR-0048), so a real live
    session never actually produces the third case — this is defence for a
    hand-built or corrupted fixture, not a race this module expects to hit.
    """
    path = Path(out_dir) / STATE_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    return document


def read_log_lines(out_dir: str | Path) -> list[str]:
    """Read every non-blank line of ``paper_session.log``, or ``[]`` if absent."""
    path = Path(out_dir) / LOG_FILENAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Parse one ``_format_bar`` line, or ``None`` if it doesn't match that shape.

    A non-matching line is silently dropped rather than raising: it might be a
    line a future format adds new segments to, or the tail end of a write the
    log's own append-and-flush caught this reader mid-way through (the log is
    not written atomically, unlike ``paper_state.json``).
    """
    segments = line.split("  |  ")
    head = _STAMP_DECISION_RE.match(segments[0])
    if head is None:
        return None

    fills: str | None = None
    clamps: list[str] = []
    rejections: list[str] = []
    halted_now = False
    resumed_now = False
    equity: float | None = None
    exposure: float | None = None

    for segment in segments[1:]:
        if segment.startswith("fills:"):
            fills = segment[len("fills:") :].strip()
        elif segment.startswith("CLAMP "):
            clamps.append(segment)
        elif segment.startswith("REJECT "):
            rejections.append(segment)
        elif segment.startswith("HALT:"):
            halted_now = True
        elif segment.startswith("RESUME:"):
            resumed_now = True
        else:
            match = _EQUITY_RE.search(segment)
            if match is not None:
                try:
                    equity = float(match.group(1).replace(",", ""))
                    exposure = float(match.group(2)) / 100.0
                except ValueError:
                    equity = None
                    exposure = None

    if equity is None or exposure is None:
        # `_format_bar` always ends with the equity/exposure segment; its
        # absence means this line is not a well-formed bar line (e.g. a torn
        # write), so there is nothing trustworthy to report.
        return None

    return {
        "ts": head.group("stamp"),
        "decision": head.group("decision"),
        "fills": fills,
        "clamps": clamps,
        "rejections": rejections,
        "halted_now": halted_now,
        "resumed_now": resumed_now,
        "equity": equity,
        "exposure": exposure,
        "raw": line,
    }


def _chart_geometry(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Precompute inline-SVG geometry for the live equity curve.

    Reuses :mod:`trading.dashboard.payload`'s pure chart math verbatim rather
    than re-deriving it — the finished-run and live views must draw the same
    kind of line the same way, and the geometry functions are already unit
    tested on known values with no engine and no I/O.
    """
    equities = [p["equity"] for p in points]
    y_min, y_max = axis_bounds(equities)
    xy = polyline_points(equities, CHART_WIDTH, CHART_HEIGHT, y_min, y_max)
    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "y_min": y_min,
        "y_max": y_max,
        "equity_points": points_attr(xy),
        "start_ts": points[0]["ts"] if points else None,
        "end_ts": points[-1]["ts"] if points else None,
    }


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_divergence_summary(out_dir: str | Path) -> dict[str, Any] | None:
    """Summarize ``fill_divergence.csv``, or ``None`` when the file is absent.

    ``None`` means "this session did not run with ``--divergence``" — a normal,
    common case the caller renders as an omitted panel, not an error. A present
    but header-only (or momentarily mid-append) file still returns a summary
    with zero paired fills rather than ``None``, since the file *does* exist and
    the distinction (divergence tracking is on, just with no data yet) is real
    and worth keeping.

    Only rows with both a realized and a modelled slippage figure count as
    "paired" — mirroring :data:`trading.divergence.MIN_PAIRED_FILLS`'s own
    reading, imported here as a constant rather than duplicated. A row that
    fails to parse (a torn trailing write) is skipped rather than raising:
    ``csv.DictReader`` already tolerates a short/ragged final row by filling
    missing fields with ``None``, so this only needs to guard the float
    conversions.
    """
    path = Path(out_dir) / DIVERGENCE_FILENAME
    try:
        text = path.read_text(encoding="utf-8", newline="")
    except OSError:
        return None

    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error:
        return {
            "row_count": 0,
            "paired_count": 0,
            "min_paired_fills": MIN_PAIRED_FILLS,
            "sufficient_sample": False,
            "mean_realized_slippage_bps": None,
            "median_realized_slippage_bps": None,
            "mean_modelled_slippage_bps": None,
        }

    realized: list[float] = []
    modelled: list[float] = []
    for row in rows:
        r = _to_float(row.get("realized_slippage_bps"))
        m = _to_float(row.get("modelled_slippage_bps"))
        if r is not None and m is not None:
            realized.append(r)
            modelled.append(m)

    paired = len(realized)
    return {
        "row_count": len(rows),
        "paired_count": paired,
        "min_paired_fills": MIN_PAIRED_FILLS,
        "sufficient_sample": paired >= MIN_PAIRED_FILLS,
        "mean_realized_slippage_bps": statistics.fmean(realized) if realized else None,
        "median_realized_slippage_bps": statistics.median(realized) if realized else None,
        "mean_modelled_slippage_bps": statistics.fmean(modelled) if modelled else None,
    }


def build_live_payload(
    out_dir: str | Path,
    *,
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
    max_chart_points: int = DEFAULT_MAX_CHART_POINTS,
) -> dict[str, Any]:
    """Build the normalized live-session view a renderer/API endpoint consumes.

    Every read below is independently defensive (see the module docstring), so
    this function itself never raises on a missing directory, a session that
    has not produced its first bar yet, or a session run without
    ``--divergence`` — those are exactly the states the returned payload is
    supposed to represent, not failures.
    """
    out_path = Path(out_dir)
    state = read_state(out_path)
    log_lines = read_log_lines(out_path)
    parsed = [point for line in log_lines if (point := parse_log_line(line)) is not None]
    if max_chart_points and len(parsed) > max_chart_points:
        parsed = parsed[-max_chart_points:]
    divergence = read_divergence_summary(out_path)

    return {
        "out_dir": str(out_path),
        "state": state,
        "log_tail": log_lines[-log_tail_lines:] if log_tail_lines else list(log_lines),
        "chart": _chart_geometry(parsed),
        "divergence": divergence,
    }
