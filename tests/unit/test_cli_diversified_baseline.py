"""CLI wiring for the ADR-0071 diversified-baseline comparison (KAN-641) — offline.

The arithmetic is proved in ``test_diversified_baseline.py`` and its rendering in
``test_report_diversified_baseline.py``; what is at stake here is
*reachability* and its price, mirroring ``test_cli_cost_budget.py``'s two
properties:

- with ``--diversified-baseline`` the block must actually reach the terminal
  and ``result.json``, computed once and handed to both, running an
  ``equal_weight`` allocation over ``--baseline-basket`` (default ``@core10``)
  under the run's own cash/costs;
- without it the run must cost nothing extra and print exactly the bytes it
  printed before the flag existed — including the exact ``result.json`` bytes,
  since the new key is OMITTED rather than emitted as ``null``.

Every run here uses ``--source synthetic`` (no network); ``--baseline-basket``
is overridden to a plain symbol list so a synthetic run doesn't have to price
real ETF tickers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


class TestDiversifiedBaselineIsOffByDefault:
    """The comparison is never run unless asked for."""

    def test_no_diversified_baseline_block_reaches_the_terminal(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Diversified baseline" not in result.output

    def test_result_json_has_no_diversified_baseline_key_at_all(self, tmp_path: Path) -> None:
        assert _backtest(tmp_path).exit_code == 0
        payload = _result_json(tmp_path)
        assert "diversified_baseline" not in payload
        assert payload["schema_version"] == 1

    def test_equity_csv_and_result_json_are_byte_identical_without_the_flag(
        self, tmp_path: Path
    ) -> None:
        default_dir = tmp_path / "default"
        with_dir = tmp_path / "withflag"
        default_dir.mkdir()
        with_dir.mkdir()

        default = _backtest(default_dir)
        with_flag = _backtest(with_dir, "--diversified-baseline", "--baseline-basket", "CCC,DDD")

        assert default.exit_code == 0, default.output
        assert with_flag.exit_code == 0, with_flag.output
        assert (default_dir / "equity_curve.csv").read_bytes() == (
            with_dir / "equity_curve.csv"
        ).read_bytes()

        default_payload = _result_json(default_dir)
        with_payload = _result_json(with_dir)
        assert "diversified_baseline" not in default_payload
        assert "diversified_baseline" in with_payload
        with_payload.pop("diversified_baseline")
        assert default_payload == with_payload


class TestDiversifiedBaselineReachesBothOutputs:
    """One computation, two destinations: the summary and ``result.json``."""

    def test_the_block_prints_with_its_label(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--diversified-baseline", "--baseline-basket", "CCC,DDD")
        assert result.exit_code == 0, result.output
        assert "Diversified baseline (equal_weight/CCC, DDD):" in result.output

    def test_the_same_figures_land_in_result_json(self, tmp_path: Path) -> None:
        assert (
            _backtest(tmp_path, "--diversified-baseline", "--baseline-basket", "CCC,DDD").exit_code
            == 0
        )
        payload = _result_json(tmp_path)
        assert "diversified_baseline" in payload
        block = payload["diversified_baseline"]
        assert block["label"] == "equal_weight/CCC, DDD"
        assert sorted(block["symbols"]) == ["CCC", "DDD"]
        assert payload["schema_version"] == 1

    def test_default_basket_is_at_symbol(self, tmp_path: Path) -> None:
        """The default ``--baseline-basket`` is ``@core10`` — an ``@name`` expansion."""
        result = _backtest(tmp_path, "--diversified-baseline")
        assert result.exit_code == 0, result.output
        assert "Diversified baseline (equal_weight/core10):" in result.output
        payload = _result_json(tmp_path)
        assert sorted(payload["diversified_baseline"]["symbols"]) == sorted(
            [
                "SPY",
                "QQQ",
                "IWM",
                "EFA",
                "EEM",
                "TLT",
                "IEF",
                "GLD",
                "XLE",
                "XLF",
            ]
        )

    def test_reuses_the_run_s_own_cash_and_costs(self, tmp_path: Path) -> None:
        """A different --cash must change the baseline's own metrics too."""
        cheap_dir = tmp_path / "cheap"
        rich_dir = tmp_path / "rich"
        cheap_dir.mkdir()
        rich_dir.mkdir()

        cheap = _backtest(
            cheap_dir,
            "--diversified-baseline",
            "--baseline-basket",
            "CCC,DDD",
            "--cash",
            "1000",
        )
        rich = _backtest(
            rich_dir,
            "--diversified-baseline",
            "--baseline-basket",
            "CCC,DDD",
            "--cash",
            "1000000",
        )
        assert cheap.exit_code == 0, cheap.output
        assert rich.exit_code == 0, rich.output

        cheap_metrics = _result_json(cheap_dir)["diversified_baseline"]["metrics"]
        rich_metrics = _result_json(rich_dir)["diversified_baseline"]["metrics"]
        # A larger cash pile funds the same fractional-share target far more
        # precisely, so the two runs need not (and generally will not) agree.
        assert cheap_metrics != rich_metrics or cheap_metrics["total_return"] == 0.0


class TestBaselineBasketValidation:
    def test_an_unknown_basket_name_exits_2(self, tmp_path: Path) -> None:
        result = _backtest(tmp_path, "--diversified-baseline", "--baseline-basket", "@nope")
        assert result.exit_code == 2
        assert "unknown basket" in result.output

    def test_baseline_basket_is_ignored_without_the_flag(self, tmp_path: Path) -> None:
        """An unknown basket must never be parsed unless --diversified-baseline is set."""
        result = _backtest(tmp_path, "--baseline-basket", "@nope")
        assert result.exit_code == 0, result.output
