"""CLI tests for `trading sweep` on the offline synthetic path — no network."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app
from trading.data.synthetic import SyntheticAdapter
from trading.frequency import Frequency
from trading.sweep import combo_key, run_cost_sensitivity_sweep, run_sweep, run_walk_forward

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


class TestTheIntervalReachesTheTable:
    """``--interval`` must reach the sweep's own metrics, not just the engine.

    KAN-840. ``sweep.py`` called ``metrics.compute(result)`` with no basis, so every
    trial was annualized on the US-equity *daily* year however the bars were spaced —
    ``--interval 5m`` understated Sharpe, Sortino, Calmar, annualized return and
    turnover by ``sqrt(19656 / 252)`` = 8.83x.
    """

    # One month of 5-minute bars is ~2,000 of them: plenty for four sma_crossover
    # trials to diverge. The same month of *daily* bars is 21, which never clears a
    # slow=30 warmup, so the daily control below needs the two-year range.
    _INTRADAY_SPAN = ("--from", "2021-06-01", "--to", "2021-07-01")

    @staticmethod
    def _sweep(tmp_path: Path, *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                "fast=5,10",
                "--param",
                "slow=30,50",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--symbols",
                "AAA,BBB",
                "--out",
                str(tmp_path / "sweep.csv"),
                *extra,
            ],
        )

    @staticmethod
    def _winner_sharpe(result: Result) -> float:
        """The top-ranked run's Sharpe, read off the printed table."""
        lines = result.output.splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("rank  fast"))
        return float(lines[header + 2].split()[4])

    @staticmethod
    def _observed_sharpe(result: Result) -> float:
        """The Sharpe the deflation block calls the winner's, from ``(observed +X)``."""
        line = next(li for li in result.output.splitlines() if li.startswith("Trials:"))
        return float(line.rsplit("(observed ", 1)[1].rstrip(")"))

    def test_the_table_and_the_deflation_block_agree_on_the_winner(self, tmp_path: Path) -> None:
        """The symptom, on one screen.

        Before the fix a 5m sweep printed the winner at ``0.593`` in the table and
        ``observed +5.24`` in the block directly beneath it — the same run, the same
        moments, two annualization bases, differing by exactly 8.83x. Uniformly wrong
        would have been monotonic and self-consistent; this was incoherent, in
        ADR-0054's sense of an honest drawdown beside a foreign Sharpe.
        """
        result = self._sweep(tmp_path, "--interval", "5m", *self._INTRADAY_SPAN)

        assert result.exit_code == 0, result.output
        assert self._observed_sharpe(result) == pytest.approx(self._winner_sharpe(result), abs=0.01)

    def test_a_daily_sweep_agrees_too(self, tmp_path: Path) -> None:
        """The equity daily path was always coherent, and stays that way."""
        result = self._sweep(tmp_path, *_COMMON[2:])

        assert result.exit_code == 0, result.output
        assert self._observed_sharpe(result) == pytest.approx(self._winner_sharpe(result), abs=0.01)

    def test_the_annualized_column_of_an_intraday_sweep_is_a_year(self, tmp_path: Path) -> None:
        """The absurdity the wrong basis produced, in the CSV rather than the table.

        This is a one-month 5-minute run. Annualizing a month must *grow* its
        magnitude toward a year's. On the daily basis it shrank instead — a +2.52%
        month came out as ``annualized_return`` 0.351%, because ~2,000 five-minute
        bars were being counted as eight years of daily ones.
        """
        assert self._sweep(tmp_path, "--interval", "5m", *self._INTRADAY_SPAN).exit_code == 0

        with (tmp_path / "sweep.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows
        for row in rows:
            total = float(row["total_return"])
            annualized = float(row["annualized_return"])
            assert total != 0.0, row
            assert abs(annualized) > abs(total), row

    def test_the_walk_forward_path_gets_the_basis_too(self, tmp_path: Path) -> None:
        """``--folds`` shares ``_run_combo``, and is the quieter half of the defect.

        A sweep prints a deflation block whose observed Sharpe contradicted its table;
        a walk-forward prints ``IS sharpe -> OOS sharpe`` with nothing to disagree
        with it, so only an exact comparison can catch a wrong basis here. This runs
        the library entry point on the interval's real basis and requires the CLI's
        CSV to match it digit for digit.
        """
        assert (
            self._sweep(tmp_path, "--interval", "5m", "--folds", "2", *self._INTRADAY_SPAN)
        ).exit_code == 0

        freq = Frequency.parse("5m")
        expected = run_walk_forward(
            "sma_crossover",
            {"fast": [5, 10], "slow": [30, 50]},
            SyntheticAdapter(seed=5, frequency=freq),
            ["AAA", "BBB"],
            datetime(2021, 6, 1, tzinfo=UTC),
            datetime(2021, 7, 1, tzinfo=UTC),
            folds=2,
            periods_per_year=freq.periods_per_year,
        )

        with (tmp_path / "sweep.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == expected.fold_count == 2
        for row, fold in zip(rows, expected.folds, strict=True):
            assert float(row["is_sharpe"]) == pytest.approx(fold.in_sample_metrics.sharpe, abs=5e-5)
            assert float(row["oos_sharpe"]) == pytest.approx(
                fold.out_of_sample_metrics.sharpe, abs=5e-5
            )
        # And the basis really is the intraday one, not the daily year it defaulted
        # to: at 5m these Sharpes are 8.83x what 252 would have produced.
        assert abs(expected.folds[0].in_sample_metrics.sharpe) > 5.0


class TestStabilityCli:
    """``--stability`` (ADR-0065, KAN-620): a combo's score vs. its grid-neighbour mean.

    Off by default, additive: the main sweep CSV and stdout table are unaffected
    either way, and the new report is a sibling file next to ``--out``.
    """

    def _sweep(self, tmp_path: Path, out_name: str = "sweep.csv", *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                "fast=5,10,15",
                "--param",
                "slow=30,50,80",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / out_name),
                *extra,
                *_COMMON,
            ],
        )

    def test_off_by_default_writes_no_stability_file(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path)
        assert result.exit_code == 0, result.output
        assert "stability" not in result.output.lower()
        assert not (tmp_path / "sweep_stability.csv").exists()

    def test_stability_writes_a_sibling_csv_next_to_out(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "sweep.csv", "--stability")
        assert result.exit_code == 0, result.output
        stability_path = tmp_path / "sweep_stability.csv"
        assert stability_path.exists()
        assert f"Wrote parameter-stability report to {stability_path}" in result.output

    def test_stability_csv_has_one_row_per_combo_and_the_expected_columns(
        self, tmp_path: Path
    ) -> None:
        assert self._sweep(tmp_path, "sweep.csv", "--stability").exit_code == 0
        with (tmp_path / "sweep_stability.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        # 3 fast x 3 slow = 9 combos, one row each (no window repeats here).
        assert len(rows) == 9
        assert set(rows[0]) == {
            "rank",
            "fast",
            "slow",
            "sharpe",
            "neighbor_mean",
            "neighbor_count",
            "gap",
        }
        # Ranked best-first by score, same convention as the main sweep CSV.
        sharpes = [float(r["sharpe"]) for r in rows]
        assert sharpes == sorted(sharpes, reverse=True)

    def test_stability_csv_matches_run_sweeps_own_stability(self, tmp_path: Path) -> None:
        """The CLI's report is exactly `SweepSummary.stability`, not a re-derivation."""
        assert self._sweep(tmp_path, "sweep.csv", "--stability").exit_code == 0
        freq_periods_per_year = 252.0  # daily equity default, matching _COMMON's --interval 1d
        expected = run_sweep(
            "sma_crossover",
            {"fast": [5, 10, 15], "slow": [30, 50, 80]},
            SyntheticAdapter(seed=5),
            ["AAA", "BBB"],
            datetime(2021, 1, 1, tzinfo=UTC),
            datetime(2022, 12, 31, tzinfo=UTC),
            periods_per_year=freq_periods_per_year,
        ).stability()
        expected_by_key = {combo_key(r.params): r for r in expected}

        with (tmp_path / "sweep_stability.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == len(expected)
        for row in rows:
            key = combo_key({"fast": int(row["fast"]), "slow": int(row["slow"])})
            want = expected_by_key[key]
            assert float(row["sharpe"]) == pytest.approx(want.score, abs=5e-5)
            if want.neighbor_mean is None:
                assert row["neighbor_mean"] == ""
            else:
                assert float(row["neighbor_mean"]) == pytest.approx(want.neighbor_mean, abs=5e-5)

    def test_stability_prints_an_ascii_heatmap_for_a_two_axis_grid(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "sweep.csv", "--stability")
        assert result.exit_code == 0, result.output
        # The heatmap header names both axes and every slow value as a column.
        assert "fast\\slow" in result.output
        assert "30" in result.output.split("fast\\slow", 1)[1].splitlines()[0]

    def test_stability_does_not_touch_the_main_sweep_csv_or_table(self, tmp_path: Path) -> None:
        """Purely additive: the pre-existing artifacts are byte-identical either way."""
        without = self._sweep(tmp_path, "a.csv")
        with_flag = self._sweep(tmp_path, "b.csv", "--stability")
        assert without.exit_code == 0
        assert with_flag.exit_code == 0
        assert (tmp_path / "a.csv").read_text() == (tmp_path / "b.csv").read_text()
        # Same ranked table and deflation block on stdout, modulo the new lines
        # --stability appends after "Wrote sweep results to ...".
        without_head = without.output.split("Wrote sweep results to", 1)[0]
        with_head = with_flag.output.split("Wrote sweep results to", 1)[0]
        assert without_head == with_head

    def test_stability_not_wired_into_folds_prints_a_note_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        result = self._sweep(tmp_path, "wf.csv", "--stability", "--folds", "2")
        assert result.exit_code == 0, result.output
        assert "not yet wired into --folds walk-forward" in result.output
        assert not (tmp_path / "wf_stability.csv").exists()

    def test_a_single_param_axis_grid_prints_no_heatmap(self, tmp_path: Path) -> None:
        """The literal heatmap only makes sense with exactly two `--param` axes."""
        result = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "equal_weight",
                "--param",
                "invested=0.5,0.9",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--stability",
                "--out",
                str(tmp_path / "sweep.csv"),
                *_COMMON,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "\\" not in result.output
        assert (tmp_path / "sweep_stability.csv").exists()


class TestSlippageSweepCli:
    """``--slippage-sweep`` (KAN-618, ADR-0069): cost-sensitivity re-run of the winner.

    Off by default, additive: the main sweep CSV and stdout table are unaffected
    either way, and the new report is a sibling file next to ``--out``.
    """

    def _sweep(self, tmp_path: Path, out_name: str = "sweep.csv", *extra: str) -> Result:
        return runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                "fast=5,10",
                "--param",
                "slow=30,50",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / out_name),
                *extra,
                *_COMMON,
            ],
        )

    def test_off_by_default_writes_no_cost_sensitivity_output(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path)
        assert result.exit_code == 0, result.output
        assert "cost sensitivity" not in result.output.lower()
        assert not (tmp_path / "sweep_cost_sensitivity.csv").exists()

    def test_writes_a_sibling_csv_next_to_out(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "sweep.csv", "--slippage-sweep", "5,10,25,50")
        assert result.exit_code == 0, result.output
        cost_path = tmp_path / "sweep_cost_sensitivity.csv"
        assert cost_path.exists()
        assert f"Wrote cost-sensitivity report to {cost_path}" in result.output

    def test_csv_has_one_row_per_level_ascending_with_expected_columns(
        self, tmp_path: Path
    ) -> None:
        assert self._sweep(tmp_path, "sweep.csv", "--slippage-sweep", "50,5,25,10").exit_code == 0
        with (tmp_path / "sweep_cost_sensitivity.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 4
        assert set(rows[0]) == {
            "slippage_bps",
            "taker_fee_bps",
            "sharpe",
            "total_return",
            "annualized_return",
            "max_drawdown",
            "win_rate",
            "avg_exposure",
            "peak_exposure",
        }
        levels = [float(r["slippage_bps"]) for r in rows]
        assert levels == [5.0, 10.0, 25.0, 50.0]

    def test_re_runs_the_sweeps_own_winner_holding_params_fixed(self, tmp_path: Path) -> None:
        """The CLI's report is exactly `run_cost_sensitivity_sweep` on the winner."""
        result = self._sweep(tmp_path, "sweep.csv", "--slippage-sweep", "5,50")
        assert result.exit_code == 0, result.output

        winner = (
            run_sweep(
                "sma_crossover",
                {"fast": [5, 10], "slow": [30, 50]},
                SyntheticAdapter(seed=5),
                ["AAA", "BBB"],
                datetime(2021, 1, 1, tzinfo=UTC),
                datetime(2022, 12, 31, tzinfo=UTC),
                periods_per_year=252.0,
            )
            .ranked()[0]
            .params
        )
        expected = run_cost_sensitivity_sweep(
            "sma_crossover",
            winner,
            SyntheticAdapter(seed=5),
            ["AAA", "BBB"],
            datetime(2021, 1, 1, tzinfo=UTC),
            datetime(2022, 12, 31, tzinfo=UTC),
            slippage_bps=[5.0, 50.0],
            periods_per_year=252.0,
        )
        winner_pretty = ", ".join(f"{k}={v:g}" for k, v in winner.items())
        assert f"params={{{winner_pretty}}}" in result.output

        with (tmp_path / "sweep_cost_sensitivity.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        expected_by_level = {run.slippage_bps: run for run in expected.runs}
        for row in rows:
            want = expected_by_level[float(row["slippage_bps"])]
            assert float(row["sharpe"]) == pytest.approx(want.metrics.sharpe, abs=5e-5)
            assert float(row["total_return"]) == pytest.approx(want.metrics.total_return, abs=5e-5)

    def test_prints_where_the_edge_dies(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "sweep.csv", "--slippage-sweep", "5,500,5000,50000")
        assert result.exit_code == 0, result.output
        # An absurdly wide grid guarantees a crossing somewhere for a traded strategy.
        assert "Edge" in result.output

    def test_mutually_exclusive_with_slippage_bps(self, tmp_path: Path) -> None:
        result = self._sweep(
            tmp_path, "sweep.csv", "--slippage-sweep", "5,10", "--slippage-bps", "7"
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_malformed_level_exits_2(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "sweep.csv", "--slippage-sweep", "5,abc")
        assert result.exit_code == 2
        assert "--slippage-sweep" in result.output

    def test_negative_level_exits_2(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "sweep.csv", "--slippage-sweep", "-5")
        assert result.exit_code == 2
        assert "non-negative" in result.output

    def test_not_wired_into_folds_prints_a_note_and_writes_nothing(self, tmp_path: Path) -> None:
        result = self._sweep(tmp_path, "wf.csv", "--slippage-sweep", "5,10", "--folds", "2")
        assert result.exit_code == 0, result.output
        assert "not yet wired into --folds walk-forward" in result.output
        assert not (tmp_path / "wf_cost_sensitivity.csv").exists()

    def test_does_not_touch_the_main_sweep_csv_or_table(self, tmp_path: Path) -> None:
        """Purely additive: the pre-existing artifacts are byte-identical either way."""
        without = self._sweep(tmp_path, "a.csv")
        with_flag = self._sweep(tmp_path, "b.csv", "--slippage-sweep", "5,10,25,50")
        assert without.exit_code == 0
        assert with_flag.exit_code == 0
        assert (tmp_path / "a.csv").read_text() == (tmp_path / "b.csv").read_text()
        without_head = without.output.split("Wrote sweep results to", 1)[0]
        with_head = with_flag.output.split("Wrote sweep results to", 1)[0]
        assert without_head == with_head

    def test_no_runs_produced_prints_no_cost_sensitivity_block(self, tmp_path: Path) -> None:
        """A grid with no valid combo has no winner to re-run — no crash, no block."""
        result = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                "fast=40",
                "--param",
                "slow=30",  # fast >= slow: rejected by the constructor
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--slippage-sweep",
                "5,10",
                "--out",
                str(tmp_path / "sweep.csv"),
                *_COMMON,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No runs produced" in result.output
        assert "cost sensitivity" not in result.output.lower()
        assert not (tmp_path / "sweep_cost_sensitivity.csv").exists()
