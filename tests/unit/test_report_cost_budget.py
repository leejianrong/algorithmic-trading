"""Fast, no-infra tests for the ADR-0068 turnover/cost-budget reporting wiring.

Covers ``report.cost_budget_lines`` / ``summarize(cost_budget=...)`` /
``result_to_dict``'s additive ``cost_budget`` key. Mirrors
``test_report_monte_carlo.py``'s properties: a run that never asks for the check
must be byte-identical to before this feature existed, the block must reach both
the terminal and ``result.json`` from a single computation, and the
``cost_budget`` key is OMITTED (not ``null``) when absent, matching ``regimes``/
``monte_carlo`` rather than ``significance``'s always-null convention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from trading.config import CostConfig
from trading.engine import BacktestResult, EquityPoint
from trading.metrics import assess_cost_budget
from trading.report import cost_budget_lines, result_to_dict, summarize, write_result_json
from trading.types import Fill, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _curve(equities: list[float]) -> list[EquityPoint]:
    return [EquityPoint(_ts(i + 1), e) for i, e in enumerate(equities)]


def _result(fills: list[tuple[datetime, Fill]] | None = None) -> BacktestResult:
    curve = _curve([100.0, 105.0, 103.0, 108.0])
    # `fills or [...]` would silently replace an explicitly empty list (the
    # no-fills test case) with the default, since `[]` is falsy -- `is None` is
    # the only correct "caller did not say" check here.
    if fills is None:
        fills = [(_ts(2), Fill("AAA", Side.BUY, 1.0, 100.0))]
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=100.0,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=100.0),
        fills=fills,
    )


class TestSummaryIsByteIdenticalWithoutCostBudget:
    def test_no_cost_budget_block_when_omitted(self) -> None:
        summary = summarize(_result())
        assert "Cost budget" not in summary

    def test_default_matches_explicit_none(self) -> None:
        result = _result()
        assert summarize(result) == summarize(result, cost_budget=None)


class TestSummaryRendersTheBlockWhenSupplied:
    def test_the_block_prints_with_its_provenance(self) -> None:
        result = _result()
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.01)
        summary = summarize(result, cost_budget=report)
        assert "Cost budget:   1.00% of equity/year" in summary
        assert "effective rate" in summary

    def test_exceeding_budget_prints_the_warning(self) -> None:
        result = _result()
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.0001)
        summary = summarize(result, cost_budget=report)
        assert "⚠ predicted cost drag" in summary
        assert "exceeds the" in summary

    def test_no_fills_prints_only_the_note(self) -> None:
        result = _result(fills=[])
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.01)
        lines = cost_budget_lines(report)
        assert len(lines) == 2
        assert lines[1].startswith("  note:")
        assert "traded nothing" in lines[1]

    def test_never_aborts_or_vetoes_anything(self) -> None:
        """Reporting only: the summary must not gain a rejection/halt line."""
        result = _result()
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.0001)
        summary = summarize(result, cost_budget=report)
        assert "Rejected:" not in summary
        assert "Halt:" not in summary


class TestResultJsonOmitsTheKeyWhenAbsent:
    """The convention this card must follow, matching ``regimes``/``monte_carlo``."""

    def test_key_absent_by_default(self) -> None:
        payload = result_to_dict(_result(), mode="backtest")
        assert "cost_budget" not in payload

    def test_schema_version_unchanged(self) -> None:
        payload = result_to_dict(_result(), mode="backtest")
        assert payload["schema_version"] == 1

    def test_write_result_json_omits_key_too(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        write_result_json(_result(), path, mode="backtest")
        payload = json.loads(path.read_text())
        assert "cost_budget" not in payload


class TestResultJsonCarriesTheComputedBlock:
    def test_key_present_and_asdict_shaped(self) -> None:
        result = _result()
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.01)
        payload = result_to_dict(result, mode="backtest", cost_budget=report)
        assert "cost_budget" in payload
        block = payload["cost_budget"]
        assert set(block.keys()) == {
            "cost_budget_pct",
            "turnover",
            "effective_rate_bps",
            "implied_max_turnover",
            "predicted_drag_pct",
            "notes",
        }
        assert block["cost_budget_pct"] == 0.01

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        result = _result()
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.01)
        path = tmp_path / "result.json"
        write_result_json(result, path, mode="backtest", cost_budget=report)
        payload = json.loads(path.read_text())
        assert payload["cost_budget"]["effective_rate_bps"] == report.effective_rate_bps
