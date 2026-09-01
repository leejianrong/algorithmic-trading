"""CLI wiring for the walk-forward's own trial accounting (ADR-0074, KAN-677).

Before this, ``sweep --folds --ledger`` printed "not yet wired" and appended
nothing (see the pre-change assertion this replaced, still visible in the git
history of ``test_cli_ledger.py``), and ``--folds`` had no ``--bootstrap`` at all.
The statistics themselves are proved in ``test_sweep.py``
(``WalkForwardSummary.deflated_in_sample`` / the OOS bootstrap interval); what is
at stake here is CLI *reachability* — the flags actually reach the walk-forward
path, an invocation without them costs nothing and changes no artifact, and the
ledger record carries the honest ``(folds x grid)`` count.

Every run here uses ``--source synthetic`` (no network) and a small
``--bootstrap-resamples`` so the fast layer stays fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from trading.cli import app
from trading.ledger import TrialLedger

runner = CliRunner()

_RANGE = ["--from", "2019-01-01", "--to", "2022-12-31"]


def _folds(out_dir: Path, *extra: str) -> Result:
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
            "slow=30",
            "--folds",
            "2",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out_dir / "wf.csv"),
            *_RANGE,
            *extra,
        ],
    )


def _result_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text())
    return payload


class TestFoldsAreByteIdenticalWithoutTheNewFlags:
    """Neither --bootstrap nor --ledger, absent, may move a persisted artifact."""

    def test_walk_forward_csv_is_byte_identical(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert _folds(a).exit_code == 0
        assert _folds(b).exit_code == 0
        assert (a / "wf.csv").read_bytes() == (b / "wf.csv").read_bytes()

    def test_no_ledger_file_is_created_by_default(self, tmp_path: Path) -> None:
        assert _folds(tmp_path).exit_code == 0
        assert not (tmp_path / "ledger.jsonl").exists()

    def test_walk_forward_csv_unaffected_by_bootstrap(self, tmp_path: Path) -> None:
        """--bootstrap changes stdout (a new CI block) but never the CSV."""
        plain = tmp_path / "plain"
        boosted = tmp_path / "boosted"
        plain.mkdir()
        boosted.mkdir()
        assert _folds(plain).exit_code == 0
        assert _folds(boosted, "--bootstrap", "--bootstrap-resamples", "30").exit_code == 0
        assert (plain / "wf.csv").read_bytes() == (boosted / "wf.csv").read_bytes()


class TestFoldsLedgerWiring:
    def test_appends_a_record_with_the_folds_times_grid_trial_count(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _folds(tmp_path, "--ledger", str(ledger_path))
        assert result.exit_code == 0, result.output
        assert "not yet wired" not in result.output
        assert "KAN-677" not in result.output

        records = TrialLedger(ledger_path).load()
        assert len(records) == 1
        record = records[0]
        assert record.command == "sweep --folds"
        assert record.strategy == "sma_crossover"
        assert record.symbols == ("AAA", "BBB")
        assert record.market == "us_equity"
        assert record.interval == "1d"
        # 2 folds x 2 combos (fast=5,10 at slow=30) = 4 — folds x grid, not the
        # grid once and not the fold count once.
        assert record.trial_count == 4
        assert record.observed_sharpe is not None

    def test_hypothesis_is_recorded_verbatim(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _folds(
            tmp_path,
            "--ledger",
            str(ledger_path),
            "--hypothesis",
            "expect the crossover to hold up out of sample",
        )
        assert result.exit_code == 0, result.output
        records = TrialLedger(ledger_path).load()
        assert records[0].hypothesis == "expect the crossover to hold up out of sample"

    def test_a_second_folds_run_widens_the_deflation(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        first = _folds(first_dir, "--ledger", str(ledger_path))
        assert first.exit_code == 0, first.output
        second = _folds(second_dir, "--ledger", str(ledger_path))
        assert second.exit_code == 0, second.output

        assert "Trials:        4 scored" in first.output
        assert "Trials:        8 scored" in second.output
        assert "4 from this run plus 4 carried over" in second.output
        assert TrialLedger(ledger_path).cumulative_trials() == 8

    def test_a_run_that_produces_no_folds_appends_nothing(self, tmp_path: Path) -> None:
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
                "fast=40",  # fast >= slow: every combo rejected
                "--param",
                "slow=30",
                "--folds",
                "2",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "wf.csv"),
                "--ledger",
                str(ledger_path),
                *_RANGE,
            ],
        )
        assert result.exit_code == 0, result.output
        assert not ledger_path.exists()


class TestFoldsDeflationBlock:
    """The IS deflation is free arithmetic — printed always, not behind --bootstrap."""

    def test_deflation_block_prints_without_any_new_flag(self, tmp_path: Path) -> None:
        result = _folds(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Trials:" in result.output
        assert "Deflated:" in result.output

    def test_deflation_names_the_folds_times_grid_count(self, tmp_path: Path) -> None:
        result = _folds(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Trials:        4 scored" in result.output


class TestFoldsBootstrap:
    def test_off_by_default_no_ci_line(self, tmp_path: Path) -> None:
        result = _folds(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Sharpe 95% CI" not in result.output

    def test_bootstrap_prints_a_per_fold_confidence_interval(self, tmp_path: Path) -> None:
        result = _folds(tmp_path, "--bootstrap", "--bootstrap-resamples", "30")
        assert result.exit_code == 0, result.output
        assert "Sharpe 95% CI" in result.output

    def test_bootstrap_reaches_result_of_a_bad_resample_count(self, tmp_path: Path) -> None:
        result = _folds(tmp_path, "--bootstrap", "--bootstrap-resamples", "0")
        assert result.exit_code == 2, result.output
        assert "--bootstrap-resamples must be >= 1" in result.output

    def test_bootstrap_without_folds_warns_and_computes_nothing(self, tmp_path: Path) -> None:
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
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "sweep.csv"),
                "--bootstrap",
                *_RANGE,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "only applies to --folds" in result.output
        assert "Sharpe 95% CI" not in result.output
