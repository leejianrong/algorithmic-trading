"""Fast, no-infra tests for the ADR-0071 diversified-baseline reporting wiring.

Covers ``report.diversified_baseline_lines`` / ``summarize(diversified_baseline=...)``
/ ``result_to_dict``'s additive ``diversified_baseline`` key. Mirrors
``test_report_cost_budget.py``'s properties: a run that never asks for the
comparison must be byte-identical to before this feature existed, the block must
reach both the terminal and ``result.json`` from a single computation, and the
``diversified_baseline`` key is OMITTED (not ``null``) when absent, matching
``regimes``/``monte_carlo``/``cost_budget`` rather than ``significance``'s
always-null convention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import PerformanceMetrics, assess_diversified_baseline, compute
from trading.report import diversified_baseline_lines, result_to_dict, summarize, write_result_json
from trading.types import Order, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _curve(equities: list[float], exposures: list[float] | None = None) -> list[EquityPoint]:
    if exposures is None:
        exposures = [1.0] * len(equities)
    return [
        EquityPoint(_ts(i + 1), e, x)
        for i, (e, x) in enumerate(zip(equities, exposures, strict=True))
    ]


def _result(
    equities: list[float],
    symbols: list[str] | None = None,
    exposures: list[float] | None = None,
    rejections: list[tuple[Order, str]] | None = None,
) -> BacktestResult:
    return BacktestResult(
        symbols=symbols or ["AAA", "BBB"],
        starting_cash=100.0,
        equity_curve=_curve(equities, exposures),
        final_portfolio=Portfolio(cash=100.0),
        rejections=rejections or [],
    )


class TestSummaryIsByteIdenticalWithoutDiversifiedBaseline:
    def test_no_diversified_baseline_block_when_omitted(self) -> None:
        summary = summarize(_result([100.0, 105.0, 103.0, 108.0]))
        assert "Diversified baseline" not in summary

    def test_default_matches_explicit_none(self) -> None:
        result = _result([100.0, 105.0, 103.0, 108.0])
        assert summarize(result) == summarize(result, diversified_baseline=None)

    def test_benchmark_alone_does_not_trigger_it(self) -> None:
        result = _result([100.0, 105.0, 103.0, 108.0])
        benchmark = _result([100.0, 102.0, 101.0, 104.0], symbols=["SPY"])
        summary = summarize(result, benchmark)
        assert "Diversified baseline" not in summary


class TestSummaryRendersTheBlockWhenSupplied:
    def test_the_block_prints_label_and_return(self) -> None:
        result = _result([100.0, 110.0, 105.0, 120.0])
        baseline = _result([100.0, 105.0, 103.0, 108.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")

        summary = summarize(result, diversified_baseline=report)

        assert "Diversified baseline (equal_weight/core10): +8.00%" in summary
        assert "vs baseline" in summary

    def test_prints_alongside_the_spy_benchmark_block(self) -> None:
        result = _result([100.0, 110.0, 105.0, 120.0])
        benchmark = _result([100.0, 102.0, 101.0, 104.0], symbols=["SPY"])
        baseline = _result([100.0, 105.0, 103.0, 108.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")

        summary = summarize(result, benchmark, diversified_baseline=report)

        assert "Benchmark (SPY):" in summary
        assert "Diversified baseline (equal_weight/core10):" in summary
        # Benchmark block precedes the diversified-baseline block.
        assert summary.index("Benchmark (SPY):") < summary.index("Diversified baseline")

    def test_never_invested_caveat_reaches_the_summary(self) -> None:
        result = _result([100.0, 100.0, 100.0])
        flat_baseline = _result(
            [100.0, 100.0, 100.0],
            exposures=[0.0, 0.0, 0.0],
            rejections=[
                (Order("AAA", Side.BUY, 1.0), "insufficient cash: need 101.00, have 100.00")
            ],
        )
        report = assess_diversified_baseline(result, flat_baseline, label="equal_weight/core10")

        summary = summarize(result, diversified_baseline=report)

        assert "⚠ the diversified baseline never took a position" in summary
        assert "insufficient cash" in summary

    def test_relative_stats_print_when_enough_shared_bars(self) -> None:
        result = _result([100.0, 110.0, 105.0, 120.0, 130.0])
        baseline = _result([100.0, 105.0, 103.0, 108.0, 112.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")

        summary = summarize(result, diversified_baseline=report)

        assert "Baseline beta:" in summary
        assert "Baseline alpha (ann.):" in summary
        assert "Baseline correlation:" in summary
        assert "Baseline info ratio:" in summary
        assert "Baseline ret/exposure:" in summary

    def test_too_few_shared_bars_says_so_instead_of_printing_stats(self) -> None:
        result = _result([100.0])
        baseline = _result([100.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")

        lines = diversified_baseline_lines(report, metrics=_metrics_stub(), strategy_bars=1)
        joined = "\n".join(lines)
        assert "too few to compute" in joined
        assert "Baseline beta:" not in joined

    def test_never_aborts_or_vetoes_anything(self) -> None:
        result = _result([100.0, 105.0, 103.0, 108.0])
        baseline = _result([100.0, 90.0, 80.0, 70.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")

        summary = summarize(result, diversified_baseline=report)

        assert "Rejected:" not in summary
        assert "Halt:" not in summary


def _metrics_stub() -> PerformanceMetrics:
    """A minimal stand-in with the fields ``diversified_baseline_lines`` reads."""
    return compute(_result([100.0]))


class TestResultJsonOmitsTheKeyWhenAbsent:
    """The convention this card must follow, matching ``regimes``/``monte_carlo``."""

    def test_key_absent_by_default(self) -> None:
        payload = result_to_dict(_result([100.0, 105.0]), mode="backtest")
        assert "diversified_baseline" not in payload

    def test_schema_version_unchanged(self) -> None:
        payload = result_to_dict(_result([100.0, 105.0]), mode="backtest")
        assert payload["schema_version"] == 1

    def test_write_result_json_omits_key_too(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        write_result_json(_result([100.0, 105.0]), path, mode="backtest")
        payload = json.loads(path.read_text())
        assert "diversified_baseline" not in payload


class TestResultJsonCarriesTheComputedBlock:
    def test_key_present_and_asdict_shaped(self) -> None:
        result = _result([100.0, 110.0, 105.0, 120.0])
        baseline = _result([100.0, 105.0, 103.0, 108.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")

        payload = result_to_dict(result, mode="backtest", diversified_baseline=report)

        assert "diversified_baseline" in payload
        block = payload["diversified_baseline"]
        assert set(block.keys()) == {"label", "symbols", "metrics", "comparison", "notes"}
        assert block["label"] == "equal_weight/core10"
        # dataclasses.asdict preserves tuple-typed fields as tuples; json.dumps
        # (used by write_result_json) serializes either shape as a JSON array.
        assert tuple(block["symbols"]) == ("AAA", "BBB")
        assert set(block["comparison"].keys()) == {
            "shared_bars",
            "beta",
            "alpha",
            "correlation",
            "information_ratio",
        }

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        result = _result([100.0, 110.0, 105.0, 120.0])
        baseline = _result([100.0, 105.0, 103.0, 108.0])
        report = assess_diversified_baseline(result, baseline, label="equal_weight/core10")
        path = tmp_path / "result.json"

        write_result_json(result, path, mode="backtest", diversified_baseline=report)

        payload = json.loads(path.read_text())
        assert payload["diversified_baseline"]["metrics"]["total_return"] == (
            report.metrics.total_return
        )
