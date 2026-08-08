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


class TestDataFeedOption:
    """``--data-feed`` is an Alpaca-only notion (ADR-0034)."""

    def test_rejected_for_a_non_alpaca_source(self) -> None:
        # Silently ignoring it would let an operator believe they picked a tape.
        result = runner.invoke(
            app,
            [
                "paper",
                "--strategy",
                "buy_and_hold",
                "--symbols",
                "AAA",
                "--from",
                "2024-01-02",
                "--to",
                "2024-01-10",
                "--source",
                "synthetic",
                "--data-feed",
                "iex",
            ],
        )
        assert result.exit_code == 2
        assert "--data-feed applies only to --source alpaca" in result.output

    def test_declared_as_a_paper_option(self) -> None:
        # Introspect the declared parameter rather than scraping rendered --help:
        # with colour enabled, rich splits an option name across ANSI escapes
        # (reproducible locally with FORCE_COLOR=1 COLUMNS=80), so a substring
        # match on the help text passes on a plain terminal and fails in CI.
        import typer.main

        command = typer.main.get_command(app)
        paper_cmd = command.commands["paper"]  # type: ignore[attr-defined]
        option_names = {name for param in paper_cmd.params for name in param.opts}
        assert "--data-feed" in option_names
