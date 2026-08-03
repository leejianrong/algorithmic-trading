"""Fast CLI wiring tests — no network (they fail before any data fetch)."""

from __future__ import annotations

from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()


def test_help_lists_the_backtest_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "backtest" in result.output.lower()


def test_unknown_strategy_exits_before_fetching() -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "nope",
            "--symbols",
            "AAA",
            "--from",
            "2024-01-01",
            "--to",
            "2024-02-01",
        ],
    )
    assert result.exit_code == 2
    assert "unknown strategy" in result.output.lower()


def test_bad_date_is_reported() -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--symbols",
            "AAA",
            "--from",
            "nope",
            "--to",
            "2024-02-01",
        ],
    )
    assert result.exit_code == 2
    assert "yyyy-mm-dd" in result.output.lower()
