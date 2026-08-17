"""Fast, no-infra tests for the ADR-0066 regime-split reporting wiring.

Covers ``report.regime_lines`` / ``summarize(regimes=...)`` / ``result_to_dict``'s
additive ``regimes`` key. Every fixture is a hand-built curve/result; no engine,
no network. The two properties at stake mirror ADR-0039's own wiring tests
(``test_cli_significance.py``): a run that never asks for the regime split must
be byte-identical to before this feature existed, and a run that does must see
the same computed block in both the terminal and ``result.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import compute_regime_report
from trading.report import regime_lines, result_to_dict, summarize, write_result_json
from trading.types import Portfolio


def _ts(day: int) -> datetime:
    return datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=day)


def _curve_from_returns(returns: list[float], start: float = 1000.0) -> list[EquityPoint]:
    equity = start
    points = [EquityPoint(_ts(0), equity)]
    for i, r in enumerate(returns, start=1):
        equity *= 1.0 + r
        points.append(EquityPoint(_ts(i), equity))
    return points


def _result(curve: list[EquityPoint]) -> BacktestResult:
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=curve[0].equity,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=curve[-1].equity),
    )


def _long_result() -> BacktestResult:
    returns = [0.0006 * ((-1) ** i) + 0.0001 * i for i in range(400)]
    return _result(_curve_from_returns(returns))


class TestSummaryIsByteIdenticalWithoutRegimes:
    def test_no_regimes_block_when_omitted(self) -> None:
        summary = summarize(_long_result())
        assert "Regimes" not in summary
        assert "high_vol" not in summary

    def test_default_matches_explicit_none(self) -> None:
        result = _long_result()
        assert summarize(result) == summarize(result, regimes=None)


class TestSummaryRendersTheRegimeBlockWhenSupplied:
    def test_all_four_labels_appear(self) -> None:
        result = _long_result()
        report = compute_regime_report(result)
        summary = summarize(result, regimes=report)
        assert "Regimes (window=" in summary
        for label in ("high_vol", "low_vol", "trending", "mean_reverting"):
            assert label in summary

    def test_thresholds_are_printed(self) -> None:
        result = _long_result()
        report = compute_regime_report(result)
        assert report.vol_threshold is not None
        assert report.trend_threshold is not None
        summary = summarize(result, regimes=report)
        assert f"{report.vol_threshold * 100:.2f}%" in summary
        assert f"{report.trend_threshold:.2f}" in summary

    def test_too_short_to_classify_prints_only_the_note(self) -> None:
        result = _result(_curve_from_returns([0.001] * 5))
        report = compute_regime_report(result)
        lines = regime_lines(report)
        assert len(lines) == 1
        assert lines[0].startswith("  note:")

    def test_underpowered_regime_gets_a_warning_line(self) -> None:
        # A run just past the window: return periods classified are far below
        # MIN_BOOTSTRAP_OBSERVATIONS on at least one side.
        result = _result(_curve_from_returns([0.001] * 25))
        report = compute_regime_report(result)
        summary = summarize(result, regimes=report)
        assert "too thin to read this slice" in summary


class TestResultJsonOmitsTheKeyWhenAbsent:
    """The one key in this schema that is OMITTED rather than null when absent."""

    def test_key_absent_by_default(self) -> None:
        payload = result_to_dict(_long_result(), mode="backtest")
        assert "regimes" not in payload

    def test_schema_version_unchanged(self) -> None:
        payload = result_to_dict(_long_result(), mode="backtest")
        assert payload["schema_version"] == 1

    def test_write_result_json_omits_key_too(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        write_result_json(_long_result(), path, mode="backtest")
        payload = json.loads(path.read_text())
        assert "regimes" not in payload


class TestResultJsonCarriesTheComputedBlock:
    def test_key_present_and_asdict_shaped(self) -> None:
        result = _long_result()
        report = compute_regime_report(result)
        payload = result_to_dict(result, mode="backtest", regimes=report)
        assert "regimes" in payload
        block = payload["regimes"]
        assert block["window"] == report.window
        assert set(block.keys()) == {
            "window",
            "vol_threshold",
            "trend_threshold",
            "high_vol",
            "low_vol",
            "trending",
            "mean_reverting",
            "notes",
        }
        assert block["high_vol"]["label"] == "high_vol"
        assert "sharpe" in block["high_vol"]["metrics"]

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        result = _long_result()
        report = compute_regime_report(result)
        path = tmp_path / "result.json"
        write_result_json(result, path, mode="backtest", regimes=report)
        payload = json.loads(path.read_text())
        assert payload["regimes"]["high_vol"]["bar_count"] > 0
        assert payload["schema_version"] == 1
