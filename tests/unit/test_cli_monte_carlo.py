"""CLI wiring for the ADR-0067 Monte Carlo path-shuffle block — offline, deterministic.

The shuffle itself is proved in ``test_monte_carlo.py`` and its rendering in
``test_report_monte_carlo.py``; what is at stake here is *reachability* and its
price, mirroring ``test_cli_regimes.py``'s two properties for ``--regimes``:

- with ``--monte-carlo`` the block must actually reach the terminal and
  ``result.json``, computed **once** and handed to both;
- without it the run must cost nothing extra and print exactly the bytes it
  printed before the flag existed — including the exact ``result.json`` bytes,
  since the new key is OMITTED rather than emitted as ``null`` (matching
  ``regimes``, ADR-0066/ADR-0067).

Every run here uses ``--source synthetic`` (no network).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from trading.cli import app

runner = CliRunner()

# Long enough to clear MIN_BOOTSTRAP_OBSERVATIONS (30) with plenty of room.
_RANGE = ["--from", "2018-01-01", "--to", "2022-01-01"]


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


class TestMonteCarloIsOffByDefault:
    """The shuffle is never computed unless asked for."""

    def test_no_monte_carlo_block_reaches_the_terminal(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Monte Carlo" not in result.output

    def test_result_json_has_no_monte_carlo_key_at_all(self, tmp_path: Path) -> None:
        """Omitted entirely — not even ``null`` — matching ``regimes``."""
        assert _backtest(tmp_path).exit_code == 0
        payload = _result_json(tmp_path)
        assert "monte_carlo" not in payload
        assert payload["schema_version"] == 1

    def test_the_explicit_off_switch_matches_the_default_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        default_dir = tmp_path / "default"
        explicit_dir = tmp_path / "explicit"
        default_dir.mkdir()
        explicit_dir.mkdir()

        default = _backtest(default_dir)
        explicit = _backtest(explicit_dir, "--no-monte-carlo")

        assert default.exit_code == 0, default.output
        assert explicit.exit_code == 0, explicit.output
        assert default.output.replace(str(default_dir), "") == explicit.output.replace(
            str(explicit_dir), ""
        )
        assert (default_dir / "result.json").read_bytes() == (
            explicit_dir / "result.json"
        ).read_bytes()

    def test_equity_csv_is_untouched_by_this_card(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path).exit_code == 0
        header = (tmp_path / "equity_curve.csv").read_bytes().splitlines()[0]
        assert header == b"ts,equity,exposure"


class TestMonteCarloReachesBothOutputs:
    """One computation, two destinations: the summary and ``result.json``."""

    def test_the_block_prints_with_its_provenance(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--monte-carlo", "--monte-carlo-resamples", "100")
        assert result.exit_code == 0, result.output
        assert "Monte Carlo shuffle (" in result.output
        assert "Sharpe (order-invariant)" in result.output

    def test_the_same_figures_land_in_result_json(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path, "--monte-carlo", "--monte-carlo-resamples", "100").exit_code == 0
        payload = _result_json(tmp_path)
        assert "monte_carlo" in payload
        block = payload["monte_carlo"]
        assert block["resamples"] == 100
        assert block["actual_max_drawdown"] is not None
        assert payload["schema_version"] == 1

    def test_the_printed_drawdown_matches_the_serialized_one(self, tmp_path: Path) -> None:
        """Computed once and shared, so the page and the file cannot disagree."""
        result = _backtest(tmp_path, "--monte-carlo", "--monte-carlo-resamples", "100")
        block = _result_json(tmp_path)["monte_carlo"]
        assert f"{block['actual_max_drawdown'] * 100:6.2f}%" in result.output

    def test_the_seed_is_reproducible(self, tmp_path: Path) -> None:
        one_dir = tmp_path / "one"
        two_dir = tmp_path / "two"
        one_dir.mkdir()
        two_dir.mkdir()
        args = ("--monte-carlo", "--monte-carlo-resamples", "50", "--monte-carlo-seed", "123")
        assert _backtest(one_dir, *args).exit_code == 0
        assert _backtest(two_dir, *args).exit_code == 0
        one = _result_json(one_dir)["monte_carlo"]
        two = _result_json(two_dir)["monte_carlo"]
        assert one == two


class TestMonteCarloOptionValidation:
    def test_a_bad_resample_count_is_rejected_before_the_run(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--monte-carlo", "--monte-carlo-resamples", "0")
        assert result.exit_code == 2
        assert "--monte-carlo-resamples must be >= 1" in result.output

    def test_a_bad_resample_count_is_ignored_when_the_flag_is_off(self, tmp_path: Path) -> None:
        """Mirrors --bootstrap: the count is meaningless without the flag."""
        result = _backtest(tmp_path, "--monte-carlo-resamples", "0")
        assert result.exit_code == 0, result.output
