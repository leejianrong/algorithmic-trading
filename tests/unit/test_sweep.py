"""Fast tests for the parameter sweep / walk-forward outer loop (offline).

All runs use the deterministic ``SyntheticAdapter`` so the whole sweep is
reproducible with no network, exactly as ADR-0016 requires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

from trading.config import RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.sweep import expand_grid, run_sweep, split_windows

_START = datetime(2021, 1, 1, tzinfo=UTC)
_END = datetime(2022, 12, 31, tzinfo=UTC)
_SYMBOLS = ["AAA", "BBB"]


def _adapter() -> SyntheticAdapter:
    return SyntheticAdapter(seed=5)


# --- pure grid expansion ------------------------------------------------------


def test_expand_grid_is_cartesian_product_in_deterministic_order() -> None:
    combos = expand_grid({"fast": [5, 10], "slow": [30, 50]})
    assert combos == [
        {"fast": 5, "slow": 30},
        {"fast": 5, "slow": 50},
        {"fast": 10, "slow": 30},
        {"fast": 10, "slow": 50},
    ]


def test_expand_grid_empty_grid_yields_one_default_combo() -> None:
    assert expand_grid({}) == [{}]


def test_expand_grid_empty_axis_collapses_to_zero_combos() -> None:
    assert expand_grid({"fast": [], "slow": [30]}) == []


def test_split_windows_are_consecutive_and_cover_the_range() -> None:
    spans = split_windows(_START, _END, 3)
    assert len(spans) == 3
    assert spans[0][0] == _START
    assert spans[-1][1] == _END
    # Back-to-back: each window starts where the previous ended.
    for (_a_start, a_end), (b_start, _b_end) in pairwise(spans):
        assert a_end == b_start


def test_split_windows_one_or_fewer_is_the_whole_range() -> None:
    assert split_windows(_START, _END, 1) == [(_START, _END)]
    assert split_windows(_START, _END, 0) == [(_START, _END)]


# --- run_sweep ----------------------------------------------------------------


def test_run_sweep_runs_one_backtest_per_grid_combination() -> None:
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    assert len(summary.runs) == 4
    assert summary.skipped == []
    # Every run carries its combo and a full metric set.
    for run in summary.runs:
        assert set(run.params) == {"fast", "slow"}
        assert run.window == 0
        assert isinstance(run.metrics.sharpe, float)


def test_run_sweep_ranked_orders_best_first_by_key() -> None:
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10, 20], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    by_sharpe = summary.ranked(by="sharpe")
    assert [r.metrics.sharpe for r in by_sharpe] == sorted(
        (r.metrics.sharpe for r in by_sharpe), reverse=True
    )
    by_return = summary.ranked(by="total_return")
    assert [r.metrics.total_return for r in by_return] == sorted(
        (r.metrics.total_return for r in by_return), reverse=True
    )


def test_run_sweep_is_deterministic_on_synthetic() -> None:
    grid = {"fast": [5, 10], "slow": [30, 50]}
    first = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
    second = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
    assert [(r.params, r.metrics) for r in first.runs] == [
        (r.params, r.metrics) for r in second.runs
    ]


def test_run_sweep_skips_invalid_combos_without_aborting() -> None:
    # fast >= slow is rejected by SmaCrossover; only the valid corner survives.
    summary = run_sweep(
        "sma_crossover",
        {"fast": [10, 40], "slow": [30]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    assert len(summary.runs) == 1
    assert summary.runs[0].params == {"fast": 10, "slow": 30}
    assert len(summary.skipped) == 1
    bad_combo, reason = summary.skipped[0]
    assert bad_combo == {"fast": 40, "slow": 30}
    assert "fast" in reason.lower()


def test_run_sweep_walk_forward_runs_each_combo_per_window() -> None:
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        windows=2,
    )
    # 4 combos x 2 windows = 8 runs, tagged 0 and 1.
    assert len(summary.runs) == 8
    assert {run.window for run in summary.runs} == {0, 1}


def test_run_sweep_empty_grid_runs_strategy_defaults_once() -> None:
    summary = run_sweep("equal_weight", {}, _adapter(), _SYMBOLS, _START, _END)
    assert len(summary.runs) == 1
    assert summary.runs[0].params == {}


def test_run_sweep_unknown_strategy_raises() -> None:
    try:
        run_sweep("no_such_strategy", {}, _adapter(), _SYMBOLS, _START, _END)
    except KeyError as exc:
        assert "no_such_strategy" in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected KeyError for an unknown strategy")


def test_run_sweep_respects_injected_risk_config() -> None:
    # An unlimited config never clamps; a run still completes and is ranked.
    summary = run_sweep(
        "equal_weight",
        {},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        risk=RiskConfig.unlimited(),
    )
    assert len(summary.runs) == 1
