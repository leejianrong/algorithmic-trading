"""Fast, no-infra tests for the canonical machine-readable ``result.json``.

Every fixture is a hand-built :class:`BacktestResult` and
:class:`PerformanceMetrics`, so the asserted keys and values are transcribed by
hand, not re-derived from the code under test. No engine, no network. The whole
document must round-trip through ``json.dumps`` with the stock encoder.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import PerformanceMetrics
from trading.report import (
    RESULT_SCHEMA_VERSION,
    result_to_dict,
    write_result_json,
)
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
        (
            Order("BBB", Side.BUY, 20.0),
            Order("BBB", Side.BUY, 12.0),
            "gross exposure cap",
        )
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


def test_result_to_dict_shape_and_values() -> None:
    result = _result()
    metrics = _metrics()
    bench = [EquityPoint(_ts(1), 1000.0, 1.0), EquityPoint(_ts(2), 1010.0, 1.0)]

    doc = result_to_dict(
        result,
        mode="backtest",
        frequency="1d",
        metrics=metrics,
        benchmark_curve=bench,
    )

    assert doc["schema_version"] == RESULT_SCHEMA_VERSION == 1
    assert doc["mode"] == "backtest"
    assert doc["frequency"] == "1d"
    assert doc["symbols"] == ["AAA", "BBB"]
    assert doc["starting_cash"] == 1000.0
    assert doc["final_equity"] == 1050.0
    assert doc["total_return"] == 1050.0 / 1000.0 - 1.0

    assert doc["equity_curve"] == [
        {"ts": _ts(1).isoformat(), "equity": 1000.0, "exposure": 0.0},
        {"ts": _ts(2).isoformat(), "equity": 1050.0, "exposure": 0.5},
    ]
    assert doc["benchmark_curve"] == [
        {"ts": _ts(1).isoformat(), "equity": 1000.0, "exposure": 1.0},
        {"ts": _ts(2).isoformat(), "equity": 1010.0, "exposure": 1.0},
    ]

    # Metrics serialized generically so new fields flow through automatically.
    assert doc["metrics"] == asdict(metrics)

    assert doc["fills"] == [
        {
            "ts": _ts(1).isoformat(),
            "symbol": "AAA",
            "side": "buy",
            "qty": 10.0,
            "price": 100.0,
            "commission": 1.0,
        },
        {
            "ts": _ts(2).isoformat(),
            "symbol": "AAA",
            "side": "sell",
            "qty": 5.0,
            "price": 110.0,
            "commission": 0.5,
        },
    ]
    assert doc["clamps"] == [
        {
            "symbol": "BBB",
            "original_qty": 20.0,
            "clamped_qty": 12.0,
            "side": "buy",
            "reason": "gross exposure cap",
        }
    ]
    assert doc["rejections"] == [
        {
            "symbol": "CCC",
            "qty": 3.0,
            "side": "buy",
            "reason": "halted: new entries blocked",
        }
    ]
    assert doc["halt"] == {
        "halted": True,
        "halt_ts": _ts(2).isoformat(),
        "halt_reason": "max drawdown breached",
    }


def test_result_to_dict_round_trips_through_json() -> None:
    doc = result_to_dict(
        _result(),
        mode="paper",
        metrics=_metrics(),
        benchmark_curve=[EquityPoint(_ts(1), 1000.0, 0.0)],
    )
    # No custom encoder: the stock json.dumps must accept the whole document.
    reloaded = json.loads(json.dumps(doc))
    assert reloaded == doc


def test_none_metrics_and_benchmark_emit_null() -> None:
    doc = result_to_dict(_result(), mode="backtest", metrics=None, benchmark_curve=None)
    assert doc["metrics"] is None
    assert doc["benchmark_curve"] is None
    # Still fully serializable with the nulls in place.
    assert json.loads(json.dumps(doc)) == doc


def test_frequency_passed_through_verbatim() -> None:
    doc = result_to_dict(_result(), mode="paper", frequency="1h")
    assert doc["frequency"] == "1h"


def test_default_frequency_is_1d() -> None:
    doc = result_to_dict(_result(), mode="backtest")
    assert doc["frequency"] == "1d"


def test_write_result_json_reloads_equal(tmp_path: Path) -> None:
    result = _result()
    metrics = _metrics()
    path = tmp_path / "nested" / "result.json"

    write_result_json(result, path, mode="backtest", metrics=metrics)

    assert path.exists()
    with path.open() as fh:
        loaded = json.load(fh)
    assert loaded == result_to_dict(result, mode="backtest", frequency="1d", metrics=metrics)
