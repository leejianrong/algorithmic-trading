"""Fast CLI wiring tests — no network.

Most fail before any data fetch; the ones that need bars use ``--source csv``
against a temporary directory, so the whole file stays offline.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading import cli
from trading.cli import app
from trading.interfaces import StrategyContext
from trading.strategies import get_strategy
from trading.types import Bar, Order, TargetWeight

runner = CliRunner()


def _write_csv(directory: Path, symbol: str, days: Iterable[str], price: float = 100.0) -> None:
    """Write a minimal ``<SYMBOL>.csv`` in the schema ``CsvAdapter`` reads."""
    rows = ["ts,open,high,low,close,volume"]
    rows += [f"{day},{price},{price},{price},{price},1000" for day in days]
    (directory / f"{symbol}.csv").write_text("\n".join(rows) + "\n")


_JANUARY_2024 = [f"2024-01-{day:02d}" for day in range(2, 20)]


def _backtest_args(
    data_dir: Path,
    symbols: str,
    *extra: str,
    strategy: str = "buy_and_hold",
) -> list[str]:
    return [
        "backtest",
        "--strategy",
        strategy,
        "--symbols",
        symbols,
        "--from",
        "2024-01-01",
        "--to",
        "2024-01-31",
        "--source",
        "csv",
        "--cache-dir",
        str(data_dir),
        "--out",
        str(data_dir / "results" / "equity_curve.csv"),
        *extra,
    ]


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


class TestBenchmarkFailureIsNotFatal:
    """A benchmark that cannot run must not discard a run that already succeeded.

    ``--benchmark`` is a comparison bolted onto a finished backtest, so its
    failure costs one line of output — not the summary, the CSV, and the
    result.json the operator actually asked for.
    """

    def test_a_benchmark_symbol_the_source_cannot_look_up_only_warns(self, tmp_path: Path) -> None:
        # No GHOST.csv: CsvAdapter raises, load_series records fetch_failed, and the
        # one-symbol benchmark universe comes back empty (ADR-0032).
        _write_csv(tmp_path, "AAA", _JANUARY_2024)
        result = runner.invoke(app, _backtest_args(tmp_path, "AAA", "--benchmark", "GHOST"))

        assert result.exit_code == 0, result.output
        assert "Total return:" in result.output  # the strategy run survived
        assert "warning:" in result.output
        assert "GHOST" in result.output
        assert (tmp_path / "results" / "equity_curve.csv").exists()
        assert (tmp_path / "results" / "result.json").exists()

    def test_a_benchmark_absent_from_the_range_only_warns(self, tmp_path: Path) -> None:
        # A real shape: SPY exists, but not in the window (a pre-listing fold).
        _write_csv(tmp_path, "AAA", _JANUARY_2024)
        _write_csv(tmp_path, "OLD", [f"2023-01-{day:02d}" for day in range(2, 20)])
        result = runner.invoke(app, _backtest_args(tmp_path, "AAA", "--benchmark", "OLD"))

        assert result.exit_code == 0, result.output
        assert "Total return:" in result.output
        assert "OLD" in result.output
        assert "Benchmark (OLD)" not in result.output  # no fabricated comparison

    def test_a_working_benchmark_still_produces_the_comparison(self, tmp_path: Path) -> None:
        _write_csv(tmp_path, "AAA", _JANUARY_2024)
        _write_csv(tmp_path, "BBB", _JANUARY_2024, price=50.0)
        result = runner.invoke(app, _backtest_args(tmp_path, "AAA", "--benchmark", "BBB"))

        assert result.exit_code == 0, result.output
        assert "Benchmark (BBB)" in result.output
        assert "warning:" not in result.output

    def test_a_broken_benchmark_run_is_still_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only *data* absence is tolerated; a misbehaving engine stays loud.

        No data-shaped failure can reach the CLI as anything but
        ``EmptyUniverseError``, so the narrowness of the ``except`` is proved by
        making the benchmark's *strategy* explode instead.
        """

        class Exploding:
            def on_bar(
                self, ts: datetime, bars: dict[str, Bar], context: StrategyContext
            ) -> list[Order | TargetWeight]:
                raise RuntimeError("the engine itself is broken")

        def fake_get_strategy(name: str) -> object:
            return Exploding() if name == "buy_and_hold" else get_strategy(name)

        monkeypatch.setattr(cli, "get_strategy", fake_get_strategy)
        _write_csv(tmp_path, "AAA", _JANUARY_2024)
        _write_csv(tmp_path, "BBB", _JANUARY_2024, price=50.0)
        result = runner.invoke(
            app,
            _backtest_args(tmp_path, "AAA", "--benchmark", "BBB", strategy="equal_weight"),
        )

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)


class TestAbsentSymbolsAreVisible:
    """A universe that shrank under the operator must say so at the terminal."""

    def test_backtest_prints_the_absent_symbol_and_the_traded_universe(
        self, tmp_path: Path
    ) -> None:
        _write_csv(tmp_path, "AAA", _JANUARY_2024)
        result = runner.invoke(app, _backtest_args(tmp_path, "AAA,GHOST"))

        assert result.exit_code == 0, result.output
        assert "GHOST" in result.output
        assert "1 of 2 requested symbol(s) contributed no bars" in result.output
        assert "Traded:        AAA" in result.output

    def test_a_complete_universe_prints_no_absence_block(self, tmp_path: Path) -> None:
        _write_csv(tmp_path, "AAA", _JANUARY_2024)
        _write_csv(tmp_path, "BBB", _JANUARY_2024, price=50.0)
        result = runner.invoke(app, _backtest_args(tmp_path, "AAA,BBB"))

        assert result.exit_code == 0, result.output
        assert "contributed no bars" not in result.output
        assert "Traded:" not in result.output


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
