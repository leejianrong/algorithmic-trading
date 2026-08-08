"""CLI tests for `trading sweep` on the offline synthetic path — no network."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner, Result

from trading.cli import app

runner = CliRunner()

_COMMON = ["--symbols", "AAA,BBB", "--from", "2021-01-01", "--to", "2022-12-31"]


def test_sweep_source_synthetic_runs_and_writes_csv(tmp_path: Path) -> None:
    out = tmp_path / "sweep.csv"
    result = runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--param",
            "fast=5,10",
            "--param",
            "slow=20,40",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    # Header line names the sweep and the ranking metric.
    assert "ranked by sharpe" in result.output
    assert "combos=4" in result.output
    # The ranked table header is printed.
    assert "sharpe" in result.output
    assert "total_return" in result.output
    # CSV written: header + one row per combo (4).
    assert out.exists()
    lines = out.read_text().splitlines()
    assert lines[0].startswith("rank,fast,slow,window")
    assert len(lines) == 1 + 4


def test_sweep_ranked_output_is_ordered_best_first(tmp_path: Path) -> None:
    out = tmp_path / "sweep.csv"
    result = runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--param",
            "fast=5,10,20",
            "--param",
            "slow=30,50",
            "--rank-by",
            "sharpe",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    # CSV rows carry rank 1..N ascending and sharpe descending.
    rows = [line.split(",") for line in out.read_text().splitlines()[1:]]
    header = out.read_text().splitlines()[0].split(",")
    sharpe_idx = header.index("sharpe")
    ranks = [int(r[0]) for r in rows]
    assert ranks == sorted(ranks)
    sharpes = [float(r[sharpe_idx]) for r in rows]
    assert sharpes == sorted(sharpes, reverse=True)


def test_sweep_walk_forward_windows_multiply_runs(tmp_path: Path) -> None:
    out = tmp_path / "sweep.csv"
    result = runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--param",
            "fast=5,10",
            "--param",
            "slow=30",
            "--windows",
            "2",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    # 2 combos x 2 windows = 4 rows.
    assert len(out.read_text().splitlines()) == 1 + 4


def test_sweep_rejects_malformed_param(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--param",
            "fast",  # no '=' → malformed
            "--source",
            "synthetic",
            "--out",
            str(tmp_path / "s.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "--param" in result.output


def test_sweep_rejects_unknown_rank_by(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--param",
            "fast=5",
            "--param",
            "slow=30",
            "--rank-by",
            "bogus",
            "--source",
            "synthetic",
            "--out",
            str(tmp_path / "s.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "rank-by" in result.output.lower()


def test_sweep_reports_skipped_invalid_combos(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "sweep",
            "--strategy",
            "sma_crossover",
            "--param",
            "fast=10,40",  # 40 >= slow(30) → that combo is skipped
            "--param",
            "slow=30",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(tmp_path / "s.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output.lower()


class TestWinnerDeflation:
    """A sweep's headline is the maximum of N draws, and must say so (ADR-0039).

    Run 24 combinations over one data set and the best scores well above zero even
    if not one has an edge, purely because you kept the maximum. This block is not
    behind a flag: the sweep already ran every trial and kept each one's
    ``ReturnMoments``, so the deflation is arithmetic on numbers already in hand —
    no bootstrap, no cost, nothing to opt into.
    """

    def _sweep(self, tmp_path: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                "fast=5,10",
                "--param",
                "slow=20,40",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "sweep.csv"),
                *extra,
                *_COMMON,
            ],
        )

    def test_the_table_is_followed_by_the_trial_count_and_the_null(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path)
        assert result.exit_code == 0, result.output
        # 2 x 2 combinations all ran, so the winner beat exactly four trials.
        assert "combos=4" in result.output
        assert "Trials:        4 scored" in result.output
        assert "the luckiest skill-free one would show Sharpe" in result.output
        assert "Deflated:      P(true Sharpe > that null best)" in result.output

    def test_the_deflation_sits_under_the_table_not_over_it(self, tmp_path: Path) -> None:
        """The ranking is the answer; the deflation is the caveat on it."""
        result = self._sweep(tmp_path)
        assert result.output.index("rank  fast") < result.output.index("Trials:")

    def test_the_invisible_trials_caveat_is_printed_here_too(self, tmp_path: Path) -> None:
        """ADR-0039 calls the sentence "not optional and not conditional"."""
        result = self._sweep(tmp_path)
        assert "LOWER BOUND" in result.output
        assert "counts 4 trial(s)" in result.output

    def test_the_deflation_does_not_touch_the_results_csv(self, tmp_path: Path) -> None:
        """It is a statement *about* the ranking, not another ranked column."""
        out = tmp_path / "sweep.csv"
        assert self._sweep(tmp_path).exit_code == 0
        lines = out.read_text().splitlines()
        assert lines[0].startswith("rank,fast,slow,window")
        assert len(lines) == 1 + 4
        assert "Trials" not in out.read_text()

    def test_a_sweep_that_produced_no_runs_deflates_nothing(self, tmp_path: Path) -> None:
        """No trials means no winner; an absent block, never a fabricated one."""
        result = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
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
                *_COMMON,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No runs produced" in result.output
        assert "Trials:" not in result.output


class TestWalkForwardCli:
    """`--folds` runs TRUE out-of-sample validation (ADR-0026)."""

    def _invoke(self, tmp_path: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                "fast=5,10",
                "--param",
                "slow=20,30",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "wf.csv"),
                *extra,
                *_COMMON,
            ],
        )

    def test_folds_prints_per_fold_is_and_oos(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, "--folds", "2")
        assert result.exit_code == 0, result.output
        assert "Walk-forward:" in result.output
        assert "IS sharpe" in result.output
        assert "OOS sharpe" in result.output
        # The aggregate the whole exercise exists to produce.
        assert "OUT-OF-SAMPLE mean sharpe" in result.output
        assert "profitable out of sample" in result.output

    def test_folds_writes_a_csv_that_labels_is_vs_oos(self, tmp_path: Path) -> None:
        out = tmp_path / "wf.csv"
        result = self._invoke(tmp_path, "--folds", "2")
        assert result.exit_code == 0, result.output
        header = out.read_text().splitlines()[0]
        # A reader must never mistake a tuned number for a validated one.
        assert "is_sharpe" in header
        assert "oos_sharpe" in header

    def test_rolling_mode_accepted(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, "--folds", "2", "--wf-mode", "rolling")
        assert result.exit_code == 0, result.output
        assert "mode=rolling" in result.output

    def test_bad_mode_rejected(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, "--folds", "2", "--wf-mode", "sideways")
        assert result.exit_code == 2, result.output
        assert "--wf-mode must be" in result.output

    def test_folds_and_windows_together_rejected(self, tmp_path: Path) -> None:
        """Two different validation schemes; asking for both is ambiguous."""
        result = self._invoke(tmp_path, "--folds", "2", "--windows", "3")
        assert result.exit_code == 2, result.output
        assert "only one" in result.output

    def test_walk_forward_is_off_by_default(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Walk-forward:" not in result.output
        assert "Sweep:" in result.output
