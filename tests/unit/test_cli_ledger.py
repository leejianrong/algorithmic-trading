"""CLI wiring for the cross-invocation trial ledger (ADR-0062, KAN-858).

Mirrors ``test_cli_significance.py``'s two-property structure for ``--bootstrap``:

- absent ``--ledger``, nothing changes — no file, no different bytes anywhere;
- present, both ``backtest`` and ``sweep`` append exactly one line per invocation
  and a second invocation reading the ledger widens the ADR-0039 deflation.

Everything here uses ``--source synthetic`` (no network) and small resample /
grid sizes so the fast layer stays fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from trading.cli import app
from trading.ledger import TrialLedger

runner = CliRunner()

_RANGE = ["--from", "2021-01-01", "--to", "2021-12-31"]


def _backtest(out_dir: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "sma_crossover",
            "--symbols",
            "AAA,BBB",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out_dir / "equity_curve.csv"),
            *_RANGE,
            *extra,
        ],
    )


def _sweep(out_dir: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--symbols",
            "AAA,BBB",
            "--param",
            "fast=5,10",
            "--param",
            "slow=20,40",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out_dir / "sweep.csv"),
            *_RANGE,
            *extra,
        ],
    )


def _result_json(out_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((out_dir / "result.json").read_text())
    return payload


class TestLedgerIsOffByDefault:
    """A path not given is a path this tool does not touch (ADR-0062)."""

    def test_no_ledger_file_is_created(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path).exit_code == 0
        assert not (tmp_path / "ledger.jsonl").exists()
        assert list(tmp_path.iterdir()) == [
            p for p in tmp_path.iterdir() if p.name != "ledger.jsonl"
        ]

    def test_backtest_result_json_and_equity_csv_are_byte_identical(self, tmp_path: Path) -> None:
        default_dir = tmp_path / "default"
        explicit_dir = tmp_path / "explicit"
        default_dir.mkdir()
        explicit_dir.mkdir()

        default = _backtest(default_dir)
        explicit = _backtest(explicit_dir, "--hypothesis", "")

        assert default.exit_code == 0, default.output
        assert explicit.exit_code == 0, explicit.output
        assert (default_dir / "result.json").read_bytes() == (
            explicit_dir / "result.json"
        ).read_bytes()
        assert (default_dir / "equity_curve.csv").read_bytes() == (
            explicit_dir / "equity_curve.csv"
        ).read_bytes()

    def test_sweep_csv_is_byte_identical(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert _sweep(a).exit_code == 0
        assert _sweep(b).exit_code == 0
        assert (a / "sweep.csv").read_bytes() == (b / "sweep.csv").read_bytes()


class TestBacktestAppendsOneTrial:
    def test_a_plain_backtest_appends_trial_count_one_even_without_bootstrap(
        self, tmp_path: Path
    ) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _backtest(tmp_path, "--ledger", str(ledger_path))
        assert result.exit_code == 0, result.output
        records = TrialLedger(ledger_path).load()
        assert len(records) == 1
        assert records[0].command == "backtest"
        assert records[0].trial_count == 1
        assert records[0].strategy == "sma_crossover"
        assert records[0].symbols == ("AAA", "BBB")
        assert records[0].market == "us_equity"
        assert records[0].interval == "1d"
        assert records[0].observed_sharpe is not None

    def test_the_hypothesis_is_recorded_verbatim(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _backtest(
            tmp_path,
            "--ledger",
            str(ledger_path),
            "--hypothesis",
            "expect crossover to beat buy-and-hold in a trending regime",
        )
        assert result.exit_code == 0, result.output
        records = TrialLedger(ledger_path).load()
        assert records[0].hypothesis == "expect crossover to beat buy-and-hold in a trending regime"

    def test_a_hypothesis_without_a_ledger_is_harmless(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--hypothesis", "some idea")
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "ledger.jsonl").exists()

    def test_two_backtests_into_the_same_ledger_both_land(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        assert _backtest(first_dir, "--ledger", str(ledger_path)).exit_code == 0
        assert _backtest(second_dir, "--ledger", str(ledger_path)).exit_code == 0
        assert TrialLedger(ledger_path).cumulative_trials() == 2


class TestBacktestBootstrapWidensOnThePriorLedger:
    def test_a_second_run_deflates_against_the_first_runs_trial(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        assert (
            _backtest(
                first_dir,
                "--ledger",
                str(ledger_path),
                "--bootstrap",
                "--bootstrap-resamples",
                "40",
            ).exit_code
            == 0
        )
        second = _backtest(
            second_dir, "--ledger", str(ledger_path), "--bootstrap", "--bootstrap-resamples", "40"
        )
        assert second.exit_code == 0, second.output

        first_block = _result_json(first_dir)["significance"]["deflated"]
        second_block = _result_json(second_dir)["significance"]["deflated"]
        # First run: nothing logged yet, so it deflates against itself alone (1).
        assert first_block["trials"] == 1
        # Second run: widened by the first run's one prior trial.
        assert second_block["trials"] == 2
        assert "1 from this run plus 1 carried over" in second.output

    def test_without_bootstrap_the_ledger_still_grows_but_nothing_is_deflated(
        self, tmp_path: Path
    ) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _backtest(tmp_path, "--ledger", str(ledger_path))
        assert result.exit_code == 0, result.output
        assert TrialLedger(ledger_path).cumulative_trials() == 1
        assert _result_json(tmp_path)["significance"] is None


class TestSweepAppendsTheWholeGrid:
    def test_a_sweep_appends_trial_count_equal_to_the_number_of_runs(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _sweep(tmp_path, "--ledger", str(ledger_path))
        assert result.exit_code == 0, result.output
        records = TrialLedger(ledger_path).load()
        assert len(records) == 1
        assert records[0].command == "sweep"
        assert records[0].trial_count == 4  # 2 fast x 2 slow
        assert records[0].observed_sharpe is not None

    def test_a_sweep_with_no_runs_appends_nothing(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--symbols",
                "AAA,BBB",
                "--param",
                "fast=40",  # 40 >= slow(30): the only combo is rejected
                "--param",
                "slow=30",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "sweep.csv"),
                "--ledger",
                str(ledger_path),
                *_RANGE,
            ],
        )
        assert result.exit_code == 0, result.output
        assert not ledger_path.exists()

    def test_a_second_sweep_deflates_wider_than_a_lone_one(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        first = _sweep(first_dir, "--ledger", str(ledger_path))
        assert first.exit_code == 0, first.output
        second = _sweep(second_dir, "--ledger", str(ledger_path))
        assert second.exit_code == 0, second.output

        assert "Trials:        4 scored" in first.output
        assert "Trials:        8 scored" in second.output
        assert "4 from this run plus 4 carried over" in second.output

    def test_the_hypothesis_reaches_the_sweep_ledger_too(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _sweep(tmp_path, "--ledger", str(ledger_path), "--hypothesis", "grid search idea")
        assert result.exit_code == 0, result.output
        assert TrialLedger(ledger_path).load()[0].hypothesis == "grid search idea"


class TestWalkForwardIsUnaffected:
    def test_ledger_with_folds_warns_and_appends_nothing(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--symbols",
                "AAA,BBB",
                "--param",
                "fast=5,10",
                "--param",
                "slow=30",
                "--folds",
                "2",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "sweep.csv"),
                "--ledger",
                str(ledger_path),
                "--from",
                "2019-01-01",
                "--to",
                "2022-12-31",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "KAN-677" in result.output
        assert not ledger_path.exists()

    def test_folds_without_ledger_is_unaffected(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--symbols",
                "AAA,BBB",
                "--param",
                "fast=5,10",
                "--param",
                "slow=30",
                "--folds",
                "2",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "sweep.csv"),
                "--from",
                "2019-01-01",
                "--to",
                "2022-12-31",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "KAN-677" not in result.output
