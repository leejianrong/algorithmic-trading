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
from typing import ClassVar

import pytest

from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.synthetic import SyntheticAdapter
from trading.metrics import PerformanceMetrics
from trading.sweep import (
    CostSensitivityRun,
    CostSensitivitySummary,
    SweepRun,
    SweepSummary,
    WalkForwardFold,
    WalkForwardSummary,
    combo_key,
    expand_grid,
    neighbor_stability,
    run_cost_sensitivity_sweep,
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


def test_walk_forward_records_a_span_with_no_data_as_an_unusable_fold() -> None:
    """An adapter with nothing to serve produces no fold at all (ADR-0032).

    A span in which *no* symbol has data raises ``EmptyUniverseError`` inside the
    engine. The sweep catches it per span and records the fold as unusable rather
    than fabricating one whose metrics are structurally zero — and rather than
    letting one dataless span abort the whole sweep, which is the case that matters
    for a real universe whose members list at different times.
    """
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5], "slow": [30]},
        FakeAdapter([]),
        _SYMBOLS,
        _START,
        _END,
        folds=1,
    )
    assert summary.folds == []
    assert [index for index, _reason in summary.unusable_folds] == [0]
    (_index, reason) = summary.unusable_folds[0]
    assert "no data for any symbol" in reason


def test_walk_forward_warns_when_a_span_has_too_few_bars() -> None:
    """A span with data but almost none still warns: 1 bar is not a result.

    Distinct from the no-data case above — here the universe is non-empty, so the
    fold forms and the ``_MIN_USABLE_POINTS`` guard is what must speak up.
    """
    # One bar in each half of [_START, _END], so both the IS and the OOS span of a
    # single fold see exactly one bar.
    bars = [
        Bar("AAA", ts, 10.0, 10.0, 10.0, 10.0, 1_000)
        for ts in (datetime(2021, 6, 1, tzinfo=UTC), datetime(2022, 6, 1, tzinfo=UTC))
    ]
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5], "slow": [30]},
        FakeAdapter(bars),
        ["AAA"],
        _START,
        _END,
        folds=1,
    )
    (fold,) = summary.folds
    assert fold.in_sample_points == 1
    assert fold.out_of_sample_points == 1
    assert any("in-sample span produced 1 bar(s)" in w for w in summary.warnings)
    assert any("out-of-sample span produced 1 bar(s)" in w for w in summary.warnings)


def test_walk_forward_unknown_strategy_and_rank_key_raise() -> None:
    with pytest.raises(KeyError, match="no_such_strategy"):
        run_walk_forward("no_such_strategy", {}, _adapter(), _SYMBOLS, _START, _END)
    with pytest.raises(ValueError, match="unknown rank key"):
        run_walk_forward(
            "equal_weight", {}, _adapter(), _SYMBOLS, _START, _END, rank_by="profit_factor"
        )
    with pytest.raises(ValueError, match="unknown walk-forward mode"):
        run_walk_forward("equal_weight", {}, _adapter(), _SYMBOLS, _START, _END, mode="diagonal")


# --- trial accounting and the deflated winner (KAN-619, ADR-0039) -------------


def test_every_run_records_its_return_moments() -> None:
    """The five floats the deflation needs, captured without keeping the curve."""
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    assert summary.runs
    for run in summary.runs:
        assert run.moments is not None
        assert run.moments.count > 0
        assert run.moments.stdev > 0.0
        # The moments describe the same series the metrics do: mean / stdev,
        # annualized, is exactly the Sharpe already on the run.
        annualized = run.moments.mean / run.moments.stdev * math.sqrt(252.0)
        assert annualized == pytest.approx(run.metrics.sharpe)


def test_trial_count_is_the_number_of_runs_that_competed() -> None:
    """A trial is a (combination, window) run — the granularity a winner is picked at."""
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    assert summary.trial_count == 4
    assert len(summary.trial_sharpes()) == 4


def test_windows_multiply_the_trial_count() -> None:
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        windows=3,
    )
    assert summary.trial_count == 6


def test_rejected_combos_are_not_trials() -> None:
    """A combination the constructor refused never ran, so it never had a chance to win."""
    summary = run_sweep(
        "sma_crossover",
        {"fast": [10, 50], "slow": [30]},  # fast=50 >= slow=30 is rejected
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    assert len(summary.skipped) == 1
    assert summary.trial_count == 1


def test_deflated_winner_scores_the_top_ranked_run_against_the_whole_search() -> None:
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10, 15], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    deflated = summary.deflated_winner()
    assert deflated is not None
    assert deflated.trials == summary.trial_count == 6
    assert deflated.observed_sharpe == pytest.approx(summary.ranked()[0].metrics.sharpe)
    # Six candidates with a spread of Sharpes means the null is no longer zero:
    # the best of six coin flips already looks like an edge.
    assert deflated.null_best_sharpe > 0.0
    assert deflated.trial_sharpe_stdev is not None


def test_deflated_winner_prior_trials_widens_the_count_and_never_lowers_the_null() -> None:
    """ADR-0062: a ledger's cumulative count reaches the sweep the same way it
    reaches a plain backtest — through ``prior_trials``, threaded to
    ``deflated_sharpe`` unchanged."""
    summary = run_sweep(
        "sma_crossover",
        {"fast": [5, 10, 15], "slow": [30, 50]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    unledgered = summary.deflated_winner()
    ledgered = summary.deflated_winner(prior_trials=18)
    assert unledgered is not None
    assert ledgered is not None
    assert ledgered.trials == unledgered.trials + 18 == 24
    assert ledgered.null_best_sharpe >= unledgered.null_best_sharpe
    assert summary.deflated_winner(prior_trials=0) == unledgered


def test_a_bigger_grid_raises_the_bar_the_winner_must_clear() -> None:
    """The point of KAN-619: searching harder makes the winner *less* impressive."""
    small = run_sweep(
        "sma_crossover", {"fast": [5], "slow": [30, 50]}, _adapter(), _SYMBOLS, _START, _END
    )
    large = run_sweep(
        "sma_crossover",
        {"fast": [5, 10, 15, 20], "slow": [30, 50, 80]},
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
    )
    small_deflated = small.deflated_winner()
    large_deflated = large.deflated_winner()
    assert small_deflated is not None
    assert large_deflated is not None
    assert large_deflated.trials > small_deflated.trials
    assert large_deflated.null_best_sharpe > small_deflated.null_best_sharpe


def test_deflated_winner_is_none_when_there_is_nothing_to_deflate() -> None:
    empty = run_sweep(
        "sma_crossover", {"fast": [50], "slow": [30]}, _adapter(), _SYMBOLS, _START, _END
    )
    assert empty.runs == []
    assert empty.deflated_winner() is None


def test_deflated_winner_is_none_for_a_hand_built_summary_without_moments() -> None:
    """An honest absence: a summary that never recorded moments cannot be deflated."""
    summary = SweepSummary(
        strategy="sma_crossover",
        symbols=["AAA"],
        runs=[
            SweepRun(
                params={"fast": 5},
                metrics=_metrics(1.5, 0.2),
                window=0,
                start=_START,
                end=_END,
            )
        ],
    )
    assert summary.trial_count == 1
    assert summary.deflated_winner() is None


def test_deflation_is_deterministic_and_needs_no_rng() -> None:
    first = run_sweep(
        "sma_crossover", {"fast": [5, 10], "slow": [30, 50]}, _adapter(), _SYMBOLS, _START, _END
    )
    second = run_sweep(
        "sma_crossover", {"fast": [5, 10], "slow": [30, 50]}, _adapter(), _SYMBOLS, _START, _END
    )
    assert first.deflated_winner() == second.deflated_winner()


# --- the annualization basis reaches every trial (KAN-840, ADR-0059) ----------
#
# ``_run_combo`` called ``compute(result)`` with no basis, so every trial took
# ``metrics.compute``'s 252.0 default however the bars were spaced. A sweep at
# ``--interval 5m`` therefore reported a US-equity *daily* year for 5-minute bars,
# understating Sharpe by ``sqrt(19656 / 252)`` = 8.83x. ADR-0054's defect, one
# module along.

# A US-equity 5-minute year: 252 sessions x (390 min / 5 min).
_FIVE_MINUTE_YEAR = 252.0 * (390.0 / 5.0)
# What every risk-adjusted figure is out by when 5m bars are annualized daily.
_FIVE_MINUTE_RATIO = math.sqrt(_FIVE_MINUTE_YEAR / 252.0)

_GRID = {"fast": [5, 10], "slow": [30, 50]}


def _swept(periods_per_year: float | None = None) -> SweepSummary:
    """The same sweep, optionally on a non-default annualization basis."""
    if periods_per_year is None:
        return run_sweep("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END)
    return run_sweep(
        "sma_crossover",
        _GRID,
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        periods_per_year=periods_per_year,
    )


def test_the_basis_reaches_every_trials_metrics() -> None:
    """The defect itself: a sweep's Sharpe must follow the interval it ran at."""
    daily = _swept()
    five_minute = _swept(_FIVE_MINUTE_YEAR)

    assert len(daily.runs) == len(five_minute.runs) == 4
    for slow, fast in zip(daily.runs, five_minute.runs, strict=True):
        assert slow.params == fast.params
        # Identical bars, identical fills — only the year they are annualized on.
        assert fast.metrics.sharpe == pytest.approx(slow.metrics.sharpe * _FIVE_MINUTE_RATIO)
        assert fast.metrics.sortino == pytest.approx(slow.metrics.sortino * _FIVE_MINUTE_RATIO)


def test_total_return_and_drawdown_do_not_move_with_the_basis() -> None:
    """Why a mis-annualized sweep is incoherent, not merely biased (ADR-0054).

    The unscaled figures stay put while the annualized ones move, so a wrong basis
    pairs an honest drawdown with a Sharpe from another market's year.
    """
    daily = _swept()
    five_minute = _swept(_FIVE_MINUTE_YEAR)

    for slow, fast in zip(daily.runs, five_minute.runs, strict=True):
        assert fast.metrics.total_return == slow.metrics.total_return
        assert fast.metrics.max_drawdown == slow.metrics.max_drawdown
        assert fast.metrics.win_rate == slow.metrics.win_rate
        # ...while these two do.
        assert fast.metrics.annualized_return != slow.metrics.annualized_return
        assert fast.metrics.turnover == pytest.approx(slow.metrics.turnover * 78.0)


def test_a_summary_records_the_basis_its_metrics_were_computed_on() -> None:
    """The number is on the summary, so nothing downstream has to guess it."""
    assert _swept().periods_per_year == 252.0
    assert _swept(_FIVE_MINUTE_YEAR).periods_per_year == _FIVE_MINUTE_YEAR


def test_the_default_basis_is_the_equity_daily_year() -> None:
    """Unchanged for every existing caller: a daily equity sweep still reads 252."""
    summary = _swept()
    for run in summary.runs:
        assert run.moments is not None
        annualized = run.moments.mean / run.moments.stdev * math.sqrt(252.0)
        assert annualized == pytest.approx(run.metrics.sharpe)


def test_the_deflation_reads_the_basis_the_trials_were_scored_on() -> None:
    """The incoherence KAN-840 actually shipped.

    ``deflated_winner`` received the *correct* ``periods_per_year`` from the CLI and
    applied it to ``trial_sharpes()`` — which were annualized at 252. One calculation,
    two years. The symptom was visible on stdout: the ranking table printed the
    winner at ``0.593`` while the deflation block under it called the same run
    ``observed +5.24``.
    """
    summary = _swept(_FIVE_MINUTE_YEAR)
    deflated = summary.deflated_winner()

    assert deflated is not None
    winner = summary.ranked()[0]
    assert deflated.observed_sharpe == pytest.approx(winner.metrics.sharpe)


def test_the_null_best_sharpe_moves_with_the_basis_too() -> None:
    """Not just the observed figure: the bar it must clear is annualized as well.

    ``null_best_sharpe`` is built from the spread of ``trial_sharpes()``, so leaving
    those at 252 pinned the null to the equity daily year while the observed Sharpe
    followed the interval — making the winner look 8.83x more significant than it is.
    """
    daily = _swept().deflated_winner()
    five_minute = _swept(_FIVE_MINUTE_YEAR).deflated_winner()

    assert daily is not None
    assert five_minute is not None
    assert daily.null_best_sharpe > 0.0
    assert five_minute.null_best_sharpe == pytest.approx(
        daily.null_best_sharpe * _FIVE_MINUTE_RATIO
    )
    assert five_minute.trial_sharpe_stdev is not None
    assert daily.trial_sharpe_stdev is not None
    assert five_minute.trial_sharpe_stdev == pytest.approx(
        daily.trial_sharpe_stdev * _FIVE_MINUTE_RATIO
    )


def test_the_deflation_probability_is_basis_free() -> None:
    """The one figure that must *not* move: a probability is not annualized.

    PSR compares the winner's per-bar moments against a per-bar threshold. Both
    de-annualize by the same root, so the answer is invariant — which is precisely
    what a mixed-basis calculation broke.
    """
    daily = _swept().deflated_winner()
    five_minute = _swept(_FIVE_MINUTE_YEAR).deflated_winner()

    assert daily is not None
    assert five_minute is not None
    assert daily.probability is not None
    assert five_minute.probability == pytest.approx(daily.probability)


def test_deflating_on_a_basis_the_runs_were_not_scored_on_is_refused() -> None:
    """The mixed-basis calculation is now unrepresentable, not merely unlikely.

    A caller bug, in the same class as ``deflated_sharpe`` raising on an empty
    ``trial_sharpes``: the trial Sharpes are fixed at the basis they were computed
    on, so re-deflating them at another year is arithmetic on two calendars.
    """
    summary = _swept()

    assert summary.deflated_winner("sharpe", 252.0) == summary.deflated_winner()
    with pytest.raises(ValueError, match="annualized at 252"):
        summary.deflated_winner("sharpe", _FIVE_MINUTE_YEAR)


def test_walk_forward_folds_are_annualized_on_the_basis_too() -> None:
    """``--folds`` shares ``_run_combo``, so it had the same defect — silently.

    A sweep at least printed a deflation block whose observed Sharpe disagreed with
    the table. A walk-forward prints IS/OOS Sharpes and nothing to contradict them.
    """
    daily = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    five_minute = run_walk_forward(
        "sma_crossover",
        _GRID,
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        folds=2,
        periods_per_year=_FIVE_MINUTE_YEAR,
    )

    assert daily.fold_count == five_minute.fold_count == 2
    assert daily.periods_per_year == 252.0
    assert five_minute.periods_per_year == _FIVE_MINUTE_YEAR
    for slow, fast in zip(daily.folds, five_minute.folds, strict=True):
        # The same fold picked the same winner — only the year moved.
        assert fast.params == slow.params
        assert fast.out_of_sample_metrics.total_return == slow.out_of_sample_metrics.total_return
        assert fast.in_sample_metrics.sharpe == pytest.approx(
            slow.in_sample_metrics.sharpe * _FIVE_MINUTE_RATIO
        )
        assert fast.out_of_sample_metrics.sharpe == pytest.approx(
            slow.out_of_sample_metrics.sharpe * _FIVE_MINUTE_RATIO
        )


def test_the_basis_never_changes_which_combination_wins() -> None:
    """Why this survived: one constant factor across trials is monotonic.

    The ranking is genuinely unaffected — which is what made the table look
    self-consistent while every absolute figure in it was wrong.
    """
    daily = _swept()
    five_minute = _swept(_FIVE_MINUTE_YEAR)

    assert [run.params for run in daily.ranked()] == [run.params for run in five_minute.ranked()]
    assert [run.params for run in daily.ranked("total_return")] == [
        run.params for run in five_minute.ranked("total_return")
    ]


# --- walk-forward's own trial accounting and IS deflation (ADR-0074, KAN-677) -
#
# Each fold internally sweeps the whole grid in-sample to pick a winner, so the
# honest trial count behind a walk-forward's own search is (folds x grid size),
# not the grid size once. The deflation must score the IS side — the thing a
# search actually produced — never the single, never-selected OOS run.


def test_fold_records_every_in_sample_candidates_sharpe() -> None:
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    combos = len(expand_grid(_GRID))
    assert summary.fold_count == 2
    for fold in summary.folds:
        assert fold.candidates == combos
        assert len(fold.in_sample_candidate_sharpes) == combos
        # The winner's own Sharpe is one of the pooled candidates, not a stray value.
        assert fold.in_sample_metrics.sharpe in fold.in_sample_candidate_sharpes


def test_fold_records_the_winners_own_per_bar_in_sample_returns() -> None:
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    for fold in summary.folds:
        # daily_returns() of an N-point curve has N-1 entries.
        assert len(fold.in_sample_winner_returns) == fold.in_sample_points - 1


def test_in_sample_trial_count_is_folds_times_grid_size() -> None:
    """The card's central claim, checked directly: (folds) x (grid size)."""
    combos = len(expand_grid(_GRID))
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=3)
    assert summary.fold_count == 3
    assert summary.in_sample_trial_count == 3 * combos
    assert len(summary.in_sample_trial_sharpes()) == summary.in_sample_trial_count


def test_in_sample_trial_count_only_counts_completed_folds() -> None:
    """A fold whose OOS span has no data contributes nothing to the count.

    Mirrors SweepSummary.trial_count's own "only what actually ran" rule: the IS
    side of this fold really did score candidates, but the fold never completed
    (no OOS result), so it is excluded exactly as ``unusable_folds`` excludes it
    from every other aggregate.
    """
    # Data only in the first half of the range, so the anchored fold's IS span
    # (which starts at `start`) has data but the single fold's OOS span does not.
    bars = [
        Bar("AAA", datetime(2021, 1, d, tzinfo=UTC), 10.0, 10.0, 10.0, 10.0, 1_000)
        for d in range(1, 29)
    ]
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5], "slow": [10]},
        FakeAdapter(bars),
        ["AAA"],
        _START,
        _END,
        folds=1,
    )
    assert summary.folds == []
    assert summary.unusable_folds
    assert summary.in_sample_trial_count == 0


def test_deflated_in_sample_scores_against_the_full_search() -> None:
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    deflated = summary.deflated_in_sample()
    assert deflated is not None
    assert deflated.trials == summary.in_sample_trial_count
    assert deflated.trial_sharpe_stdev is not None


def test_deflated_in_sample_prior_trials_widens_the_count_and_never_lowers_the_null() -> None:
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    unledgered = summary.deflated_in_sample()
    ledgered = summary.deflated_in_sample(prior_trials=10)
    assert unledgered is not None
    assert ledgered is not None
    assert ledgered.trials == unledgered.trials + 10
    assert ledgered.null_best_sharpe >= unledgered.null_best_sharpe
    assert summary.deflated_in_sample(prior_trials=0) == unledgered


def test_deflated_in_sample_is_none_without_completed_folds() -> None:
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5], "slow": [30]},
        FakeAdapter([]),
        _SYMBOLS,
        _START,
        _END,
        folds=1,
    )
    assert summary.folds == []
    assert summary.deflated_in_sample() is None


def test_deflated_in_sample_is_none_when_pooled_returns_have_no_dispersion() -> None:
    """A fold with one bar per span has nothing for return_moments to compute."""
    bars = [
        Bar("AAA", ts, 10.0, 10.0, 10.0, 10.0, 1_000)
        for ts in (datetime(2021, 6, 1, tzinfo=UTC), datetime(2022, 6, 1, tzinfo=UTC))
    ]
    summary = run_walk_forward(
        "sma_crossover",
        {"fast": [5], "slow": [30]},
        FakeAdapter(bars),
        ["AAA"],
        _START,
        _END,
        folds=1,
    )
    assert summary.fold_count == 1
    assert summary.folds[0].in_sample_winner_returns == ()
    assert summary.deflated_in_sample() is None


def test_deflating_walk_forward_on_a_basis_not_scored_on_is_refused() -> None:
    """Mirrors SweepSummary.deflated_winner's own basis-mismatch guard (KAN-840)."""
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    assert summary.deflated_in_sample(252.0) == summary.deflated_in_sample()
    with pytest.raises(ValueError, match="annualized at 252"):
        summary.deflated_in_sample(_FIVE_MINUTE_YEAR)


def test_deflated_in_sample_scores_the_in_sample_side_not_out_of_sample() -> None:
    """The card's core correctness requirement, checked directly.

    Reuses the rigged fixture (see the IS-vs-OOS section above) where the winner
    looks brilliant in-sample and loses out-of-sample. Deflating the wrong side
    would report the OOS run's *negative* Sharpe; deflating the right side reports
    the positive one the search actually produced.
    """
    bars = _rigged_bars()
    adapter = FakeAdapter(bars)
    end = bars[-1].ts
    grid = {"lookback": [2, 9]}
    risk = RiskConfig.unlimited()

    summary = run_walk_forward(
        "momentum", grid, adapter, ["AAA"], _RIG_START, end, folds=1, risk=risk
    )
    (fold,) = summary.folds
    assert fold.in_sample_metrics.sharpe > 0.0 > fold.out_of_sample_metrics.sharpe

    deflated = summary.deflated_in_sample()
    assert deflated is not None
    assert deflated.observed_sharpe > 0.0


def test_walk_forward_bootstrap_is_off_by_default() -> None:
    summary = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=1)
    assert summary.folds
    assert all(fold.out_of_sample_sharpe_interval is None for fold in summary.folds)


def test_walk_forward_bootstrap_populates_the_oos_interval() -> None:
    """Scoped to OOS: the one curve per fold that was observed, not selected."""
    summary = run_walk_forward(
        "sma_crossover",
        _GRID,
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        folds=1,
        bootstrap=True,
        bootstrap_resamples=30,
        bootstrap_seed=7,
    )
    (fold,) = summary.folds
    interval = fold.out_of_sample_sharpe_interval
    assert interval is not None
    assert interval.resamples == 30
    assert interval.seed == 7


def test_walk_forward_bootstrap_never_touches_the_in_sample_side() -> None:
    """--bootstrap changes nothing about the (already-free) IS deflation."""
    plain = run_walk_forward("sma_crossover", _GRID, _adapter(), _SYMBOLS, _START, _END, folds=2)
    bootstrapped = run_walk_forward(
        "sma_crossover",
        _GRID,
        _adapter(),
        _SYMBOLS,
        _START,
        _END,
        folds=2,
        bootstrap=True,
        bootstrap_resamples=30,
    )
    assert plain.deflated_in_sample() == bootstrapped.deflated_in_sample()
    for plain_fold, boot_fold in zip(plain.folds, bootstrapped.folds, strict=True):
        assert plain_fold.in_sample_candidate_sharpes == boot_fold.in_sample_candidate_sharpes
        assert plain_fold.in_sample_winner_returns == boot_fold.in_sample_winner_returns


# --- parameter-stability: a combo's score next to its grid-neighbour mean -----
#
# ADR-0065 / KAN-620: a flat ranked CSV hides whether a winner sits on a plateau or
# a spike a real search would not reliably land on. For a grid of `fast` x `slow`,
# combo (fast=10, slow=100)'s neighbours are the adjacent `fast` values at
# slow=100 and the adjacent `slow` values at fast=10 — never a diagonal move.


def test_combo_key_is_order_independent() -> None:
    """Two combos with the same pairs must compare equal regardless of dict order."""
    assert combo_key({"fast": 5, "slow": 30}) == combo_key({"slow": 30, "fast": 5})
    assert combo_key({"fast": 5, "slow": 30}) != combo_key({"fast": 5, "slow": 50})


class TestNeighborStabilityPure:
    """The pure function on a hand-built grid + score map — no engine, no adapter."""

    _GRID: ClassVar[dict[str, list[object]]] = {"fast": [5, 10, 15], "slow": [30, 50, 80]}

    def _scores(self, **overrides: float) -> dict[tuple[tuple[str, object], ...], float]:
        """All nine combos of ``_GRID`` scored ``0.0``, with ``overrides`` applied.

        ``overrides`` keys are ``"fast,slow"`` strings for brevity, e.g. ``"10,50"``.
        """
        base = {
            combo_key({"fast": f, "slow": s}): 0.0
            for f in self._GRID["fast"]
            for s in self._GRID["slow"]
        }
        for spec, score in overrides.items():
            f, s = (int(x) for x in spec.split(","))
            base[combo_key({"fast": f, "slow": s})] = score
        return base

    def test_a_center_combo_averages_all_four_neighbours(self) -> None:
        """(10, 50) sits in the middle of a 3x3 grid: 2 fast neighbours + 2 slow ones."""
        scores = self._scores(
            **{"10,50": 1.0, "5,50": 0.2, "15,50": 0.4, "10,30": 0.6, "10,80": 0.8}
        )
        rows = neighbor_stability(self._GRID, scores)
        row = next(r for r in rows if r.params == {"fast": 10, "slow": 50})
        assert row.score == 1.0
        assert row.neighbor_count == 4
        assert row.neighbor_mean is not None
        assert row.neighbor_mean == pytest.approx((0.2 + 0.4 + 0.6 + 0.8) / 4.0)
        assert row.gap == pytest.approx(1.0 - row.neighbor_mean)

    def test_a_corner_combo_has_only_two_neighbours(self) -> None:
        """(5, 30) is a corner: no fast value below 5, no slow value below 30."""
        scores = self._scores(**{"5,30": 1.0, "10,30": 0.5, "5,50": 0.3})
        rows = neighbor_stability(self._GRID, scores)
        row = next(r for r in rows if r.params == {"fast": 5, "slow": 30})
        assert row.neighbor_count == 2
        assert row.neighbor_mean == pytest.approx((0.5 + 0.3) / 2.0)

    def test_a_missing_neighbour_is_excluded_not_treated_as_zero(self) -> None:
        """A combo the strategy constructor rejected never enters the mean."""
        scores = self._scores(**{"10,50": 1.0, "5,50": 0.2, "10,30": 0.6, "10,80": 0.8})
        # (15, 50) never ran (as if the strategy constructor rejected it) — remove
        # it from the score map entirely, not merely leave it at the 0.0 baseline.
        del scores[combo_key({"fast": 15, "slow": 50})]
        rows = neighbor_stability(self._GRID, scores)
        row = next(r for r in rows if r.params == {"fast": 10, "slow": 50})
        assert row.neighbor_count == 3
        assert row.neighbor_mean == pytest.approx((0.2 + 0.6 + 0.8) / 3.0)

    def test_no_neighbours_at_all_is_none_not_zero(self) -> None:
        """An honest absence: a combo whose every neighbour is missing scores no mean."""
        scores = {combo_key({"fast": 10, "slow": 50}): 1.0}
        rows = neighbor_stability(self._GRID, scores)
        assert len(rows) == 1
        assert rows[0].neighbor_count == 0
        assert rows[0].neighbor_mean is None
        assert rows[0].gap is None

    def test_a_single_value_axis_contributes_no_neighbours(self) -> None:
        """A parameter that was not actually swept has nothing adjacent to compare."""
        grid: dict[str, list[object]] = {"fast": [10], "slow": [30, 50, 80]}
        scores = {
            combo_key({"fast": 10, "slow": s}): score
            for s, score in zip([30, 50, 80], [0.1, 1.0, 0.3], strict=True)
        }
        rows = neighbor_stability(grid, scores)
        row = next(r for r in rows if r.params == {"fast": 10, "slow": 50})
        # Only the slow axis is swept: one neighbour each side, none from fast.
        assert row.neighbor_count == 2
        assert row.neighbor_mean == pytest.approx((0.1 + 0.3) / 2.0)

    def test_neighbours_are_positional_in_the_grids_own_list_order(self) -> None:
        """Adjacency follows the grid's list order, not numeric distance.

        `fast=[5, 20, 10]` (deliberately out of numeric order) must treat 20 as
        adjacent to both 5 and 10 — the same order `expand_grid` reads.
        """
        grid: dict[str, list[object]] = {"fast": [5, 20, 10], "slow": [30]}
        scores = {
            combo_key({"fast": 5, "slow": 30}): 1.0,
            combo_key({"fast": 20, "slow": 30}): 2.0,
            combo_key({"fast": 10, "slow": 30}): 3.0,
        }
        rows = neighbor_stability(grid, scores)
        middle = next(r for r in rows if r.params == {"fast": 20, "slow": 30})
        assert middle.neighbor_count == 2
        assert middle.neighbor_mean == pytest.approx((1.0 + 3.0) / 2.0)
        edge = next(r for r in rows if r.params == {"fast": 5, "slow": 30})
        # 5 is adjacent only to 20 (index 1), not to 10 (index 2) — list order.
        assert edge.neighbor_count == 1
        assert edge.neighbor_mean == pytest.approx(2.0)

    def test_a_cliff_is_a_large_positive_gap(self) -> None:
        """The motivating case: a spike surrounded by much lower scores."""
        scores = self._scores(
            **{"10,50": 5.0, "5,50": 0.1, "15,50": 0.1, "10,30": 0.1, "10,80": 0.1}
        )
        rows = neighbor_stability(self._GRID, scores)
        row = next(r for r in rows if r.params == {"fast": 10, "slow": 50})
        assert row.gap is not None
        assert row.gap > 4.0  # far above its neighbours' mean of 0.1

    def test_returns_one_row_per_scored_combo_in_the_scores_order(self) -> None:
        scores = {
            combo_key({"fast": 5, "slow": 30}): 1.0,
            combo_key({"fast": 10, "slow": 30}): 2.0,
        }
        rows = neighbor_stability(self._GRID, scores)
        assert [r.params for r in rows] == [{"fast": 5, "slow": 30}, {"fast": 10, "slow": 30}]


class TestSweepSummaryStability:
    """The end-to-end path: `run_sweep`'s own summary feeds `stability()`."""

    def test_stability_is_empty_for_a_hand_built_summary_without_a_grid(self) -> None:
        """An honest absence, the same idiom as `deflated_winner`'s missing moments."""
        summary = SweepSummary(
            strategy="sma_crossover",
            symbols=["AAA"],
            runs=[
                SweepRun(
                    params={"fast": 5}, metrics=_metrics(1.0, 0.1), window=0, start=_START, end=_END
                )
            ],
        )
        assert summary.grid == {}
        assert summary.stability() == []

    def test_run_sweep_records_its_grid_on_the_summary(self) -> None:
        grid = {"fast": [5, 10], "slow": [30, 50]}
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
        assert summary.grid == grid

    def test_stability_covers_every_combo_that_ran(self) -> None:
        grid = {"fast": [5, 10, 15], "slow": [30, 50, 80]}
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
        rows = summary.stability()
        assert {tuple(sorted(r.params.items())) for r in rows} == {
            tuple(sorted(run.params.items())) for run in summary.runs
        }

    def test_stability_matches_the_pure_function_on_the_summarys_own_scores(self) -> None:
        grid = {"fast": [5, 10, 15], "slow": [30, 50, 80]}
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
        rows = summary.stability()
        expected = {
            combo_key(r.params): (r.score, r.neighbor_mean, r.neighbor_count, r.gap) for r in rows
        }
        direct = neighbor_stability(grid, summary.combo_scores())
        for row in direct:
            key = combo_key(row.params)
            assert expected[key] == (row.score, row.neighbor_mean, row.neighbor_count, row.gap)

    def test_stability_is_deterministic(self) -> None:
        grid = {"fast": [5, 10, 15], "slow": [30, 50, 80]}
        first = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END).stability()
        second = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END).stability()
        assert first == second

    def test_stability_does_not_change_the_ranking_or_any_existing_field(self) -> None:
        """Purely additive reporting: computing it must not perturb the summary."""
        grid = {"fast": [5, 10], "slow": [30, 50]}
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
        before = (summary.ranked(), summary.trial_count, summary.skipped, summary.deflated_winner())
        summary.stability()
        after = (summary.ranked(), summary.trial_count, summary.skipped, summary.deflated_winner())
        assert before == after

    def test_a_skipped_combo_never_enters_a_neighbours_mean(self) -> None:
        """`sma_crossover` rejects fast >= slow; a rejected combo cannot pull a mean."""
        grid = {"fast": [10, 40], "slow": [30]}  # fast=40 >= slow=30 is invalid
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
        assert len(summary.skipped) == 1
        rows = summary.stability()
        # Only the one runnable combo produced a score, so it has zero neighbours —
        # never a mean silently computed from a combo that was never run.
        assert len(rows) == 1
        assert rows[0].neighbor_count == 0
        assert rows[0].neighbor_mean is None

    def test_combo_scores_averages_across_windows(self) -> None:
        """`--windows` runs a combo multiple times; stability reads the combo's mean."""
        grid = {"fast": [5], "slow": [30]}
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END, windows=3)
        scores = summary.combo_scores()
        only_key = combo_key({"fast": 5, "slow": 30})
        window_sharpes = [run.metrics.sharpe for run in summary.runs]
        assert len(window_sharpes) == 3
        assert scores[only_key] == pytest.approx(sum(window_sharpes) / 3.0)

    def test_stability_ranks_by_total_return_when_asked(self) -> None:
        grid = {"fast": [5, 10], "slow": [30, 50]}
        summary = run_sweep("sma_crossover", grid, _adapter(), _SYMBOLS, _START, _END)
        rows = summary.stability(by="total_return")
        expected_scores = {combo_key(r.params): r.score for r in rows}
        for run in summary.runs:
            assert expected_scores[combo_key(run.params)] == pytest.approx(run.metrics.total_return)


# --- cost-sensitivity sweep: re-run one fixed combo across a slippage grid ----


def _cost_run(
    slippage_bps: float, *, total_return: float = 0.0, sharpe: float = 0.0
) -> CostSensitivityRun:
    # _metrics(sharpe, total_return) is the module-level helper defined above,
    # under "aggregate honesty figures" — reused rather than re-declared.
    return CostSensitivityRun(
        slippage_bps=slippage_bps,
        taker_fee_bps=0.0,
        metrics=_metrics(sharpe, total_return),
    )


class TestEdgeDeathPure:
    """`CostSensitivitySummary.edge_death` on hand-built runs — no engine calls."""

    def test_no_runs_is_none(self) -> None:
        summary = CostSensitivitySummary(strategy="sma_crossover", symbols=["AAA"], params={})
        assert summary.edge_death() is None

    def test_interpolates_the_crossing_between_the_bracketing_levels(self) -> None:
        # total_return: +0.04 at 10 bps, -0.01 at 25 bps -> crosses zero 4/5 of the
        # way from 10 to 25, i.e. at 22 bps.
        runs = [
            _cost_run(10.0, total_return=0.04),
            _cost_run(25.0, total_return=-0.01),
        ]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        death = summary.edge_death(metric="total_return")
        assert death is not None
        assert not death.already_dead
        assert not death.survives_grid
        assert death.crossing_bps == pytest.approx(22.0)

    def test_reads_runs_in_ascending_slippage_order_regardless_of_input_order(self) -> None:
        # Same scenario as above, but the runs list is built out of order.
        runs = [
            _cost_run(25.0, total_return=-0.01),
            _cost_run(10.0, total_return=0.04),
        ]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        death = summary.edge_death(metric="total_return")
        assert death is not None
        assert death.crossing_bps == pytest.approx(22.0)

    def test_already_dead_at_the_cheapest_level(self) -> None:
        runs = [_cost_run(5.0, total_return=-0.02), _cost_run(50.0, total_return=-0.10)]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        death = summary.edge_death(metric="total_return")
        assert death is not None
        assert death.already_dead
        assert not death.survives_grid
        assert death.crossing_bps == pytest.approx(5.0)

    def test_already_dead_when_the_cheapest_level_sits_exactly_at_the_threshold(self) -> None:
        # threshold is inclusive: an exact 0.0 at the cheapest level is already dead,
        # not "still alive by a hair".
        runs = [_cost_run(5.0, total_return=0.0), _cost_run(50.0, total_return=-0.10)]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        death = summary.edge_death(metric="total_return")
        assert death is not None
        assert death.already_dead
        assert death.crossing_bps == pytest.approx(5.0)

    def test_survives_the_whole_grid(self) -> None:
        runs = [_cost_run(5.0, total_return=0.10), _cost_run(50.0, total_return=0.02)]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        death = summary.edge_death(metric="total_return")
        assert death is not None
        assert death.survives_grid
        assert not death.already_dead
        assert death.crossing_bps is None

    def test_exact_zero_at_a_tested_level_is_the_crossing_not_an_interpolation(self) -> None:
        runs = [_cost_run(10.0, total_return=0.02), _cost_run(25.0, total_return=0.0)]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        death = summary.edge_death(metric="total_return")
        assert death is not None
        assert death.crossing_bps == pytest.approx(25.0)

    def test_sharpe_metric_is_read_independently_of_total_return(self) -> None:
        runs = [
            _cost_run(10.0, total_return=0.5, sharpe=0.2),
            _cost_run(25.0, total_return=0.4, sharpe=-0.1),
        ]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        by_sharpe = summary.edge_death(metric="sharpe")
        by_return = summary.edge_death(metric="total_return")
        assert by_sharpe is not None
        assert by_sharpe.survives_grid is False
        assert by_return is not None
        assert by_return.survives_grid is True

    def test_unknown_metric_raises(self) -> None:
        summary = CostSensitivitySummary(
            strategy="s", symbols=["AAA"], params={}, runs=[_cost_run(5.0)]
        )
        with pytest.raises(ValueError, match="unknown"):
            summary.edge_death(metric="bogus")

    def test_a_custom_threshold_is_honored(self) -> None:
        runs = [_cost_run(10.0, total_return=0.10), _cost_run(25.0, total_return=0.03)]
        summary = CostSensitivitySummary(strategy="s", symbols=["AAA"], params={}, runs=runs)
        # Never crosses 0.0, but does cross a 0.05 threshold.
        at_zero = summary.edge_death(metric="total_return", threshold=0.0)
        assert at_zero is not None
        assert at_zero.survives_grid
        death = summary.edge_death(metric="total_return", threshold=0.05)
        assert death is not None
        assert not death.survives_grid


class TestRunCostSensitivitySweep:
    """`run_cost_sensitivity_sweep`: one fixed combo re-run at every slippage level."""

    def test_runs_once_per_deduplicated_sorted_level(self) -> None:
        summary = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 30},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=[50.0, 5.0, 25.0, 10.0, 10.0],
        )
        assert [run.slippage_bps for run in summary.runs] == [5.0, 10.0, 25.0, 50.0]
        assert summary.params == {"fast": 5, "slow": 30}

    def test_params_are_held_fixed_across_every_level(self) -> None:
        summary = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 30},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=[5.0, 50.0],
        )
        assert summary.params == {"fast": 5, "slow": 30}

    def test_higher_slippage_never_improves_a_high_turnover_strategys_return(self) -> None:
        # sma_crossover trades on every crossover; more slippage can only cost more.
        summary = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 20},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=[5.0, 10.0, 25.0, 50.0],
        )
        returns = [run.metrics.total_return for run in summary.runs]
        assert returns == sorted(returns, reverse=True)

    def test_is_deterministic(self) -> None:
        params = {"fast": 5, "slow": 30}
        first = run_cost_sensitivity_sweep(
            "sma_crossover", params, _adapter(), _SYMBOLS, _START, _END, slippage_bps=[5.0, 25.0]
        )
        second = run_cost_sensitivity_sweep(
            "sma_crossover", params, _adapter(), _SYMBOLS, _START, _END, slippage_bps=[5.0, 25.0]
        )
        assert [(r.slippage_bps, r.metrics) for r in first.runs] == [
            (r.slippage_bps, r.metrics) for r in second.runs
        ]

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(KeyError):
            run_cost_sensitivity_sweep(
                "no-such-strategy", {}, _adapter(), _SYMBOLS, _START, _END, slippage_bps=[5.0]
            )

    def test_invalid_combo_raises_before_running_anything(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            run_cost_sensitivity_sweep(
                "sma_crossover",
                {"fast": 40, "slow": 30},  # fast >= slow: rejected by the constructor
                _adapter(),
                _SYMBOLS,
                _START,
                _END,
                slippage_bps=[5.0, 10.0],
            )

    def test_empty_slippage_grid_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            run_cost_sensitivity_sweep(
                "sma_crossover",
                {"fast": 5, "slow": 30},
                _adapter(),
                _SYMBOLS,
                _START,
                _END,
                slippage_bps=[],
            )

    def test_a_symbol_slippage_tier_is_refused(self) -> None:
        """Sweeping the flat rate wouldn't move a tiered symbol's effective cost (ADR-0063)."""
        tiered = CostConfig(symbol_slippage_bps={"AAA": 2.0})
        with pytest.raises(ValueError, match="per-symbol"):
            run_cost_sensitivity_sweep(
                "sma_crossover",
                {"fast": 5, "slow": 30},
                _adapter(),
                _SYMBOLS,
                _START,
                _END,
                slippage_bps=[5.0, 10.0],
                base_costs=tiered,
            )

    def test_a_dataless_span_is_recorded_as_an_unusable_level_not_raised(self) -> None:
        summary = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 30},
            FakeAdapter([]),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=[5.0, 10.0],
        )
        assert summary.runs == []
        assert [level for level, _reason in summary.unusable_levels] == [5.0, 10.0]

    def test_every_run_records_its_return_moments(self) -> None:
        summary = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 30},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=[5.0, 25.0],
        )
        assert all(run.moments is not None for run in summary.runs)

    def test_taker_fee_and_commission_are_held_at_the_base_cost_model(self) -> None:
        base = CostConfig(commission_per_share=0.01, taker_fee_bps=3.0)
        summary = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 30},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=[5.0, 25.0],
            base_costs=base,
        )
        assert all(run.taker_fee_bps == 3.0 for run in summary.runs)

    def test_a_low_turnover_strategy_degrades_slower_than_a_high_turnover_one(self) -> None:
        """The headline claim (KAN-618): high turnover should die faster under cost.

        Same universe, same range, same slippage grid — only the strategy's own
        turnover differs (`sma_crossover` trades every crossover; `equal_weight`
        rebalances to one static target once and mostly holds).
        """
        levels = [5.0, 10.0, 25.0, 50.0]
        high_turnover = run_cost_sensitivity_sweep(
            "sma_crossover",
            {"fast": 5, "slow": 20},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=levels,
        )
        low_turnover = run_cost_sensitivity_sweep(
            "equal_weight",
            {},
            _adapter(),
            _SYMBOLS,
            _START,
            _END,
            slippage_bps=levels,
        )
        high_degradation = (
            high_turnover.runs[0].metrics.total_return - high_turnover.runs[-1].metrics.total_return
        )
        low_degradation = (
            low_turnover.runs[0].metrics.total_return - low_turnover.runs[-1].metrics.total_return
        )
        assert high_degradation > low_degradation
