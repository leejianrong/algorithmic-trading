"""CLI tests for `trading sweep` on the offline synthetic path — no network."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

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
