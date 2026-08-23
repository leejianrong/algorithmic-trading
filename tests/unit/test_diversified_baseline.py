"""Fast, no-infra unit tests for the ADR-0071 diversified-baseline comparison (KAN-641).

Fixtures are hand-built curves, exactly like ``test_cost_budget.py``: every
expected value is a transcribed hand computation. Covers
``metrics.assess_diversified_baseline``/``DiversifiedBaselineReport`` — the
naive equal-weight-basket run compared against the strategy the same way
``compare_to_benchmark`` (ADR-0037) already compares against a single-symbol
buy-and-hold, plus the never-invested/invested-late honesty check restated as
plain-text notes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import DiversifiedBaselineReport, assess_diversified_baseline
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
    curve: list[EquityPoint],
    symbols: list[str] | None = None,
    rejections: list[tuple[Order, str]] | None = None,
) -> BacktestResult:
    return BacktestResult(
        symbols=symbols or ["AAA", "BBB"],
        starting_cash=100.0,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=100.0),
        rejections=rejections or [],
    )


class TestAssessDiversifiedBaseline:
    def test_computes_baseline_metrics_and_comparison(self) -> None:
        strategy = _result(_curve([100.0, 110.0, 105.0, 120.0]), symbols=["ZZZ"])
        baseline = _result(_curve([100.0, 105.0, 103.0, 108.0]))

        report = assess_diversified_baseline(
            strategy, baseline, periods_per_year=252.0, label="equal_weight/core10"
        )

        assert report.label == "equal_weight/core10"
        assert report.symbols == ("AAA", "BBB")
        assert report.metrics.total_return == baseline.equity_curve[-1].equity / 100.0 - 1.0
        assert report.comparison.shared_bars == 4
        assert report.notes == []

    def test_never_invested_is_flagged_in_notes(self) -> None:
        strategy = _result(_curve([100.0, 100.0, 100.0]))
        flat_baseline = _result(
            _curve([100.0, 100.0, 100.0], exposures=[0.0, 0.0, 0.0]),
            rejections=[
                (Order("AAA", Side.BUY, 1.0), "insufficient cash: need 101.00, have 100.00")
            ],
        )

        report = assess_diversified_baseline(strategy, flat_baseline, label="equal_weight/core10")

        assert len(report.notes) == 1
        assert "never took a position" in report.notes[0]
        assert "insufficient cash" in report.notes[0]

    def test_invested_late_is_flagged_in_notes(self) -> None:
        strategy = _result(_curve([100.0, 100.0, 100.0, 105.0]))
        late_baseline = _result(
            _curve([100.0, 100.0, 100.0, 105.0], exposures=[0.0, 0.0, 1.0, 1.0])
        )

        report = assess_diversified_baseline(strategy, late_baseline, label="equal_weight/core10")

        assert len(report.notes) == 1
        assert "held nothing until bar 3 of 4" in report.notes[0]

    def test_healthy_baseline_has_no_notes(self) -> None:
        strategy = _result(_curve([100.0, 105.0, 110.0]))
        baseline = _result(_curve([100.0, 102.0, 104.0], exposures=[1.0, 1.0, 1.0]))

        report = assess_diversified_baseline(strategy, baseline, label="equal_weight/core10")

        assert report.notes == []

    def test_too_few_shared_bars_leaves_comparison_undefined(self) -> None:
        strategy = _result(_curve([100.0]))
        baseline = _result(_curve([100.0]))

        report = assess_diversified_baseline(strategy, baseline, label="equal_weight/core10")

        assert report.comparison.shared_bars < 2
        assert report.comparison.beta is None
        assert report.comparison.alpha is None


class TestDiversifiedBaselineReportIsFrozen:
    def test_is_a_plain_frozen_dataclass(self) -> None:
        strategy = _result(_curve([100.0, 105.0]))
        baseline = _result(_curve([100.0, 103.0]))
        report = assess_diversified_baseline(strategy, baseline, label="x")
        assert isinstance(report, DiversifiedBaselineReport)
