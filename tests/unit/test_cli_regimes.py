"""CLI wiring for the ADR-0066 regime-split block — offline, deterministic.

The classifier itself is proved in ``test_regime_metrics.py`` and its rendering
in ``test_report_regimes.py``; what is at stake here is *reachability* and its
price, mirroring ``test_cli_significance.py``'s two properties for ``--bootstrap``:

- with ``--regimes`` the block must actually reach the terminal and
  ``result.json``, computed **once** and handed to both;
- without it the run must cost nothing extra and print exactly the bytes it
  printed before the flag existed — including the exact ``result.json`` bytes,
  since the new key is OMITTED rather than emitted as ``null`` (ADR-0066).

Every run here uses ``--source synthetic`` (no network).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner, Result

from trading.cli import app

runner = CliRunner()

# Long enough to clear REGIME_WINDOW (20 bars) with room to classify both
# buckets on each axis.
_RANGE = ["--from", "2018-01-01", "--to", "2022-01-01"]

_REGIME_LABELS = ("high_vol", "low_vol", "trending", "mean_reverting")


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


class TestRegimesAreOffByDefault:
    """The split is never computed unless asked for (ADR-0066 §3)."""

    def test_no_regime_block_reaches_the_terminal(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Regimes" not in result.output
        for label in _REGIME_LABELS:
            assert label not in result.output

    def test_result_json_has_no_regimes_key_at_all(self, tmp_path: Path) -> None:
        """Omitted entirely — not even ``null`` — unlike ``significance`` (ADR-0066)."""
        assert _backtest(tmp_path).exit_code == 0
        payload = _result_json(tmp_path)
        assert "regimes" not in payload
        assert payload["schema_version"] == 1

    def test_the_explicit_off_switch_matches_the_default_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        default_dir = tmp_path / "default"
        explicit_dir = tmp_path / "explicit"
        default_dir.mkdir()
        explicit_dir.mkdir()

        default = _backtest(default_dir)
        explicit = _backtest(explicit_dir, "--no-regimes")

        assert default.exit_code == 0, default.output
        assert explicit.exit_code == 0, explicit.output
        assert default.output.replace(str(default_dir), "") == explicit.output.replace(
            str(explicit_dir), ""
        )
        assert (default_dir / "result.json").read_bytes() == (
            explicit_dir / "result.json"
        ).read_bytes()

    def test_equity_csv_is_untouched_by_this_card(self, tmp_path: Path) -> None:
        """The equity curve CSV has no regime column and no new rows."""
        assert _backtest(tmp_path).exit_code == 0
        header = (tmp_path / "equity_curve.csv").read_bytes().splitlines()[0]
        assert header == b"ts,equity,exposure"


class TestRegimesReachBothOutputs:
    """One computation, two destinations: the summary and ``result.json``."""

    def test_the_block_prints_with_its_provenance(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--regimes")
        assert result.exit_code == 0, result.output
        assert "Regimes (window=" in result.output
        for label in _REGIME_LABELS:
            assert label in result.output

    def test_the_same_figures_land_in_result_json(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path, "--regimes").exit_code == 0
        payload = _result_json(tmp_path)
        assert "regimes" in payload
        block = payload["regimes"]
        assert block["high_vol"]["bar_count"] > 0
        assert payload["schema_version"] == 1

    def test_the_printed_split_points_match_the_serialized_ones(self, tmp_path: Path) -> None:
        """Computed once and shared, so the page and the file cannot disagree."""
        result = _backtest(tmp_path, "--regimes")
        block = _result_json(tmp_path)["regimes"]
        assert f"{block['vol_threshold'] * 100:.2f}%" in result.output
        assert f"{block['trend_threshold']:.2f}" in result.output

    def test_free_parameters_flow_through_to_regime_trade_counts(self, tmp_path: Path) -> None:
        """``sma_crossover`` has free parameters, so trade-count math is exercised."""
        assert _backtest(tmp_path, "--regimes").exit_code == 0
        block = _result_json(tmp_path)["regimes"]
        for label in _REGIME_LABELS:
            assert "trade_count" in block[label]["metrics"]
