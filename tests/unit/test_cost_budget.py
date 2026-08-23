"""Fast, no-infra unit tests for the ADR-0068 turnover/cost-budget check (KAN-860).

Fixtures are hand-built curves/fills, exactly like ``test_metrics.py``'s
``TestTurnover``, so every expected value is a transcribed hand computation. The
headline worked example mirrors the card's own real measurement — a run at
~1454% annual turnover and a 25 bps one-way rate predicts ~3.6% of equity lost to
cost, and an implied ceiling of ~400% turnover at a 1% budget — using
a ``periods_per_year`` chosen to cancel the annualization factor to keep the
turnover arithmetic exact rather than approximate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.config import CostConfig
from trading.engine import BacktestResult, EquityPoint
from trading.metrics import (
    CostBudgetReport,
    assess_cost_budget,
    effective_cost_rate_bps,
)
from trading.types import Fill, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _curve(equities: list[float]) -> list[EquityPoint]:
    return [EquityPoint(_ts(i + 1), e) for i, e in enumerate(equities)]


def _result(
    curve: list[EquityPoint], fills: list[tuple[datetime, Fill]], starting_cash: float = 100.0
) -> BacktestResult:
    return BacktestResult(
        symbols=sorted({fill.symbol for _ts, fill in fills}) or ["X"],
        starting_cash=starting_cash,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=starting_cash),
        fills=fills,
    )


class TestEffectiveCostRateBps:
    def test_flat_rate_with_no_symbol_tiers(self) -> None:
        costs = CostConfig(slippage_bps=5.0, taker_fee_bps=2.0)
        fills = [
            (_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("BBB", Side.BUY, 1.0, 1_000.0)),
        ]
        # Every fill prices at slippage_bps + taker_fee_bps regardless of notional,
        # so the notional-weighted blend collapses to that one flat number.
        assert effective_cost_rate_bps(fills, costs) == pytest.approx(7.0)

    def test_blends_tiered_and_default_symbols_by_notional(self) -> None:
        costs = CostConfig(slippage_bps=5.0, taker_fee_bps=0.0, symbol_slippage_bps={"AAA": 1.0})
        fills = [
            (_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0)),  # notional 1,000 @ 1.0 bps
            (_ts(2), Fill("BBB", Side.BUY, 10.0, 100.0)),  # notional 1,000 @ 5.0 bps (default)
        ]
        # Equal notional -> plain average of the two rates.
        assert effective_cost_rate_bps(fills, costs) == pytest.approx(3.0)

    def test_notional_weighting_is_not_a_plain_average(self) -> None:
        costs = CostConfig(slippage_bps=5.0, taker_fee_bps=0.0, symbol_slippage_bps={"AAA": 1.0})
        fills = [
            (_ts(1), Fill("AAA", Side.BUY, 90.0, 100.0)),  # notional 9,000 @ 1.0 bps
            (_ts(2), Fill("BBB", Side.BUY, 10.0, 100.0)),  # notional 1,000 @ 5.0 bps
        ]
        expected = (9_000.0 * 1.0 + 1_000.0 * 5.0) / 10_000.0
        assert effective_cost_rate_bps(fills, costs) == pytest.approx(expected)

    def test_none_when_no_fills(self) -> None:
        assert effective_cost_rate_bps([], CostConfig()) is None

    def test_zero_when_costs_are_free(self) -> None:
        costs = CostConfig(slippage_bps=0.0, taker_fee_bps=0.0)
        fills = [(_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0))]
        assert effective_cost_rate_bps(fills, costs) == 0.0


class TestAssessCostBudget:
    def test_matches_the_card_worked_example(self) -> None:
        # turnover() annualizes by periods_per_year / len(curve); a 2-point curve
        # (one return period) with periods_per_year=2.0 makes that factor exactly
        # 1.0, so turnover = traded / avg_equity: 1,454 notional over 100 avg
        # equity is turnover 14.54 (1454%), the card's own headline number.
        curve = _curve([100.0, 100.0])
        fills = [(_ts(1), Fill("X", Side.BUY, 14.54, 100.0))]
        result = _result(curve, fills)
        costs = CostConfig(slippage_bps=25.0, taker_fee_bps=0.0)

        report = assess_cost_budget(result, costs, cost_budget_pct=0.01, periods_per_year=2.0)

        assert report.turnover == pytest.approx(14.54)
        assert report.effective_rate_bps == pytest.approx(25.0)
        # cost_drag = turnover * one_way_rate = 14.54 * 0.0025 = 0.03635 (~3.6%),
        # matching the card's "predicted 3.6%" at this rate.
        assert report.predicted_drag_pct == pytest.approx(0.03635)
        # implied max turnover at a 1% budget and 25 bps: 0.01 / 0.0025 = 4.0 (400%),
        # matching the card's "Alpaca crypto at 22-25 bps allows ~400% turnover".
        assert report.implied_max_turnover == pytest.approx(4.0)
        assert report.exceeds_budget is True

    def test_stays_silent_under_budget(self) -> None:
        curve = _curve([100.0, 100.0])
        fills = [(_ts(1), Fill("X", Side.BUY, 1.0, 100.0))]  # turnover 1.0 (100%)
        result = _result(curve, fills)
        costs = CostConfig(slippage_bps=5.0, taker_fee_bps=0.0)

        report = assess_cost_budget(result, costs, cost_budget_pct=0.01, periods_per_year=2.0)

        assert report.predicted_drag_pct == pytest.approx(1.0 * 0.0005)
        assert report.exceeds_budget is False
        assert report.notes == []

    def test_no_fills_reports_no_rate_and_never_exceeds(self) -> None:
        curve = _curve([100.0, 100.0])
        result = _result(curve, [])
        report = assess_cost_budget(result, CostConfig(), cost_budget_pct=0.01)

        assert report.effective_rate_bps is None
        assert report.implied_max_turnover is None
        assert report.predicted_drag_pct is None
        assert report.exceeds_budget is False
        assert report.notes != []

    def test_zero_effective_rate_names_no_ceiling_and_never_exceeds(self) -> None:
        curve = _curve([100.0, 100.0])
        fills = [(_ts(1), Fill("X", Side.BUY, 1_000.0, 100.0))]
        result = _result(curve, fills)
        costs = CostConfig(slippage_bps=0.0, taker_fee_bps=0.0)

        report = assess_cost_budget(result, costs, cost_budget_pct=0.01, periods_per_year=1.0)

        assert report.effective_rate_bps == 0.0
        assert report.implied_max_turnover is None
        assert report.predicted_drag_pct == 0.0
        assert report.exceeds_budget is False
        assert report.notes != []

    def test_rejects_a_non_positive_budget(self) -> None:
        curve = _curve([100.0, 100.0])
        result = _result(curve, [])
        with pytest.raises(ValueError, match="cost_budget_pct must be positive"):
            assess_cost_budget(result, CostConfig(), cost_budget_pct=0.0)
        with pytest.raises(ValueError, match="cost_budget_pct must be positive"):
            assess_cost_budget(result, CostConfig(), cost_budget_pct=-0.01)


class TestCostBudgetReportExceedsBudget:
    """The property in isolation, so its "absent is not a violation" rule is pinned."""

    def test_false_when_predicted_drag_is_unknown(self) -> None:
        report = CostBudgetReport(
            cost_budget_pct=0.01,
            turnover=0.0,
            effective_rate_bps=None,
            implied_max_turnover=None,
            predicted_drag_pct=None,
            notes=[],
        )
        assert report.exceeds_budget is False

    def test_true_when_drag_strictly_exceeds_the_budget(self) -> None:
        report = CostBudgetReport(
            cost_budget_pct=0.01,
            turnover=5.0,
            effective_rate_bps=25.0,
            implied_max_turnover=4.0,
            predicted_drag_pct=0.0125,
            notes=[],
        )
        assert report.exceeds_budget is True

    def test_false_when_drag_exactly_equals_the_budget(self) -> None:
        report = CostBudgetReport(
            cost_budget_pct=0.01,
            turnover=4.0,
            effective_rate_bps=25.0,
            implied_max_turnover=4.0,
            predicted_drag_pct=0.01,
            notes=[],
        )
        assert report.exceeds_budget is False
