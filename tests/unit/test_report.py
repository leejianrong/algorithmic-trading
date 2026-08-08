"""Fast, no-infra tests for the V4 report: summary text, CSV, and PNG writer.

Every fixture is a hand-built equity curve so the asserted numbers (max
drawdown, return sign, exposure) are transcribed hand computations, not
re-derivations of the code under test. No engine, no network.
"""

from __future__ import annotations

import builtins
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.engine import (
    REASON_FETCH_FAILED,
    REASON_NO_BARS,
    AbsentSymbol,
    BacktestResult,
    Engine,
    EquityPoint,
    HaltEpisode,
)
from trading.metrics import compare_to_benchmark, compute
from trading.report import (
    RESULT_SCHEMA_VERSION,
    result_to_dict,
    summarize,
    write_equity_csv,
    write_equity_png,
    write_result_json,
)
from trading.risk import Guardrails
from trading.strategies import get_strategy
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


class TestSummaryHaltLines:
    """The halt block: unchanged under the default latch, episodes when it re-armed."""

    @staticmethod
    def _halted(episodes: list[HaltEpisode]) -> BacktestResult:
        result = _result([100.0, 90.0, 95.0])
        result.halted = True
        result.halt_ts = _ts(2)
        result.halt_reason = "drawdown 20.0% ≥ max 20.0%"
        result.halt_episodes = episodes
        return result

    def test_single_latched_halt_prints_only_the_legacy_line(self) -> None:
        # ADR-0031: one open-ended episode says nothing the Halt: line does not, so
        # the default-config summary is byte-identical to before the feature.
        summary = summarize(
            self._halted([HaltEpisode(halt_ts=_ts(2), reason="drawdown 20.0% ≥ max 20.0%")])
        )
        assert "Halt:          fired at 2024-01-02T00:00:00+00:00" in summary
        assert "Halt episodes:" not in summary

    def test_re_armed_halts_report_the_episode_count_and_spans(self) -> None:
        summary = summarize(
            self._halted(
                [
                    HaltEpisode(
                        halt_ts=_ts(2), reason="drawdown 20.0% ≥ max 20.0%", resume_ts=_ts(4)
                    ),
                    HaltEpisode(halt_ts=_ts(6), reason="drawdown 21.0% ≥ max 20.0%"),
                ]
            )
        )
        assert "Halt episodes: 2 (1 re-armed, 1 still in force at the end)" in summary
        assert "#1 2024-01-02T00:00:00+00:00 → 2024-01-04T00:00:00+00:00" in summary
        assert "#2 2024-01-06T00:00:00+00:00 → (in force)" in summary


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


class TestAbsentSymbolLines:
    """A shrunk universe is a caveat on every number below it, so say so (ADR-0032).

    ``BacktestResult.absent`` was populated but never rendered, so a run whose
    universe silently lost members read exactly like a clean one.
    """

    @staticmethod
    def _shrunk() -> BacktestResult:
        result = _result([100.0, 101.0], symbols=["AAA", "GHOST", "BOOM"])
        result.absent = [
            AbsentSymbol(
                symbol="GHOST",
                reason=REASON_NO_BARS,
                detail="no bars in 2024-01-01..2024-01-31 — not listed in this window",
            ),
            AbsentSymbol(
                symbol="BOOM",
                reason=REASON_FETCH_FAILED,
                detail="data lookup failed (ConnectionError: upstream reset)",
            ),
        ]
        return result

    def test_every_absent_symbol_is_named_with_its_machine_readable_reason(self) -> None:
        text = summarize(self._shrunk())
        assert "GHOST" in text
        assert REASON_NO_BARS in text
        assert "BOOM" in text
        assert REASON_FETCH_FAILED in text

    def test_absent_details_are_shown_verbatim(self) -> None:
        text = summarize(self._shrunk())
        assert "not listed in this window" in text
        assert "ConnectionError: upstream reset" in text

    def test_requested_and_traded_universes_are_both_visible(self) -> None:
        text = summarize(self._shrunk())
        assert "Symbols:       AAA, GHOST, BOOM" in text  # what was asked for
        assert "Traded:        AAA" in text  # what the numbers actually cover

    def test_it_warns_that_the_figures_cover_the_reduced_universe(self) -> None:
        text = summarize(self._shrunk())
        assert "2 of 3 requested symbol(s) contributed no bars" in text

    def test_the_caveat_sits_with_the_universe_not_at_the_bottom(self) -> None:
        """It qualifies every figure, so it must precede them, not trail them."""
        lines = summarize(self._shrunk()).splitlines()
        assert lines[0].startswith("Symbols:")
        warning = next(i for i, ln in enumerate(lines) if "contributed no bars" in ln)
        total_return = next(i for i, ln in enumerate(lines) if ln.startswith("Total return:"))
        assert warning < total_return

    def test_a_full_universe_summary_is_byte_identical(self) -> None:
        """The backward-compatibility guard: a run with no gaps gains no lines."""
        text = summarize(_result([100.0, 101.0], symbols=["AAA", "BBB"]))
        assert "Traded:" not in text
        assert "contributed no bars" not in text


class TestResultJsonAbsent:
    """``absent`` in result.json — additive, so the schema version does not move."""

    def test_absent_symbols_are_serialized(self) -> None:
        result = _result([100.0, 101.0], symbols=["AAA", "GHOST"])
        result.absent = [
            AbsentSymbol(symbol="GHOST", reason=REASON_NO_BARS, detail="no bars in range")
        ]
        document = result_to_dict(result, mode="backtest")
        assert document["absent"] == [
            {"symbol": "GHOST", "reason": REASON_NO_BARS, "detail": "no bars in range"}
        ]
        # The requested universe keeps its exact old meaning; absence is a new key.
        assert document["symbols"] == ["AAA", "GHOST"]
        assert document["schema_version"] == RESULT_SCHEMA_VERSION == 1

    def test_absent_is_an_empty_list_when_nothing_was_missing(self) -> None:
        document = result_to_dict(_result([100.0, 101.0]), mode="backtest")
        assert document["absent"] == []


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


# --- Benchmark-relative metrics (ADR-0037) -----------------------------------


def _bench_curve(
    days: list[int], equities: list[float], exposure: float = 1.0
) -> list[EquityPoint]:
    """A benchmark equity curve on explicit day numbers (for misalignment cases)."""
    return [EquityPoint(_ts(d), e, exposure) for d, e in zip(days, equities, strict=True)]


class TestSummaryUnchangedWithoutBenchmark:
    """The regression that matters most: no benchmark → byte-identical summary.

    A literal golden, not a set of substring probes. Every benchmark-relative line
    ADR-0037 adds is gated on a benchmark actually having been run, so a run
    without one must render exactly the text it rendered before the feature
    existed — including the ADR-0029 significance block.
    """

    GOLDEN = (
        "Symbols:       AAA\n"
        "Starting cash: $1,000.00\n"
        "Final equity:  $1,080.00\n"
        "Total return:  +8.00%\n"
        "Annualized:    +12655.47%\n"
        "Sharpe:        8.50\n"
        "Sortino:       18.64\n"
        "Calmar:        4302.86\n"
        "Max drawdown:  2.94%\n"
        "Avg exposure:  42.00%\n"
        "Peak exposure: 60.00%\n"
        "Win rate:      100.00%\n"
        "Turnover:      5147.86%\n"
        "Trades:        1 entry/entries\n"
        "Bars:          5\n"
        "Trades/param:  0.5 (1 entries / 2 free parameter(s))\n"
        "  ⚠ under 30 trades per free parameter — too small a sample to distinguish "
        "edge from noise; widen the universe or the date range before trusting these numbers"
    )

    @staticmethod
    def _run() -> BacktestResult:
        fills = [
            (_ts(2), Fill("AAA", Side.BUY, 5.0, 100.0, 0.5)),
            (_ts(4), Fill("AAA", Side.SELL, 5.0, 110.0, 0.5)),
        ]
        return _result(
            [1000.0, 1020.0, 990.0, 1050.0, 1080.0],
            exposures=[0.0, 0.5, 0.45, 0.6, 0.55],
            fills=fills,
        )

    def test_summary_is_byte_identical_to_the_pre_adr_0037_text(self) -> None:
        assert summarize(self._run(), free_parameters=2) == self.GOLDEN

    def test_no_benchmark_relative_label_leaks_in(self) -> None:
        summary = summarize(self._run())
        for label in ("Beta:", "Alpha", "Correlation:", "Info ratio:", "Ret/exposure:"):
            assert label not in summary


class TestBenchmarkRelativeSummary:
    """The block that only appears when a benchmark ran."""

    @staticmethod
    def _pair() -> tuple[BacktestResult, BacktestResult]:
        strat = _result([100.0, 104.0, 101.0, 107.0, 106.0], exposures=[0.5] * 5)
        bench = _result([50.0, 51.0, 50.0, 52.0, 51.5], exposures=[1.0] * 5, symbols=["SPY"])
        return strat, bench

    def test_all_five_lines_render(self) -> None:
        strat, bench = self._pair()
        summary = summarize(strat, bench)
        for label in ("Beta:", "Alpha (ann.):", "Correlation:", "Info ratio:", "Ret/exposure:"):
            assert label in summary

    def test_the_pre_existing_benchmark_line_is_untouched(self) -> None:
        strat, bench = self._pair()
        assert "Benchmark (SPY): +3.00%" in summarize(strat, bench)

    def test_values_match_the_metrics_module(self) -> None:
        strat, bench = self._pair()
        expected = compare_to_benchmark(strat.equity_curve, bench.equity_curve)
        assert expected.beta is not None and expected.correlation is not None
        summary = summarize(strat, bench)
        assert f"Beta:          {expected.beta:.2f}" in summary
        assert f"Correlation:   {expected.correlation:.2f}" in summary

    def test_exposure_adjusted_line_names_both_sides(self) -> None:
        strat, bench = self._pair()
        line = next(
            ln for ln in summarize(strat, bench).splitlines() if ln.startswith("Ret/exposure:")
        )
        # Average exposures 50% (strategy) and 100% (benchmark) both appear, so the
        # reader can see why the raw returns were never comparable.
        assert "50.00% vs 100.00% invested" in line

    def test_partial_overlap_is_flagged_not_silently_absorbed(self) -> None:
        strat = _result([100.0, 104.0, 101.0, 107.0, 106.0], exposures=[0.5] * 5)
        bench = _result([50.0, 51.0, 52.0], symbols=["SPY"])
        # The benchmark covers only days 3-5 of the strategy's five bars.
        bench.equity_curve = _bench_curve([3, 4, 5], [50.0, 51.0, 52.0])
        summary = summarize(strat, bench)
        assert "Bench overlap: 3 of 5 strategy bars" in summary
        assert "cover only the shared span" in summary

    def test_full_overlap_prints_no_caveat(self) -> None:
        strat, bench = self._pair()
        assert "Bench overlap:" not in summarize(strat, bench)

    def test_one_shared_bar_says_so_instead_of_four_n_a_lines(self) -> None:
        strat = _result([100.0, 104.0, 101.0], exposures=[0.5] * 3)
        bench = _result([50.0, 51.0], symbols=["SPY"])
        bench.equity_curve = _bench_curve([3, 4], [50.0, 51.0])
        summary = summarize(strat, bench)
        assert "Bench overlap: 1 shared bar(s) with the benchmark" in summary
        assert "too few to compute beta, alpha, correlation, or information ratio" in summary
        assert "Beta:" not in summary

    def test_undefined_statistics_render_n_a_never_zero(self) -> None:
        # A flat benchmark has no variance, so beta/alpha/correlation are undefined.
        strat = _result([100.0, 104.0, 101.0, 107.0], exposures=[0.5] * 4)
        bench = _result([50.0, 50.0, 50.0, 50.0], symbols=["SPY"])
        summary = summarize(strat, bench)
        assert "Beta:          n/a" in summary
        assert "Alpha (ann.):  n/a" in summary
        assert "Correlation:   n/a" in summary
        # The active return still exists, so the information ratio is a number.
        assert "Info ratio:    n/a" not in summary

    def test_never_invested_strategy_reports_n_a_per_unit(self) -> None:
        strat = _result([100.0, 104.0, 101.0], exposures=[0.0] * 3)
        bench = _result([50.0, 51.0, 52.0], symbols=["SPY"])
        line = next(
            ln for ln in summarize(strat, bench).splitlines() if ln.startswith("Ret/exposure:")
        )
        assert line.startswith("Ret/exposure:  n/a vs benchmark")


class TestResultJsonBenchmarkBlock:
    """``result.json`` carries the comparison additively — no schema bump."""

    STRATEGY: ClassVar[list[float]] = [100.0, 104.0, 101.0, 107.0]
    BENCHMARK: ClassVar[list[float]] = [50.0, 51.0, 50.0, 52.0]

    @classmethod
    def _strat(cls) -> BacktestResult:
        return _result(cls.STRATEGY, exposures=[0.5] * 4)

    @classmethod
    def _curve(cls, equities: list[float]) -> list[EquityPoint]:
        return [EquityPoint(_ts(i + 1), e, 1.0) for i, e in enumerate(equities)]

    @classmethod
    def _doc(cls, *, with_benchmark: bool, frequency: str = "1d") -> dict[str, Any]:
        strat = cls._strat()
        return result_to_dict(
            strat,
            mode="backtest",
            frequency=frequency,
            metrics=compute(strat),
            benchmark_curve=cls._curve(cls.BENCHMARK) if with_benchmark else None,
        )

    def test_schema_version_does_not_move(self) -> None:
        assert self._doc(with_benchmark=True)["schema_version"] == RESULT_SCHEMA_VERSION == 1

    def test_block_is_null_without_a_benchmark(self) -> None:
        doc = self._doc(with_benchmark=False)
        assert doc["benchmark_metrics"] is None
        # ...and the exposure-adjusted return, which needs no benchmark, is not.
        assert doc["metrics"]["return_per_unit_exposure"] is not None

    def test_block_is_derived_from_the_two_curves(self) -> None:
        block = self._doc(with_benchmark=True)["benchmark_metrics"]
        assert block is not None
        assert block["shared_bars"] == 4
        assert set(block) == {"shared_bars", "beta", "alpha", "correlation", "information_ratio"}

    def test_the_metrics_key_is_still_exactly_asdict_of_the_metrics(self) -> None:
        # The comparison lives beside benchmark_curve at the top level, never
        # inside metrics: a v1 reader's `metrics` contract is untouched.
        strat = self._strat()
        metrics = compute(strat)
        doc = result_to_dict(
            strat,
            mode="backtest",
            metrics=metrics,
            benchmark_curve=self._curve(self.BENCHMARK),
        )
        assert doc["metrics"] == asdict(metrics)
        assert "benchmark" not in doc["metrics"]

    def test_every_v1_key_is_still_present_and_unchanged(self) -> None:
        with_bench = self._doc(with_benchmark=True)
        without = self._doc(with_benchmark=False)
        v1_keys = {
            "schema_version",
            "mode",
            "frequency",
            "symbols",
            "starting_cash",
            "final_equity",
            "total_return",
            "equity_curve",
            "benchmark_curve",
            "metrics",
            "fills",
            "clamps",
            "rejections",
            "absent",
            "halt",
        }
        assert v1_keys <= set(with_bench)
        for key in v1_keys - {"benchmark_curve"}:
            assert with_bench[key] == without[key]

    def test_the_document_still_round_trips_through_json(self) -> None:
        doc = self._doc(with_benchmark=True)
        assert json.loads(json.dumps(doc)) == doc

    def test_a_precomputed_comparison_is_respected(self) -> None:
        strat = self._strat()
        # Built against a FLAT benchmark, so its beta is None by construction.
        precomputed = compare_to_benchmark(strat.equity_curve, self._curve([50.0] * 4))
        doc = result_to_dict(
            strat,
            mode="backtest",
            metrics=compute(strat),
            benchmark_curve=self._curve(self.BENCHMARK),
            benchmark_metrics=precomputed,
        )
        # The caller's block wins over anything this function could re-derive.
        assert doc["benchmark_metrics"]["beta"] is None

    def test_the_interval_label_sets_the_annualization_factor(self) -> None:
        daily = self._doc(with_benchmark=True)["benchmark_metrics"]
        hourly = self._doc(with_benchmark=True, frequency="1h")["benchmark_metrics"]
        # Alpha scales with periods_per_year, so the two must differ...
        assert daily["alpha"] != hourly["alpha"]
        # ...while beta is scale-free and must not.
        assert daily["beta"] == hourly["beta"]

    def test_an_unknown_frequency_label_falls_back_to_the_daily_basis(self) -> None:
        odd = self._doc(with_benchmark=True, frequency="7 fortnights")["benchmark_metrics"]
        assert odd == self._doc(with_benchmark=True)["benchmark_metrics"]

    def test_write_result_json_carries_the_block_to_disk(self, tmp_path: Path) -> None:
        strat = self._strat()
        path = tmp_path / "nested" / "result.json"
        write_result_json(
            strat,
            path,
            mode="backtest",
            metrics=compute(strat),
            benchmark_curve=self._curve(self.BENCHMARK),
        )
        with path.open() as fh:
            loaded = json.load(fh)
        assert loaded["benchmark_metrics"]["shared_bars"] == 4


class TestBenchmarkSilentlyFlat:
    """The `--benchmark` run can end 100% in cash, and report `+0.00%` (ADR-0037).

    Mechanism, not speculation: the benchmark runs `buy_and_hold` under
    ``RiskConfig.unlimited()``, so nothing clamps its entry. `buy_and_hold` targets
    ``INVESTED_WEIGHT = 0.998`` and sizes from bar *t*'s **close**, but
    :class:`~trading.broker.SimulatedBroker` fills at bar *t+1*'s **open** plus 5 bps
    slippage. An overnight gap up of more than ~25 bps on that one entry bar
    overshoots the cash, the broker rejects for insufficient funds, and
    `buy_and_hold` has already set ``_invested`` so it never retries.

    Scope is data-dependent, not universal — 22 of 50 synthetic seeds over 2018 hit
    it, which is why it went unnoticed. Under default guardrails the position cap
    clamps the entry to ~25% of equity and it cannot happen.

    An insufficient-cash rejection is not an exception, so `cli._run_benchmark`'s
    ``except EmptyUniverseError`` cannot catch it: the run reports a confident
    ``Benchmark (SPY): +0.00%``.

    The fix belongs in ``strategies/buy_and_hold.py`` / ``broker.py`` / ``cli.py``,
    outside this slice — so the repro is pinned here as ``xfail(strict=True)``. It
    stays green in the fast gate and converts to a hard failure the moment the
    sizing is fixed, which is the signal to move this test next to the fix.
    """

    SEED = 7
    START = datetime(2018, 1, 1, tzinfo=UTC)
    END = datetime(2018, 12, 31, tzinfo=UTC)

    @classmethod
    def _benchmark_run(cls) -> BacktestResult:
        """Exactly what ``cli._run_benchmark`` does, on an offline adapter."""
        adapter = SyntheticAdapter(seed=cls.SEED)
        broker = SimulatedBroker(Portfolio(cash=1000.0))
        engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
        return engine.run(get_strategy("buy_and_hold"), ["SPY"], cls.START, cls.END)

    def test_the_entry_is_rejected_for_insufficient_cash(self) -> None:
        """Characterization: this is the observed behaviour today."""
        result = self._benchmark_run()
        reasons = [reason for _order, reason in result.rejections]
        assert any("insufficient cash" in reason for reason in reasons), reasons
        assert result.fills == []

    @pytest.mark.xfail(
        strict=True,
        reason="ADR-0037: entry sized on the close overshoots the next-open fill; "
        "buy_and_hold never retries, so the benchmark stays in cash. Fix lives in "
        "buy_and_hold/broker/cli — remove this marker when it lands.",
    )
    def test_the_benchmark_actually_invests(self) -> None:
        """What the benchmark is *supposed* to do: buy once and hold."""
        result = self._benchmark_run()
        assert result.fills != []
        assert max(point.exposure for point in result.equity_curve) > 0.9

    def test_the_new_metrics_make_the_flat_benchmark_loud(self) -> None:
        """The honesty guarantee this slice does own.

        A flat benchmark has zero variance, so beta / alpha / correlation are
        undefined and print ``n/a``. Before ADR-0037 the only signal was a bare
        ``+0.00%``, which reads as a real market that happened to go nowhere.
        """
        bench = self._benchmark_run()
        strat = _result([100.0, 104.0, 101.0, 107.0], exposures=[0.5] * 4)
        # Align the benchmark onto the strategy's timestamps so the comparison has
        # a shared span; only the flatness matters here.
        bench.equity_curve = [
            EquityPoint(_ts(i + 1), point.equity, point.exposure)
            for i, point in enumerate(bench.equity_curve[:4])
        ]
        summary = summarize(strat, bench)
        assert "Benchmark (SPY): +0.00%" in summary
        assert "Beta:          n/a" in summary
        assert "Correlation:   n/a" in summary
