"""Fast, no-infra tests for the dashboard's pure-stdlib payload layer.

Builds a canonical ``result.json`` from a hand-made :class:`BacktestResult` via
:func:`trading.report.write_result_json`, loads it back through
:mod:`trading.dashboard.payload`, and asserts the normalized view plus the
SVG-geometry helpers on transcribed-by-hand values. Also asserts a schema-version
mismatch raises a clear error. No engine, no network, no FastAPI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading.dashboard.payload import (
    CHART_HEIGHT,
    CHART_WIDTH,
    axis_bounds,
    build_payload,
    load_document,
    load_payload,
    points_attr,
    polyline_points,
)
from trading.engine import BacktestResult, EquityPoint
from trading.metrics import PerformanceMetrics
from trading.report import RESULT_SCHEMA_VERSION, write_result_json
from trading.types import Fill, Order, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _metrics() -> PerformanceMetrics:
    return PerformanceMetrics(
        total_return=0.05,
        annualized_return=0.12,
        sharpe=1.1,
        sortino=1.4,
        calmar=0.9,
        max_drawdown=0.03,
        win_rate=0.6,
        turnover=1.5,
        avg_exposure=0.4,
        peak_exposure=0.8,
    )


def _result() -> BacktestResult:
    curve = [
        EquityPoint(_ts(1), 1000.0, 0.0),
        EquityPoint(_ts(2), 1050.0, 0.5),
    ]
    fills = [
        (_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0, 1.0)),
        (_ts(2), Fill("AAA", Side.SELL, 5.0, 110.0, 0.5)),
    ]
    clamps = [
        (Order("BBB", Side.BUY, 20.0), Order("BBB", Side.BUY, 12.0), "gross exposure cap"),
    ]
    rejections = [(Order("CCC", Side.BUY, 3.0), "halted: new entries blocked")]
    return BacktestResult(
        symbols=["AAA", "BBB"],
        starting_cash=1000.0,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=1050.0),
        fills=fills,
        rejections=rejections,
        clamps=clamps,
        halted=True,
        halt_ts=_ts(2),
        halt_reason="max drawdown breached",
    )


# -- SVG geometry helpers on known values -------------------------------------


def test_axis_bounds_spans_all_series() -> None:
    assert axis_bounds([1000.0, 1050.0], [980.0, 1010.0]) == (980.0, 1050.0)


def test_axis_bounds_empty_is_safe_unit_range() -> None:
    assert axis_bounds([], []) == (0.0, 1.0)


def test_polyline_points_maps_and_flips_y() -> None:
    # Two points across a 100x50 box, y_min=1000, y_max=1050 (span 50):
    # index 0 -> x=0, equity 1000 -> bottom (y=50); index 1 -> x=100, equity 1050 -> top (y=0).
    pts = polyline_points([1000.0, 1050.0], 100.0, 50.0, 1000.0, 1050.0)
    assert pts == [(0.0, 50.0), (100.0, 0.0)]


def test_polyline_points_single_point_at_left_edge() -> None:
    # x pins to the left edge (n == 1); y = 50 - (1234-1000)/500*50 = 26.6.
    assert polyline_points([1234.0], 100.0, 50.0, 1000.0, 1500.0) == [(0.0, 26.6)]


def test_polyline_points_flat_series_on_midline() -> None:
    # y_max == y_min (zero span): every point sits on the vertical midline.
    assert polyline_points([500.0, 500.0], 100.0, 50.0, 500.0, 500.0) == [
        (0.0, 25.0),
        (100.0, 25.0),
    ]


def test_points_attr_formats_pairs() -> None:
    assert points_attr([(0.0, 50.0), (100.0, 0.0)]) == "0.0,50.0 100.0,0.0"


# -- Loading + normalization ---------------------------------------------------


def test_load_payload_normalizes_document_and_chart(tmp_path: Path) -> None:
    result = _result()
    bench = [EquityPoint(_ts(1), 1000.0, 1.0), EquityPoint(_ts(2), 1010.0, 1.0)]
    path = tmp_path / "result.json"
    write_result_json(
        result,
        path,
        mode="backtest",
        frequency="1d",
        metrics=_metrics(),
        benchmark_curve=bench,
    )

    payload = load_payload(path)
    doc = payload["document"]

    # The raw document is carried through untouched.
    assert doc["schema_version"] == RESULT_SCHEMA_VERSION
    assert doc["mode"] == "backtest"
    assert doc["symbols"] == ["AAA", "BBB"]
    assert doc["final_equity"] == 1050.0
    assert len(doc["fills"]) == 2
    assert doc["halt"]["halted"] is True

    chart = payload["chart"]
    assert (chart["width"], chart["height"]) == (CHART_WIDTH, CHART_HEIGHT)
    # y bounds span both equity (1000..1050) and benchmark (1000..1010) series.
    assert (chart["y_min"], chart["y_max"]) == (1000.0, 1050.0)
    assert chart["has_benchmark"] is True
    # Equity: 1000 -> bottom (y=300), 1050 -> top (y=0), across full 800 width.
    assert chart["equity_points"] == "0.0,300.0 800.0,0.0"
    # Benchmark: 1000 -> y=300, 1010 -> y=300-(10/50)*300=240.
    assert chart["benchmark_points"] == "0.0,300.0 800.0,240.0"
    assert chart["start_ts"] == _ts(1).isoformat()
    assert chart["end_ts"] == _ts(2).isoformat()


def test_build_payload_without_benchmark_has_no_overlay() -> None:
    doc = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "symbols": ["AAA"],
        "equity_curve": [
            {"ts": _ts(1).isoformat(), "equity": 100.0, "exposure": 0.0},
            {"ts": _ts(2).isoformat(), "equity": 120.0, "exposure": 0.0},
        ],
        "benchmark_curve": None,
    }
    payload = build_payload(doc)
    assert payload["chart"]["has_benchmark"] is False
    assert payload["chart"]["benchmark_points"] is None


def test_build_payload_rejects_wrong_schema_version() -> None:
    doc = {"schema_version": RESULT_SCHEMA_VERSION + 1, "equity_curve": []}
    with pytest.raises(ValueError, match="schema_version"):
        build_payload(doc)


def test_load_document_rejects_wrong_schema_version(tmp_path: Path) -> None:
    result = _result()
    path = tmp_path / "result.json"
    write_result_json(result, path, mode="backtest", metrics=_metrics())

    # Tamper with the persisted schema_version, then confirm the loader refuses it.
    doc = json.loads(path.read_text())
    doc["schema_version"] = RESULT_SCHEMA_VERSION + 99
    path.write_text(json.dumps(doc))

    with pytest.raises(ValueError, match=r"Unsupported result\.json schema_version"):
        load_document(path)
