"""CLI wiring for the ADR-0068 turnover/cost-budget check — offline, deterministic.

The arithmetic is proved in ``test_cost_budget.py`` and its rendering in
``test_report_cost_budget.py``; what is at stake here is *reachability* and its
price, mirroring ``test_cli_monte_carlo.py``'s two properties for
``--monte-carlo``:

- with ``--cost-budget-pct`` the block must actually reach the terminal and
  ``result.json``, computed **once** and handed to both, and use the run's own
  ``CostConfig`` (including a ``--liquidity-tier-adv`` override, when present);
- without it the run must cost nothing extra and print exactly the bytes it
  printed before the flag existed — including the exact ``result.json`` bytes,
  since the new key is OMITTED rather than emitted as ``null``.

Every run here uses ``--source synthetic`` (no network).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app

runner = CliRunner()

_RANGE = ["--from", "2020-01-01", "--to", "2021-01-01"]


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


class TestCostBudgetIsOffByDefault:
    """The check is never computed unless asked for."""

    def test_no_cost_budget_block_reaches_the_terminal(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Cost budget" not in result.output

    def test_result_json_has_no_cost_budget_key_at_all(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path).exit_code == 0
        payload = _result_json(tmp_path)
        assert "cost_budget" not in payload
        assert payload["schema_version"] == 1

    def test_equity_csv_and_result_json_are_byte_identical_without_the_flag(
        self, tmp_path: Path
    ) -> None:
        default_dir = tmp_path / "default"
        with_dir = tmp_path / "withflag"
        default_dir.mkdir()
        with_dir.mkdir()

        default = _backtest(default_dir)
        with_flag = _backtest(with_dir, "--cost-budget-pct", "0.5")

        assert default.exit_code == 0, default.output
        assert with_flag.exit_code == 0, with_flag.output
        assert (default_dir / "equity_curve.csv").read_bytes() == (
            with_dir / "equity_curve.csv"
        ).read_bytes()

        default_payload = _result_json(default_dir)
        with_payload = _result_json(with_dir)
        assert "cost_budget" not in default_payload
        assert "cost_budget" in with_payload
        with_payload.pop("cost_budget")
        assert default_payload == with_payload


class TestCostBudgetReachesBothOutputs:
    """One computation, two destinations: the summary and ``result.json``."""

    def test_the_block_prints_with_its_provenance(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--cost-budget-pct", "0.5")
        assert result.exit_code == 0, result.output
        assert "Cost budget:   50.00% of equity/year" in result.output

    def test_the_same_figures_land_in_result_json(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path, "--cost-budget-pct", "0.5").exit_code == 0
        payload = _result_json(tmp_path)
        assert "cost_budget" in payload
        block = payload["cost_budget"]
        assert block["cost_budget_pct"] == 0.5
        assert payload["schema_version"] == 1

    def test_a_tiny_budget_makes_the_warning_fire(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--cost-budget-pct", "0.00001")
        assert result.exit_code == 0, result.output
        assert "⚠ predicted cost drag" in result.output
        payload = _result_json(tmp_path)
        assert payload["cost_budget"]["notes"] == []

    def test_a_generous_budget_stays_silent(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--cost-budget-pct", "0.99")
        assert result.exit_code == 0, result.output
        assert "⚠ predicted cost drag" not in result.output

    def test_liquidity_tiering_changes_the_effective_rate_used(self, tmp_path: Path) -> None:
        """The check must read the run's OWN CostConfig, tiering included."""
        flat_dir = tmp_path / "flat"
        tiered_dir = tmp_path / "tiered"
        flat_dir.mkdir()
        tiered_dir.mkdir()

        flat = _backtest(flat_dir, "--cost-budget-pct", "0.5")
        tiered = _backtest(
            tiered_dir,
            "--cost-budget-pct",
            "0.5",
            "--liquidity-tier-adv",
            "1",
            "--liquidity-tier-slippage-bps",
            "1.0",
        )
        assert flat.exit_code == 0, flat.output
        assert tiered.exit_code == 0, tiered.output

        flat_rate = _result_json(flat_dir)["cost_budget"]["effective_rate_bps"]
        tiered_rate = _result_json(tiered_dir)["cost_budget"]["effective_rate_bps"]
        assert flat_rate == pytest.approx(5.0)
        assert tiered_rate == pytest.approx(1.0)


class TestCostBudgetOptionValidation:
    def test_a_non_positive_budget_is_rejected_before_the_run(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--cost-budget-pct", "0")
        assert result.exit_code == 2
        assert "--cost-budget-pct must be positive" in result.output

    def test_a_negative_budget_is_rejected_before_the_run(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--cost-budget-pct", "-0.01")
        assert result.exit_code == 2
        assert "--cost-budget-pct must be positive" in result.output
