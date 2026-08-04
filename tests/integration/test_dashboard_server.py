"""Integration: the optional FastAPI dashboard server (ADR-0023).

Marked ``integration`` and it ``importorskip``s FastAPI + httpx, so it is CI-only
and SKIPS cleanly wherever the optional dashboard extra is not installed — it
never gates the fast pre-push gate or a frozen-lock environment. It exercises the
thin server shell end to end: ``GET /`` serves the self-contained HTML page and
``GET /api/result`` returns the schema-validated run document as JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from trading.dashboard.server import create_app
from trading.engine import BacktestResult, EquityPoint
from trading.metrics import PerformanceMetrics
from trading.report import RESULT_SCHEMA_VERSION, write_result_json
from trading.types import Fill, Order, Portfolio, Side

pytestmark = pytest.mark.integration


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _write_result(path: Path) -> None:
    result = BacktestResult(
        symbols=["AAA", "BBB"],
        starting_cash=1000.0,
        equity_curve=[EquityPoint(_ts(1), 1000.0, 0.0), EquityPoint(_ts(2), 1050.0, 0.5)],
        final_portfolio=Portfolio(cash=1050.0),
        fills=[(_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0, 1.0))],
        rejections=[(Order("CCC", Side.BUY, 3.0), "halted: new entries blocked")],
        clamps=[(Order("BBB", Side.BUY, 20.0), Order("BBB", Side.BUY, 12.0), "gross cap")],
        halted=True,
        halt_ts=_ts(2),
        halt_reason="max drawdown breached",
    )
    metrics = PerformanceMetrics(
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
    write_result_json(result, path, mode="backtest", metrics=metrics)


def test_index_serves_self_contained_html(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    _write_result(path)
    client = TestClient(create_app(path))

    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Trading run dashboard" in body
    assert "<svg" in body
    assert "http://" not in body and "https://" not in body


def test_api_result_returns_the_document(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    _write_result(path)
    client = TestClient(create_app(path))

    response = client.get("/api/result")
    assert response.status_code == 200
    doc = response.json()
    assert doc["schema_version"] == RESULT_SCHEMA_VERSION
    assert doc["symbols"] == ["AAA", "BBB"]
    assert doc["halt"]["halted"] is True
    assert len(doc["fills"]) == 1
