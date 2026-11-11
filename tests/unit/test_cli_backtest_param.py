"""`trading backtest --param name=value` — a confirm run of a sweep's winner.

A sweep/walk-forward names a winning combination; before this flag the follow-up
``backtest`` could only run the strategy's shipped defaults, so the confirm run
was not a confirmation of what was tested. The key acceptance property is parity:
a single-combo ``sweep`` and a ``backtest --param`` over the same data must report
the same Sharpe, because they share one parser and one strategy constructor.

All offline (``--source synthetic``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app

runner = CliRunner()

_RANGE = ["--from", "2018-01-01", "--to", "2022-01-01"]


def _backtest(out_dir: Path, *extra: str, strategy: str = "sma_crossover") -> Result:
    return runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            strategy,
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


def _payload(out_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((out_dir / "result.json").read_text())
    return data


def _sharpe(out_dir: Path) -> float:
    return float(_payload(out_dir)["metrics"]["sharpe"])


class TestNoFlagIsUnchanged:
    def test_result_json_has_no_params_key(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path).exit_code == 0
        assert "strategy_params" not in _payload(tmp_path)

    def test_summary_has_no_params_line(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path)
        assert "Params:" not in result.output


class TestOverrideTakesEffect:
    def test_fast_slow_change_the_run(self, tmp_path: Path) -> None:
        base, tuned = tmp_path / "base", tmp_path / "tuned"
        assert _backtest(base).exit_code == 0
        result = _backtest(tuned, "--param", "fast=5", "--param", "slow=30")
        assert result.exit_code == 0, result.output
        assert _sharpe(base) != _sharpe(tuned)

    def test_params_visible_in_summary_and_result_json(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--param", "fast=5", "--param", "slow=30")
        assert result.exit_code == 0, result.output
        assert "Params:        fast=5, slow=30" in result.output
        assert _payload(tmp_path)["strategy_params"] == {"fast": 5, "slow": 30}
        assert _payload(tmp_path)["schema_version"] == 1

    def test_float_value_is_coerced_like_sweep(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--param", "weight=0.5")
        assert result.exit_code == 0, result.output
        assert _payload(tmp_path)["strategy_params"] == {"weight": 0.5}

    def test_free_parameter_count_is_unchanged(self, tmp_path: Path) -> None:
        """Overriding a value does not change how many knobs the strategy has."""
        plain, tuned = tmp_path / "plain", tmp_path / "tuned"
        a = _backtest(plain)
        b = _backtest(tuned, "--param", "fast=5", "--param", "slow=30")
        line_a = next(x for x in a.output.splitlines() if "free parameter" in x)
        line_b = next(x for x in b.output.splitlines() if "free parameter" in x)
        assert line_a.split("/")[-1] == line_b.split("/")[-1]


class TestRejections:
    def test_unknown_name_exits_2_and_lists_valid_names(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--param", "fats=5")
        assert result.exit_code == 2
        assert "fats" in result.output
        for name in ("fast", "slow", "weight"):
            assert name in result.output
        assert not (tmp_path / "result.json").exists()

    def test_bad_value_exits_2(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--param", "fast=banana")
        assert result.exit_code == 2
        assert not (tmp_path / "result.json").exists()

    def test_constructor_rejection_exits_2(self, tmp_path: Path) -> None:
        """fast >= slow is refused by the strategy itself, not skipped as in a sweep."""
        result = _backtest(tmp_path, "--param", "fast=50", "--param", "slow=20")
        assert result.exit_code == 2
        assert "fast" in result.output

    def test_a_grid_of_values_is_refused(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--param", "fast=5,10")
        assert result.exit_code == 2

    def test_malformed_spec_exits_2(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path, "--param", "fast").exit_code == 2

    def test_duplicate_name_exits_2(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path, "--param", "fast=5", "--param", "fast=6").exit_code == 2

    def test_param_on_a_parameterless_strategy_exits_2(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--param", "fast=5", strategy="buy_and_hold")
        assert result.exit_code == 2


class TestParityWithSweep:
    @pytest.mark.parametrize(("fast", "slow"), [(5, 30), (10, 20)])
    def test_single_combo_sweep_and_backtest_agree_on_sharpe(
        self, tmp_path: Path, fast: int, slow: int
    ) -> None:
        sweep_csv = tmp_path / "sweep.csv"
        swept = runner.invoke(
            app,
            [
                "sweep",
                "--strategy",
                "sma_crossover",
                "--param",
                f"fast={fast}",
                "--param",
                f"slow={slow}",
                "--symbols",
                "AAA,BBB",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(sweep_csv),
                *_RANGE,
            ],
        )
        assert swept.exit_code == 0, swept.output
        rows = list(csv.DictReader(sweep_csv.open()))
        assert len(rows) == 1
        sweep_sharpe = float(rows[0]["sharpe"])

        bt = tmp_path / "bt"
        result = _backtest(bt, "--param", f"fast={fast}", "--param", f"slow={slow}")
        assert result.exit_code == 0, result.output
        assert _sharpe(bt) == pytest.approx(sweep_sharpe, abs=1e-6)
