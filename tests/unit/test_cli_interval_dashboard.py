"""CLI tests for the --interval option, result.json emission, and the dashboard.

All offline and fastapi-free: the static export and payload loader are pure stdlib,
so these run in the fast gate. The FastAPI server path lives in the CI-only
integration layer (tests/integration/test_dashboard_server.py).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner, Result

from trading.cli import app
from trading.dashboard.payload import load_payload

runner = CliRunner()

_DAILY = ["--symbols", "AAA,BBB", "--from", "2021-01-01", "--to", "2021-06-30"]
_INTRADAY = ["--symbols", "SYN1", "--from", "2022-03-01", "--to", "2022-03-10"]


def _run_backtest(out: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *extra,
        ],
    )


# --- --interval parsing / validation -----------------------------------------


def test_interval_defaults_to_daily_and_emits_1d_frequency(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    result = _run_backtest(out, *_DAILY)
    assert result.exit_code == 0, result.output
    doc = json.loads((out.parent / "result.json").read_text())
    assert doc["frequency"] == "1d"


def test_intraday_interval_with_yfinance_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--source",
            "yfinance",
            "--interval",
            "1h",
            "--out",
            str(tmp_path / "e.csv"),
            *_DAILY,
        ],
    )
    assert result.exit_code == 2
    assert "daily-only" in result.output


def test_intraday_interval_with_csv_errors(tmp_path: Path) -> None:
    (tmp_path / "AAA.csv").write_text(
        "ts,open,high,low,close,volume\n2021-01-04,100,101,99,100,1000\n"
    )
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--symbols",
            "AAA",
            "--source",
            "csv",
            "--interval",
            "30m",
            "--cache-dir",
            str(tmp_path),
            "--from",
            "2021-01-01",
            "--to",
            "2021-01-31",
            "--out",
            str(tmp_path / "e.csv"),
        ],
    )
    assert result.exit_code == 2
    assert "daily-only" in result.output


def test_unknown_interval_errors(tmp_path: Path) -> None:
    result = _run_backtest(tmp_path / "e.csv", "--interval", "7q", *_DAILY)
    assert result.exit_code == 2
    assert "unknown frequency" in result.output


def test_backtest_intraday_synthetic_runs_and_records_frequency(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    result = _run_backtest(out, "--interval", "1h", *_INTRADAY)
    assert result.exit_code == 0, result.output
    doc = json.loads((out.parent / "result.json").read_text())
    assert doc["frequency"] == "1h"
    # A 6.5-hour session yields multiple 1h bars per trading day, so an intraday
    # run over ~7 trading days has many more bars than the daily equivalent.
    assert len(doc["equity_curve"]) > 20


# --- result.json is dashboard-consumable -------------------------------------


def test_backtest_result_json_is_loadable_by_dashboard_payload(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    result = _run_backtest(out, "--benchmark", "AAA", *_DAILY)
    assert result.exit_code == 0, result.output
    payload = load_payload(out.parent / "result.json")
    assert payload["document"]["mode"] == "backtest"
    assert payload["chart"]["has_benchmark"] is True


def test_paper_result_json_is_loadable_by_dashboard_payload(tmp_path: Path) -> None:
    out = tmp_path / "paper"
    result = runner.invoke(
        app,
        [
            "paper",
            "--strategy",
            "sma_crossover",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *_DAILY,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = load_payload(out / "result.json")
    assert payload["document"]["mode"] == "paper"
    assert payload["document"]["frequency"] == "1d"


# --- dashboard --static -------------------------------------------------------


def test_dashboard_static_writes_self_contained_html(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    assert _run_backtest(out, *_DAILY).exit_code == 0
    result_json = out.parent / "result.json"

    html_path = tmp_path / "dash.html"
    result = runner.invoke(
        app,
        ["dashboard", "--result", str(result_json), "--static", str(html_path)],
    )
    assert result.exit_code == 0, result.output
    assert html_path.exists()
    text = html_path.read_text()
    assert text.startswith("<!doctype html>")
    # Self-contained: no external references of any kind.
    assert "http://" not in text
    assert "https://" not in text
    assert "src=" not in text


def test_dashboard_requires_exactly_one_of_static_or_serve(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    assert _run_backtest(out, *_DAILY).exit_code == 0
    result_json = out.parent / "result.json"

    # Neither --static nor --serve.
    neither = runner.invoke(app, ["dashboard", "--result", str(result_json)])
    assert neither.exit_code == 2
    assert "exactly one" in neither.output

    # Both --static and --serve.
    both = runner.invoke(
        app,
        [
            "dashboard",
            "--result",
            str(result_json),
            "--static",
            str(tmp_path / "d.html"),
            "--serve",
        ],
    )
    assert both.exit_code == 2
    assert "exactly one" in both.output


def test_dashboard_static_missing_result_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "dashboard",
            "--result",
            str(tmp_path / "nope.json"),
            "--static",
            str(tmp_path / "d.html"),
        ],
    )
    assert result.exit_code == 2
    assert "not found" in result.output
