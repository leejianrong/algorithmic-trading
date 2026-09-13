"""Integration: the optional FastAPI live-session dashboard server (ADR-0075).

Marked ``integration`` and it ``importorskip``s FastAPI + httpx, exactly like
``tests/integration/test_dashboard_server.py`` for the finished-run server — CI
only, and skips cleanly wherever the optional ``dashboard`` extra is not
installed. Exercises ``create_live_app`` end to end: ``GET /`` serves the
self-polling live HTML page and ``GET /api/live`` returns the normalized live
payload as JSON, for a hand-built session directory (no real ``PaperSession``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from trading.dashboard.server import create_live_app
from trading.divergence import CSV_COLUMNS

pytestmark = pytest.mark.integration

_STATE = {
    "ts": "2026-09-14T13:35:00+00:00",
    "equity": 100234.56,
    "exposure": 0.254,
    "cash": 74829.10,
    "halted": False,
    "positions": {"AAPL": {"qty": 12.5, "avg_price": 231.40}},
}

_LOG_LINES = [
    "2026-09-14 13:30  decision: (hold)  |  equity: $100,000.00  exposure: 0.0%",
    (
        "2026-09-14 13:35  decision: target AAPL 25%  |  "
        "fills: BUY 12.5000 AAPL @ 231.4000  |  equity: $100,234.56  exposure: 25.4%"
    ),
]


def _write_session(out: Path, *, with_divergence: bool = True) -> None:
    (out / "paper_state.json").write_text(json.dumps(_STATE, indent=2) + "\n")
    (out / "paper_session.log").write_text("\n".join(_LOG_LINES) + "\n")
    if with_divergence:
        with (out / "fill_divergence.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            row = dict.fromkeys(CSV_COLUMNS, "")
            row.update(
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "live_outcome": "filled",
                    "model_outcome": "filled",
                    "realized_slippage_bps": "4.5",
                    "modelled_slippage_bps": "5.0",
                    "outcome_diverged": "false",
                }
            )
            writer.writerow(row)


def test_live_index_serves_self_polling_html(tmp_path: Path) -> None:
    _write_session(tmp_path)
    client = TestClient(create_live_app(tmp_path))

    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Live paper session" in body
    assert "<svg" in body
    assert "/api/live" in body


def test_api_live_returns_the_normalized_payload(tmp_path: Path) -> None:
    _write_session(tmp_path)
    client = TestClient(create_live_app(tmp_path))

    response = client.get("/api/live")
    assert response.status_code == 200
    doc = response.json()
    assert doc["state"]["equity"] == 100234.56
    assert doc["state"]["halted"] is False
    assert doc["divergence"]["row_count"] == 1
    assert doc["chart"]["end_ts"] == "2026-09-14 13:35"


def test_api_live_without_divergence_csv_omits_it_cleanly(tmp_path: Path) -> None:
    _write_session(tmp_path, with_divergence=False)
    client = TestClient(create_live_app(tmp_path))

    response = client.get("/api/live")
    assert response.status_code == 200
    assert response.json()["divergence"] is None


def test_live_app_works_before_the_session_has_written_anything(tmp_path: Path) -> None:
    # No files at all yet — the directory exists (created by --out) but the
    # session has not produced its first completed bar.
    client = TestClient(create_live_app(tmp_path))

    index_response = client.get("/")
    assert index_response.status_code == 200
    assert "Live paper session" in index_response.text

    api_response = client.get("/api/live")
    assert api_response.status_code == 200
    doc = api_response.json()
    assert doc["state"] is None
    assert doc["log_tail"] == []
    assert doc["divergence"] is None
