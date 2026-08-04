"""Pure-stdlib load + normalize of a run's canonical ``result.json`` (ADR-0023).

This module is the load-bearing core of the dashboard and has **no third-party
imports**: it reads the machine-readable document written by
:func:`trading.report.write_result_json`, checks its ``schema_version`` against
the :data:`~trading.report.RESULT_SCHEMA_VERSION` this build understands, and
returns a normalized in-memory view the renderer consumes — the raw document
plus a precomputed inline-SVG chart geometry for the equity curve (and the
benchmark overlay, when present).

The chart helpers (:func:`axis_bounds`, :func:`polyline_points`,
:func:`points_attr`) are small, deterministic pure functions over plain floats,
so they can be unit-tested on known values with no engine and no I/O.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading.report import RESULT_SCHEMA_VERSION

# Fixed SVG user-space viewport for the equity chart. The rendered page scales
# this responsively via viewBox; the numbers only fix the coordinate system the
# polyline points are computed in.
CHART_WIDTH = 800.0
CHART_HEIGHT = 300.0


def _equities(records: list[dict[str, Any]]) -> list[float]:
    """Pull the ``equity`` series out of a list of equity-curve records."""
    return [float(record["equity"]) for record in records]


def axis_bounds(*equity_series: list[float]) -> tuple[float, float]:
    """The shared ``(y_min, y_max)`` spanning every non-empty equity series.

    Combining the strategy and benchmark series here means both are drawn against
    one vertical scale, so the overlay is comparable. Returns ``(0.0, 1.0)`` when
    there is nothing to plot, so callers never divide by a degenerate range.
    """
    values = [v for series in equity_series for v in series]
    if not values:
        return (0.0, 1.0)
    return (min(values), max(values))


def polyline_points(
    equities: list[float],
    width: float,
    height: float,
    y_min: float,
    y_max: float,
) -> list[tuple[float, float]]:
    """Map an equity series to SVG ``(x, y)`` points in a ``width`` by ``height`` box.

    ``x`` spreads the points evenly across the width (a single point sits at the
    left edge); ``y`` is flipped so higher equity is nearer the top, since SVG's
    y-axis grows downward. A flat series (``y_max == y_min``) is drawn on the
    vertical mid-line rather than dividing by a zero span. Coordinates are rounded
    to three decimals to keep the emitted attribute compact and stable.
    """
    n = len(equities)
    if n == 0:
        return []
    span = y_max - y_min
    points: list[tuple[float, float]] = []
    for i, equity in enumerate(equities):
        x = 0.0 if n == 1 else (i / (n - 1)) * width
        # A flat series (zero span) is drawn on the vertical mid-line.
        y = height / 2.0 if span <= 0 else height - (equity - y_min) / span * height
        points.append((round(x, 3), round(y, 3)))
    return points


def points_attr(points: list[tuple[float, float]]) -> str:
    """Render ``(x, y)`` points as an SVG ``points="x,y x,y …"`` attribute value."""
    return " ".join(f"{x},{y}" for x, y in points)


def _chart_geometry(
    equity_curve: list[dict[str, Any]],
    benchmark_curve: list[dict[str, Any]] | None,
    width: float = CHART_WIDTH,
    height: float = CHART_HEIGHT,
) -> dict[str, Any]:
    """Precompute everything the inline SVG needs from the two equity series."""
    equities = _equities(equity_curve)
    bench = _equities(benchmark_curve) if benchmark_curve else []
    y_min, y_max = axis_bounds(equities, bench)

    equity_points = polyline_points(equities, width, height, y_min, y_max)
    bench_points = polyline_points(bench, width, height, y_min, y_max) if bench else []

    def _edge_ts(records: list[dict[str, Any]], index: int) -> str | None:
        return str(records[index]["ts"]) if records else None

    return {
        "width": width,
        "height": height,
        "y_min": y_min,
        "y_max": y_max,
        "has_benchmark": bool(bench),
        "equity_points": points_attr(equity_points),
        "benchmark_points": points_attr(bench_points) if bench else None,
        "start_ts": _edge_ts(equity_curve, 0),
        "end_ts": _edge_ts(equity_curve, -1),
    }


def build_payload(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize an already-parsed ``result.json`` document for the renderer.

    Validates ``schema_version`` (see :func:`load_document`), then returns the raw
    document under ``"document"`` alongside a ``"chart"`` block of precomputed SVG
    geometry. Keeping the untouched document lets the static export embed it
    verbatim and the server hand it back over ``/api/result`` unchanged.
    """
    _check_schema(document)
    equity_curve: list[dict[str, Any]] = document.get("equity_curve", []) or []
    benchmark_curve = document.get("benchmark_curve")
    return {
        "document": document,
        "chart": _chart_geometry(equity_curve, benchmark_curve),
    }


def _check_schema(document: dict[str, Any]) -> None:
    """Raise a clear :class:`ValueError` unless the document's schema matches."""
    version = document.get("schema_version")
    if version != RESULT_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported result.json schema_version "
            f"{version!r}: this dashboard build understands "
            f"{RESULT_SCHEMA_VERSION!r}. Regenerate the result with a matching "
            "version of the trading bench."
        )


def load_document(path: str | Path) -> dict[str, Any]:
    """Read and parse a ``result.json`` at ``path``, validating its schema version.

    Returns the raw parsed document (the exact shape
    :func:`trading.report.result_to_dict` emits). Raises :class:`ValueError` on a
    schema-version mismatch. The server's ``/api/result`` returns this directly.
    """
    text = Path(path).read_text(encoding="utf-8")
    document: dict[str, Any] = json.loads(text)
    _check_schema(document)
    return document


def load_payload(path: str | Path) -> dict[str, Any]:
    """Load a ``result.json`` from ``path`` and return the normalized renderer view."""
    return build_payload(load_document(path))
