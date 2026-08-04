"""Fast, no-infra tests for the V4 report: summary text, CSV, and PNG writer.

Every fixture is a hand-built equity curve so the asserted numbers (max
drawdown, return sign, exposure) are transcribed hand computations, not
re-derivations of the code under test. No engine, no network.
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trading.engine import BacktestResult, EquityPoint
from trading.report import summarize, write_equity_csv, write_equity_png
from trading.types import Fill, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _result(
    equities: list[float],
    exposures: list[float] | None = None,
    fills: list[tuple[datetime, Fill]] | None = None,
    symbols: list[str] | None = None,
) -> BacktestResult:
    exp = exposures if exposures is not None else [0.0] * len(equities)
    curve = [
        EquityPoint(_ts(i + 1), e, x) for i, (e, x) in enumerate(zip(equities, exp, strict=True))
    ]
    return BacktestResult(
        symbols=symbols or ["AAA"],
        starting_cash=equities[0],
        equity_curve=curve,
        final_portfolio=Portfolio(cash=equities[-1]),
        fills=fills or [],
    )


class TestSummaryMetricsBlock:
    """SLICES V4 e2e acceptance: rising curve and known-dip curve read correctly."""

    def test_monotonic_up_curve_positive_return_sharpe_zero_drawdown(self) -> None:
        summary = summarize(_result([100.0, 101.0, 102.0, 103.0, 104.0]))
        assert "Total return:  +4.00%" in summary
        assert "Max drawdown:  0.00%" in summary
        # Sharpe of a strictly rising curve is positive; assert the sign, not text.
        sharpe_line = next(ln for ln in summary.splitlines() if ln.startswith("Sharpe:"))
        assert float(sharpe_line.split(":")[1]) > 0

    def test_known_dip_reports_exact_max_drawdown(self) -> None:
        # Peak 120 at bar 2, trough 90 at bar 3 → (120 - 90) / 120 = 25%.
        summary = summarize(_result([100.0, 120.0, 90.0, 110.0, 105.0]))
        assert "Max drawdown:  25.00%" in summary

    def test_exposure_lines_render(self) -> None:
        # avg of [0.5, 1.0, 0.75, 0.25] = 0.625; peak = 1.0.
        summary = summarize(_result([100.0] * 4, exposures=[0.5, 1.0, 0.75, 0.25]))
        assert "Avg exposure:  62.50%" in summary
        assert "Peak exposure: 100.00%" in summary

    def test_win_rate_line(self) -> None:
        fills = [
            (_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("AAA", Side.SELL, 10.0, 120.0)),
        ]
        summary = summarize(_result([100.0, 110.0, 120.0], fills=fills))
        assert "Win rate:      100.00%" in summary

    def test_sortino_calmar_turnover_lines_render(self) -> None:
        # Two flat bars (avg equity 100) with a round-trip of 2,000 traded notional:
        # turnover = 2000 / 100 * (252 / 2) = 2,520 → 252,000.00%.
        fills = [
            (_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0)),
            (_ts(2), Fill("AAA", Side.SELL, 10.0, 100.0)),
        ]
        summary = summarize(_result([100.0, 100.0], fills=fills))
        lines = summary.splitlines()
        assert any(ln.startswith("Sortino:") for ln in lines)
        assert any(ln.startswith("Calmar:") for ln in lines)
        assert "Turnover:      252000.00%" in summary


class TestBenchmarkSummary:
    def test_benchmark_comparison_line_present_only_with_benchmark(self) -> None:
        strat = _result([100.0, 130.0])
        bench = _result([100.0, 110.0], symbols=["SPY"])
        assert "Benchmark" not in summarize(strat)
        line = summarize(strat, bench)
        assert "Benchmark (SPY): +10.00%" in line
        # strategy +30% vs benchmark +10% → +20% delta.
        assert "strategy +20.00% vs benchmark" in line


class TestEquityCsv:
    def test_well_formed_one_row_per_day_with_exposure(self, tmp_path: Path) -> None:
        result = _result([100.0, 101.0, 102.0], exposures=[0.0, 0.5, 0.9])
        path = tmp_path / "equity.csv"
        write_equity_csv(result, path)
        lines = path.read_text().splitlines()
        assert lines[0] == "ts,equity,exposure"
        assert len(lines) == 1 + 3  # header + one row per trading day
        # Second data row: exposure 0.5 recorded to six decimals.
        assert lines[2].startswith(_ts(2).isoformat())
        assert lines[2].endswith(",0.500000")

    def test_benchmark_column_only_when_benchmark_passed(self, tmp_path: Path) -> None:
        result = _result([100.0, 101.0])
        no_bench = tmp_path / "a.csv"
        write_equity_csv(result, no_bench)
        assert no_bench.read_text().splitlines()[0] == "ts,equity,exposure"

        bench = _result([100.0, 108.0], symbols=["SPY"])
        with_bench = tmp_path / "b.csv"
        write_equity_csv(result, with_bench, bench)
        rows = with_bench.read_text().splitlines()
        assert rows[0] == "ts,equity,exposure,benchmark_equity"
        # Aligned by timestamp: row for _ts(2) carries the benchmark's 108.
        assert rows[2].endswith(",108.000000")

    def test_benchmark_blank_when_timestamp_missing(self, tmp_path: Path) -> None:
        # Benchmark curve is shorter, so the strategy's later day has no bench mark.
        result = _result([100.0, 101.0, 102.0])
        bench = _result([100.0], symbols=["SPY"])
        path = tmp_path / "c.csv"
        write_equity_csv(result, path, bench)
        rows = path.read_text().splitlines()
        # Day 1 has a benchmark value; days 2 and 3 are blank in the last column.
        assert rows[1].endswith(",100.000000")
        assert rows[2].endswith(",")
        assert rows[3].endswith(",")


class TestPngWriter:
    def test_clear_error_when_matplotlib_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = builtins.__import__

        def _no_matplotlib(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "matplotlib" or name.startswith("matplotlib."):
                raise ImportError("no matplotlib")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_matplotlib)
        with pytest.raises(RuntimeError, match="matplotlib is required"):
            write_equity_png(_result([100.0, 101.0]), tmp_path / "plot.png")

    def test_writes_png_when_matplotlib_present(self, tmp_path: Path) -> None:
        pytest.importorskip("matplotlib")
        path = tmp_path / "plot.png"
        write_equity_png(_result([100.0, 101.0, 102.0], exposures=[0.0, 0.5, 0.9]), path)
        assert path.exists() and path.stat().st_size > 0


class TestSignificanceLines:
    """Trades-per-parameter reporting and the underpowered warning (ADR-0029)."""

    def _fills(self, entries: int) -> list[tuple[datetime, Fill]]:
        """``entries`` distinct symbols each bought once → ``entries`` entries."""
        return [
            (_ts(1), Fill(symbol=f"S{i}", side=Side.BUY, qty=1.0, price=10.0))
            for i in range(entries)
        ]

    def test_trade_count_always_shown(self) -> None:
        text = summarize(_result([100.0, 101.0], fills=self._fills(3)))
        assert "Trades:        3 entry/entries" in text

    def test_no_ratio_line_without_a_parameter_count(self) -> None:
        text = summarize(_result([100.0, 101.0], fills=self._fills(3)))
        assert "Trades/param" not in text

    def test_ratio_line_appears_with_a_parameter_count(self) -> None:
        text = summarize(_result([100.0, 101.0], fills=self._fills(60)), free_parameters=2)
        assert "Trades/param:  30.0" in text
        assert "60 entries / 2 free parameter(s)" in text

    def test_thin_sample_warns_explicitly(self) -> None:
        text = summarize(_result([100.0, 101.0], fills=self._fills(8)), free_parameters=4)
        assert "Trades/param:  2.0" in text
        assert "too small a sample to distinguish edge from noise" in text

    def test_ample_sample_does_not_warn(self) -> None:
        text = summarize(_result([100.0, 101.0], fills=self._fills(120)), free_parameters=2)
        assert "too small a sample" not in text

    def test_zero_parameter_strategy_gets_no_ratio_line(self) -> None:
        """buy_and_hold cannot be overfit by parameter search — no ratio, no warning."""
        text = summarize(_result([100.0, 101.0], fills=self._fills(3)), free_parameters=0)
        assert "Trades/param" not in text
        assert "too small a sample" not in text

    def test_existing_metric_block_is_unchanged_by_default(self) -> None:
        """Every prior line still renders when the new argument is omitted."""
        text = summarize(_result([100.0, 110.0]))
        for label in ("Total return:", "Sharpe:", "Max drawdown:", "Turnover:", "Bars:"):
            assert label in text
