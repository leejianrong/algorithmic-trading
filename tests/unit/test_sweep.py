"""Fast tests for the parameter sweep / walk-forward outer loop (offline).

All runs use the deterministic ``SyntheticAdapter`` (or a hand-built
``FakeAdapter``) so the whole sweep is reproducible with no network, exactly as
ADR-0016 requires. The walk-forward tests (ADR-0026) additionally pin the
selection *discipline*: the winner comes from in-sample only and is run over
out-of-sample exactly once.

``SyntheticAdapter`` used to reseed per call and generate from the requested start
day, so two spans of equal length replayed the *same* price path — which quietly made
the per-window sweep a null test (two windows of equal length returned identical
metrics) and any degradation measurement on synthetic data meaningless. ADR-0030 fixed
that: a range is now a slice of one canonical series, so different spans really are
different data. The rigged IS-vs-OOS fixture below is still a hand-built
``FakeAdapter``, for the better reason — rigging a *deliberate* in-sample win and
out-of-sample loss needs authored prices, not whatever a GBM draw happens to do.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from trading.config import RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.synthetic import SyntheticAdapter
from trading.metrics import PerformanceMetrics
from trading.sweep import (
    WalkForwardFold,
    WalkForwardSummary,
    expand_grid,
    run_sweep,
    run_walk_forward,
    split_folds,
    split_windows,
)
from trading.types import Bar

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


def test_run_sweep_windows_are_distinct_data_not_one_replayed_path() -> None:
    # The consequence of ADR-0030 at this level: before it, the adapter reseeded per
    # call and every window replayed the same path, so windows of equal bar count
    # returned *identical* metrics and the per-window sweep measured nothing.
    summary = run_sweep(
        "sma_crossover", {"fast": [5], "slow": [30]}, _adapter(), _SYMBOLS, _START, _END, windows=3
    )
    assert len(summary.runs) == 3
    scored = {
        (round(run.metrics.total_return, 12), round(run.metrics.sharpe, 12)) for run in summary.runs
    }
    assert len(scored) == 3, f"windows replayed one identical price path: {scored}"


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


# --- fold construction (ADR-0026) --------------------------------------------


def test_split_folds_anchored_is_window_expands_from_the_start() -> None:
    folds = split_folds(_START, _END, 3, mode="anchored")
    assert [f.index for f in folds] == [0, 1, 2]
    # Anchored: every IS span starts at the very beginning and grows.
    assert [f.is_start for f in folds] == [_START, _START, _START]
    assert [f.is_end for f in folds] == sorted(f.is_end for f in folds)
    assert folds[-1].oos_end == _END


def test_split_folds_rolling_is_window_slides_and_stays_one_segment() -> None:
    folds = split_folds(_START, _END, 3, mode="rolling")
    # Rolling: each IS span starts where the previous one ended, so the window
    # slides instead of growing — the lengths are (near) equal, not increasing.
    assert [f.is_start for f in folds] == [_START, folds[0].is_end, folds[1].is_end]
    lengths = [(f.is_end - f.is_start).total_seconds() for f in folds]
    assert max(lengths) == pytest.approx(min(lengths), rel=1e-9)


def test_split_folds_oos_spans_march_forward_without_overlapping() -> None:
    for mode in ("anchored", "rolling"):
        folds = split_folds(_START, _END, 4, mode=mode)
        assert len(folds) == 4
        for earlier, later in pairwise(folds):
            # The next fold tests strictly after the previous one did, and picks
            # up where it left off (its IS span ends at the previous OOS end).
            assert later.oos_start > earlier.oos_end
            assert later.is_end == earlier.oos_end


def test_split_folds_in_sample_strictly_precedes_out_of_sample() -> None:
    # ADR-0001's no-look-ahead rule applied to validation: not one bar, and not
    # even one calendar day, is shared across a fold boundary.
    for mode in ("anchored", "rolling"):
        for fold in split_folds(_START, _END, 3, mode=mode):
            assert fold.is_start < fold.is_end < fold.oos_start < fold.oos_end
            assert fold.oos_start.date() > fold.is_end.date()


def test_split_folds_returns_nothing_when_no_fold_can_be_formed() -> None:
    assert split_folds(_START, _END, 0) == []
    assert split_folds(_START, _END, -1) == []
    assert split_folds(_END, _START, 3) == []
    assert split_folds(_START, _START, 3) == []


def test_split_folds_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown walk-forward mode"):
        split_folds(_START, _END, 2, mode="sideways")


# --- walk-forward: the selection discipline ----------------------------------


class _RecordingAdapter:
    """A ``DataAdapter`` spy: forwards to a real adapter and logs every request.

    ``calls`` is the ordered list of ``(symbol, start, end)`` the engine asked for,
    which is how many *runs* happened and over which spans — the only way to prove
    the one-OOS-run-per-fold rule without trusting the numbers.
    """

    def __init__(self, inner: SyntheticAdapter) -> None:
        self._inner = inner
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        self.calls.append((symbol, start, end))
        return self._inner.get_bars(symbol, start, end, adjusted=adjusted)

    def spans(self) -> list[tuple[datetime, datetime]]:
        """Just the requested spans, in call order."""
        return [(start, end) for _symbol, start, end in self.calls]


def test_walk_forward_runs_the_grid_in_sample_and_the_winner_once_out_of_sample() -> None:
    spy = _RecordingAdapter(_adapter())
    grid = {"fast": [5, 10], "slow": [30, 50]}
    summary = run_walk_forward("sma_crossover", grid, spy, _SYMBOLS, _START, _END, folds=2)

    assert summary.fold_count == 2
    combos = len(expand_grid(grid))
    per_run = len(_SYMBOLS)  # the engine asks the adapter once per symbol per run
    spans = spy.spans()
    for fold in summary.folds:
        is_span = (fold.is_start, fold.is_end)
        oos_span = (fold.oos_start, fold.oos_end)
        assert spans.count(is_span) == combos * per_run  # whole grid, in sample
        assert spans.count(oos_span) == per_run  # EXACTLY ONE out-of-sample run
        # And the OOS run happens only after in-sample selection is finished.
        assert max(i for i, s in enumerate(spans) if s == is_span) < min(
            i for i, s in enumerate(spans) if s == oos_span
        )
        assert fold.candidates == combos
    # No stray runs on any other span.
    assert len(spans) == 2 * (combos * per_run + per_run)


def test_walk_forward_winner_is_the_top_of_an_in_sample_only_sweep() -> None:
    grid = {"fast": [5, 10, 20], "slow": [30, 50]}
    summary = run_walk_forward("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END, folds=2)
    for fold in summary.folds:
        in_sample_only = run_sweep(
            "sma_crossover", grid, _adapter(), _SYMBOLS, fold.is_start, fold.is_end
        )
        best = in_sample_only.ranked(by="sharpe")[0]
        assert fold.params == best.params
        assert fold.in_sample_metrics == best.metrics


# --- the rigged fixture: the IS winner is NOT the best OOS combo --------------

_RIG_START = datetime(2021, 1, 4, tzinfo=UTC)


def _rigged_bars() -> list[Bar]:
    """A price path built so short-lookback momentum wins IS and loses OOS.

    First 120 bars: a slow 20-bar cycle, where ``lookback=2`` rides every up-leg
    and ``lookback=9`` is always half a cycle late. Next 120 bars: a steady uptrend
    hidden under a 3-bar zigzag, where ``lookback=9`` (a multiple of the zigzag)
    sees only the trend while ``lookback=2`` buys every local top. The calendar
    midpoint of the range therefore also splits the two regimes.
    """
    cycle = [100.0 + 12.0 * math.sin(2.0 * math.pi * i / 20.0) for i in range(120)]
    zig = (0.0, 7.0, -7.0)
    trend = [100.0 + 0.5 * i + zig[i % 3] for i in range(120)]

    bars: list[Bar] = []
    day = _RIG_START
    prev = cycle[0]
    for close in [*cycle, *trend]:
        while day.weekday() >= 5:  # weekdays only, matching the other adapters
            day += timedelta(days=1)
        bars.append(
            Bar(
                symbol="AAA",
                ts=day,
                open=round(prev, 4),
                high=round(max(prev, close) * 1.001, 4),
                low=round(min(prev, close) * 0.999, 4),
                close=round(close, 4),
                volume=1_000_000,
            )
        )
        prev = close
        day += timedelta(days=1)
    return bars


def test_walk_forward_reports_the_is_winners_oos_numbers_not_the_best_ones() -> None:
    bars = _rigged_bars()
    adapter = FakeAdapter(bars)
    end = bars[-1].ts
    grid = {"lookback": [2, 9]}
    risk = RiskConfig.unlimited()  # let the rigged signal show, unclamped

    summary = run_walk_forward(
        "momentum", grid, adapter, ["AAA"], _RIG_START, end, folds=1, risk=risk
    )
    (fold,) = summary.folds

    # In sample the fast lookback looks brilliant, so that is what got picked.
    assert fold.params == {"lookback": 2}
    assert fold.in_sample_metrics.sharpe > 0.0

    # Out of sample it is the *worse* of the two, and the summary says so — the
    # other combo would have scored better, and was deliberately not re-selected.
    oos_sweep = run_sweep(
        "momentum", grid, adapter, ["AAA"], fold.oos_start, fold.oos_end, risk=risk
    )
    reported = next(r for r in oos_sweep.runs if r.params == fold.params)
    rejected = next(r for r in oos_sweep.runs if r.params == {"lookback": 9})
    assert fold.out_of_sample_metrics == reported.metrics
    assert rejected.metrics.sharpe > fold.out_of_sample_metrics.sharpe
    assert fold.out_of_sample_metrics.sharpe < 0.0 < fold.in_sample_metrics.sharpe

    # Which is exactly what the honesty figures are for.
    assert summary.sharpe_degradation > 0.0
    assert summary.folds_with_positive_out_of_sample_return == 0


# --- aggregate honesty figures ------------------------------------------------


def _metrics(sharpe: float, total_return: float) -> PerformanceMetrics:
    """A metric block with only the two fields these aggregates read."""
    return PerformanceMetrics(
        total_return=total_return,
        annualized_return=0.0,
        sharpe=sharpe,
        sortino=0.0,
        calmar=0.0,
        max_drawdown=0.0,
        win_rate=0.0,
        turnover=0.0,
        avg_exposure=0.0,
        peak_exposure=0.0,
    )


def _fold(
    index: int, is_sharpe: float, oos_sharpe: float, is_ret: float, oos_ret: float
) -> WalkForwardFold:
    return WalkForwardFold(
        index=index,
        is_start=_START,
        is_end=_START,
        oos_start=_END,
        oos_end=_END,
        params={"fast": index},
        in_sample_metrics=_metrics(is_sharpe, is_ret),
        out_of_sample_metrics=_metrics(oos_sharpe, oos_ret),
        candidates=2,
        in_sample_points=10,
        out_of_sample_points=10,
    )


def test_walk_forward_aggregates_match_hand_computed_values() -> None:
    summary = WalkForwardSummary(
        strategy="sma_crossover",
        symbols=_SYMBOLS,
        mode="anchored",
        rank_by="sharpe",
        folds=[
            _fold(0, is_sharpe=2.0, oos_sharpe=1.0, is_ret=0.30, oos_ret=0.10),
            _fold(1, is_sharpe=3.0, oos_sharpe=0.5, is_ret=0.40, oos_ret=-0.05),
            _fold(2, is_sharpe=1.0, oos_sharpe=0.0, is_ret=0.20, oos_ret=0.02),
        ],
    )
    # By hand: IS Sharpe mean (2+3+1)/3 = 2.0; OOS mean (1+0.5+0)/3 = 0.5.
    assert summary.fold_count == 3
    assert summary.mean_in_sample_sharpe == pytest.approx(2.0)
    assert summary.mean_out_of_sample_sharpe == pytest.approx(0.5)
    assert summary.median_out_of_sample_sharpe == pytest.approx(0.5)
    assert summary.sharpe_degradation == pytest.approx(1.5)
    assert summary.sharpe_retention == pytest.approx(0.25)
    # Returns: IS mean 0.30; OOS mean (0.10 - 0.05 + 0.02)/3 = 0.0233...
    assert summary.mean_in_sample_total_return == pytest.approx(0.30)
    assert summary.mean_out_of_sample_total_return == pytest.approx(0.07 / 3.0)
    assert summary.median_out_of_sample_total_return == pytest.approx(0.02)
    assert summary.total_return_degradation == pytest.approx(0.30 - 0.07 / 3.0)
    assert summary.folds_with_positive_out_of_sample_return == 2


def test_walk_forward_retention_is_none_when_in_sample_sharpe_is_not_positive() -> None:
    summary = WalkForwardSummary(
        strategy="sma_crossover",
        symbols=_SYMBOLS,
        mode="anchored",
        rank_by="sharpe",
        folds=[_fold(0, is_sharpe=-1.0, oos_sharpe=-2.0, is_ret=-0.1, oos_ret=-0.2)],
    )
    # A ratio against a non-positive base is meaningless, not zero.
    assert summary.sharpe_retention is None
    assert summary.sharpe_degradation == pytest.approx(1.0)


def test_walk_forward_empty_summary_aggregates_are_zero_not_errors() -> None:
    summary = WalkForwardSummary(
        strategy="sma_crossover", symbols=_SYMBOLS, mode="anchored", rank_by="sharpe"
    )
    assert summary.fold_count == 0
    assert summary.mean_out_of_sample_sharpe == 0.0
    assert summary.median_out_of_sample_sharpe == 0.0
    assert summary.sharpe_degradation == 0.0
    assert summary.sharpe_retention is None
    assert summary.folds_with_positive_out_of_sample_return == 0


# --- determinism, modes, and degenerate input --------------------------------


def test_walk_forward_is_deterministic_on_synthetic() -> None:
    grid = {"fast": [5, 10], "slow": [30, 50]}
    first = run_walk_forward("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END, folds=3)
    second = run_walk_forward("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END, folds=3)
    assert first == second


def test_walk_forward_rolling_mode_runs_and_records_its_mode() -> None:
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5, 10], "slow": [30]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        folds=2,
        mode="rolling",
    )
    assert summary.mode == "rolling"
    assert summary.fold_count == 2
    assert [f.is_start for f in summary.folds] != [_START, _START]


def test_walk_forward_ranking_by_total_return_selects_on_that_key() -> None:
    grid = {"fast": [5, 10, 20], "slow": [30, 50]}
    summary = run_walk_forward(
        "sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END, folds=1, rank_by="total_return"
    )
    (fold,) = summary.folds
    in_sample_only = run_sweep(
        "sma_crossover", grid, _adapter(), _SYMBOLS, fold.is_start, fold.is_end
    )
    assert fold.params == in_sample_only.ranked(by="total_return")[0].params


def test_walk_forward_no_fold_possible_is_reported_not_raised() -> None:
    for folds, start, end in ((0, _START, _END), (3, _END, _START)):
        summary = run_walk_forward(
            "sma_crossover",
            {"fast": [5], "slow": [30]},
            _adapter(),
            _SYMBOLS,
            start,
            end,
            folds=folds,
        )
        assert summary.folds == []
        assert summary.unusable_folds == []
        assert any("no walk-forward fold could be formed" in w for w in summary.warnings)


def test_walk_forward_all_combos_rejected_is_reported_per_fold() -> None:
    # Every combo has fast >= slow, which SmaCrossover refuses to construct.
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [30, 40], "slow": [30]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        folds=2,
    )
    assert summary.folds == []
    assert len(summary.skipped) == 2
    assert [index for index, _reason in summary.unusable_folds] == [0, 1]
    assert all("no runnable parameter" in reason for _index, reason in summary.unusable_folds)
    assert any("no runnable parameter combination" in w for w in summary.warnings)


def test_walk_forward_warns_when_a_span_has_too_few_bars() -> None:
    # An adapter with nothing to serve: the folds still form, but their metrics are
    # structurally zero and must not read as a result.
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5], "slow": [30]},
        FakeAdapter([]),
        _SYMBOLS,
        _START,
        _END,
        folds=1,
    )
    (fold,) = summary.folds
    assert fold.in_sample_points == 0
    assert fold.out_of_sample_points == 0
    assert any("in-sample span produced 0 bar(s)" in w for w in summary.warnings)
    assert any("out-of-sample span produced 0 bar(s)" in w for w in summary.warnings)


def test_walk_forward_unknown_strategy_and_rank_key_raise() -> None:
    with pytest.raises(KeyError, match="no_such_strategy"):
        run_walk_forward("no_such_strategy", {}, _adapter(), _SYMBOLS, _START, _END)
    with pytest.raises(ValueError, match="unknown rank key"):
        run_walk_forward(
            "equal_weight", {}, _adapter(), _SYMBOLS, _START, _END, rank_by="profit_factor"
        )
    with pytest.raises(ValueError, match="unknown walk-forward mode"):
        run_walk_forward("equal_weight", {}, _adapter(), _SYMBOLS, _START, _END, mode="diagonal")
