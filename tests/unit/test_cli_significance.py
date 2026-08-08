"""CLI wiring for the ADR-0039 significance block — offline, deterministic.

The statistics themselves are proved in ``test_significance.py`` and their
rendering in ``test_report.py``; what is at stake here is *reachability* and its
price. ADR-0039 shipped ``assess_significance`` / ``summarize(significance=...)``
/ ``write_result_json(significance=...)`` with nothing calling them, and the two
properties that had to survive the wiring are opposites:

- with ``--bootstrap`` the figures must actually reach the terminal and
  ``result.json``, computed **once** and handed to both;
- without it the run must cost nothing extra and print exactly the bytes it
  printed before the flag existed.

Every run here uses ``--source synthetic`` (no network) and a small resample
count, because the default 1,000 is a deliberate expense and a fast test must not
pay it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from trading.cli import app
from trading.metrics import DEFAULT_BOOTSTRAP_SEED

runner = CliRunner()

# Long enough to clear MIN_BOOTSTRAP_OBSERVATIONS (30 return periods) so the
# interval is really computed rather than declined with a note.
_RANGE = ["--from", "2021-01-01", "--to", "2021-12-31"]

# The labels report.py prints for each of the three ADR-0039 figures. Their
# absence is what "byte-identical to before the feature" is checked through.
_SIGNIFICANCE_LABELS = ("Sharpe 95% CI:", "Beats bench:", "Trials:", "Deflated:")


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


def _result_json(out_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((out_dir / "result.json").read_text())
    return payload


class TestBootstrapIsOffByDefault:
    """The expensive figure is never paid for silently (ADR-0039 §5).

    A bootstrap is ~2.7 s on a 21-year daily run — thousands of Sharpe
    computations — so a plain ``trading backtest`` has to be exactly what it was
    before this flag existed, in the terminal *and* on disk.
    """

    def test_no_significance_line_reaches_the_terminal(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path)
        assert result.exit_code == 0, result.output
        for label in _SIGNIFICANCE_LABELS:
            assert label not in result.output

    def test_result_json_carries_the_key_as_null_not_a_computed_block(self, tmp_path: Path) -> None:
        """Present-and-null, never missing: "not measured" is itself information.

        ``result_to_dict`` must not derive the block internally — writing a
        machine-readable artifact cannot be allowed to trigger a bootstrap nobody
        asked for.
        """
        assert _backtest(tmp_path).exit_code == 0
        payload = _result_json(tmp_path)
        assert "significance" in payload
        assert payload["significance"] is None
        # Additive key only; every existing consumer keeps working (ADR-0031/0037).
        assert payload["schema_version"] == 1

    def test_the_explicit_off_switch_matches_the_default_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        default_dir = tmp_path / "default"
        explicit_dir = tmp_path / "explicit"
        default_dir.mkdir()
        explicit_dir.mkdir()

        default = _backtest(default_dir)
        explicit = _backtest(explicit_dir, "--no-bootstrap")

        assert default.exit_code == 0, default.output
        assert explicit.exit_code == 0, explicit.output
        assert default.output.replace(str(default_dir), "") == explicit.output.replace(
            str(explicit_dir), ""
        )
        assert (default_dir / "result.json").read_bytes() == (
            explicit_dir / "result.json"
        ).read_bytes()


class TestBootstrapReachesBothOutputs:
    """One computation, two destinations: the summary and ``result.json``."""

    def test_the_interval_prints_with_its_whole_provenance(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "40")
        assert result.exit_code == 0, result.output
        assert "Sharpe 95% CI:" in result.output
        # A confidence interval whose provenance is invisible is uncheckable.
        assert "40 resamples" in result.output
        assert f"seed {DEFAULT_BOOTSTRAP_SEED}" in result.output

    def test_the_same_figures_land_in_result_json(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "40").exit_code == 0
        block = _result_json(tmp_path)["significance"]
        assert block is not None
        assert block["sharpe_interval"]["resamples"] == 40
        assert block["deflated"]["trials"] == 1
        assert _result_json(tmp_path)["schema_version"] == 1

    def test_the_printed_interval_is_the_one_serialized(self, tmp_path: Path) -> None:
        """Computed once and shared, so the page and the file cannot disagree."""
        result = _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "40")
        interval = _result_json(tmp_path)["significance"]["sharpe_interval"]
        assert f"[{interval['low']:+.2f}, {interval['high']:+.2f}]" in result.output

    def test_the_invisible_trials_caveat_is_not_optional(self, tmp_path: Path) -> None:
        """ADR-0039: the count covers one invocation and is a LOWER BOUND, always."""
        result = _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "40")
        assert "LOWER BOUND" in result.output


class TestPairedFigureFollowsTheBenchmark:
    """The paired win rate exists only when there is something to be paired with."""

    def test_a_benchmark_run_turns_on_the_paired_win_rate(self, tmp_path: Path) -> None:
        result = _backtest(
            tmp_path, "--bootstrap", "--bootstrap-resamples", "40", "--benchmark", "CCC"
        )
        assert result.exit_code == 0, result.output
        assert "Beats bench:" in result.output
        assert "PAIRED resamples" in result.output
        assert _result_json(tmp_path)["significance"]["paired"] is not None

    def test_without_a_benchmark_the_absence_is_explained_not_hidden(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "40")
        assert "Beats bench:" not in result.output
        assert "no benchmark ran, so there is no paired win rate" in result.output
        assert _result_json(tmp_path)["significance"]["paired"] is None


class TestBootstrapKnobs:
    """Determinism is part of the API (ADR-0039), so the seed is an option."""

    def test_the_same_seed_reproduces_the_interval(self, tmp_path: Path) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        args = ("--bootstrap", "--bootstrap-resamples", "40", "--bootstrap-seed", "77")
        assert _backtest(first, *args).exit_code == 0
        assert _backtest(second, *args).exit_code == 0
        assert (
            _result_json(first)["significance"]["sharpe_interval"]
            == _result_json(second)["significance"]["sharpe_interval"]
        )

    def test_a_different_seed_reaches_the_rng(self, tmp_path: Path) -> None:
        """Otherwise the option would be decorative and nobody would notice."""
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        base = ("--bootstrap", "--bootstrap-resamples", "40", "--bootstrap-seed")
        assert _backtest(first, *base, "77").exit_code == 0
        assert _backtest(second, *base, "9001").exit_code == 0
        a = _result_json(first)["significance"]["sharpe_interval"]
        b = _result_json(second)["significance"]["sharpe_interval"]
        assert a["seed"] == 77
        assert b["seed"] == 9001
        assert (a["low"], a["high"]) != (b["low"], b["high"])

    def test_a_nonsense_resample_count_is_a_clean_cli_error(self, tmp_path: Path) -> None:
        """A caller mistake, not a data shortfall — exit 2, no traceback."""
        result = _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "0")
        assert result.exit_code == 2, result.output
        assert "--bootstrap-resamples must be >= 1" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_the_bad_count_is_caught_before_the_run_not_after(self, tmp_path: Path) -> None:
        """Otherwise a typo throws away a finished multi-year backtest.

        The bootstrap runs *after* the engine does, so the only way the operator
        keeps their run is for the option check to happen up front. Nothing
        written is the observable proof that nothing ran.
        """
        result = _backtest(tmp_path, "--bootstrap", "--bootstrap-resamples", "-3")
        assert result.exit_code == 2, result.output
        assert not (tmp_path / "equity_curve.csv").exists()
        assert not (tmp_path / "result.json").exists()

    def test_the_count_is_ignored_when_the_bootstrap_is_off(self, tmp_path: Path) -> None:
        """Meaningless without ``--bootstrap``, exactly like ``--bootstrap-seed``."""
        result = _backtest(tmp_path, "--bootstrap-resamples", "0")
        assert result.exit_code == 0, result.output
        assert _result_json(tmp_path)["significance"] is None


class TestShortRunDeclinesRatherThanFakes:
    """Below the observation floor there is no interval — and it says why."""

    def test_a_run_too_short_to_bootstrap_prints_the_reason(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "backtest",
                "--strategy",
                "buy_and_hold",
                "--symbols",
                "AAA",
                "--from",
                "2021-01-01",
                "--to",
                "2021-01-20",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "equity_curve.csv"),
                "--bootstrap",
                "--bootstrap-resamples",
                "20",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Sharpe 95% CI:" not in result.output
        assert "no Sharpe confidence interval" in result.output
