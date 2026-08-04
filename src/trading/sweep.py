"""Parameter sweep and walk-forward — OUTER loops over ``Engine.run``.

This module runs the *existing* backtest engine many times; it is not an engine
feature (ADR-0016). Given a strategy name, a parameter grid, a data adapter, a
symbol universe, and a date range, it expands the grid into every combination
(cartesian product), constructs a parameterized strategy for each via the
``STRATEGIES`` registry, runs ``Engine.run`` once per combination, computes the
V4 :func:`~trading.metrics.compute` metrics on each result, and returns a
structured summary that can be ranked by Sharpe or total return.

Two outer loops live here, and the difference between them is the difference
between a hopeful number and an honest one:

* :func:`run_sweep` — a *plain* grid sweep, optionally split into ``windows``
  consecutive calendar spans with every combination run independently on each
  window. Every metric it reports is **in-sample**: the same data chose the
  parameters and scored them.
* :func:`run_walk_forward` — true **in-sample -> out-of-sample** walk-forward
  (ADR-0026). Each fold optimizes the grid on its in-sample (IS) span only, picks
  the single best combination, and runs *that one combination exactly once* over
  the untouched out-of-sample (OOS) span that follows. The OOS numbers are never
  used to select anything, so they are the closest thing this bench offers to an
  unbiased estimate of forward performance. ADR-0016 parked this recombination as
  "a later slice"; ADR-0026 is that slice.

Everything here is pure with respect to the inputs: no wall clock, no RNG, no
network. Determinism comes entirely from the injected adapter (seed a
``SyntheticAdapter`` for offline, repeatable sweeps) and the deterministic grid
expansion order, so the same strategy + grid + adapter + range always yields the
same ranked summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import product
from statistics import fmean, median
from typing import TYPE_CHECKING, cast

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.engine import EmptyUniverseError, Engine
from trading.metrics import PerformanceMetrics, compute
from trading.risk import Guardrails
from trading.strategies import STRATEGIES
from trading.types import Portfolio

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from trading.interfaces import DataAdapter, Strategy

# A single parameter combination, e.g. ``{"fast": 5, "slow": 30}``.
ParamCombo = dict[str, object]

# The two ways a walk-forward's in-sample window can be shaped (ADR-0026).
WALK_FORWARD_MODES = ("anchored", "rolling")


def _next_day_start(ts: datetime) -> datetime:
    """Midnight of the calendar day after ``ts``, keeping ``ts``'s tzinfo.

    How a fold boundary is drawn: the boundary *day* belongs to in-sample, and the
    out-of-sample span starts the next day. A microsecond offset would suffice for
    an adapter that filters on exact timestamps, but not for one that filters at day
    granularity (``SyntheticAdapter`` truncates a request to whole days), and the
    whole point of a fold boundary is that no single bar is both an optimization
    input and an out-of-sample observation — ADR-0001's no-look-ahead rule applied
    to validation (ADR-0026).
    """
    day = ts.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=ts.tzinfo)


# Ranking keys → how to read them off :class:`~trading.metrics.PerformanceMetrics`.
# Both are "higher is better", so ranking sorts descending on the chosen key.
_RANK_KEYS: dict[str, Callable[[PerformanceMetrics], float]] = {
    "sharpe": lambda m: m.sharpe,
    "total_return": lambda m: m.total_return,
}


def _rank_key(by: str) -> Callable[[PerformanceMetrics], float]:
    """Look up a ranking accessor by name, or raise naming the known keys.

    Shared by :meth:`SweepSummary.ranked` and the walk-forward's in-sample
    selection so both rank on exactly the same definitions.
    """
    try:
        return _RANK_KEYS[by]
    except KeyError:
        known = ", ".join(sorted(_RANK_KEYS))
        raise ValueError(f"unknown rank key {by!r}; known: {known}") from None


def expand_grid(grid: Mapping[str, Sequence[object]]) -> list[ParamCombo]:
    """Expand a parameter grid into the cartesian product of its value lists.

    ``{"fast": [5, 10], "slow": [30, 50]}`` -> four combos, in a deterministic
    order (grid-key order outermost, first key varying slowest). An empty grid
    yields a single empty combo (one run with the strategy's own defaults); a key
    whose value list is empty collapses the whole product to zero combos.
    """
    keys = list(grid)
    value_lists = [list(grid[key]) for key in keys]
    return [dict(zip(keys, combo, strict=True)) for combo in product(*value_lists)]


def split_windows(
    start: datetime,
    end: datetime,
    windows: int,
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end]`` into ``windows`` consecutive equal calendar spans.

    Returns ``(win_start, win_end)`` pairs covering the range back-to-back; the
    last window ends exactly at ``end`` so rounding never drops the final days.
    ``windows <= 1`` (or a non-positive/zero-length range) returns the single
    ``[start, end]`` window unchanged.
    """
    if windows <= 1 or end <= start:
        return [(start, end)]
    span = (end - start) / windows
    bounds = [start + span * i for i in range(windows)]
    bounds.append(end)
    return [(bounds[i], bounds[i + 1]) for i in range(windows)]


@dataclass(frozen=True, slots=True)
class FoldSpans:
    """The IS and OOS date spans of one walk-forward fold (ADR-0026).

    ``is_start``/``is_end`` bound the in-sample span the grid is optimized on;
    ``oos_start``/``oos_end`` bound the out-of-sample span the single IS winner is
    then tested on. ``oos_start`` is midnight of the day *after* ``is_end``, so the
    two spans share no bar and not even a calendar day — the boundary day is
    in-sample.
    """

    index: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime


def split_folds(
    start: datetime,
    end: datetime,
    folds: int,
    *,
    mode: str = "anchored",
) -> list[FoldSpans]:
    """Split ``[start, end]`` into ``folds`` consecutive IS -> OOS fold spans.

    The range is cut into ``folds + 1`` equal segments (via :func:`split_windows`,
    so the last segment ends exactly at ``end``); fold *k* tests on segment
    ``k + 1`` and optimizes on what came before it:

    * ``mode="anchored"`` (default) — the IS span always starts at ``start`` and
      *expands* with each fold, using every bar available before the OOS span.
    * ``mode="rolling"`` — the IS span is the single segment immediately before the
      OOS span, so a fixed-length window *slides* forward and old regimes are
      forgotten.

    Both are fully deterministic (pure arithmetic on the two datetimes). The OOS
    spans march forward without overlapping, and each starts the day after its own
    IS span ends, so nothing an OOS run sees was ever an optimization input.
    Returns an empty list when no fold can be formed (``folds < 1``, or a
    non-positive/zero-length range) — the caller surfaces that, rather than a
    fabricated fold. Raises ``ValueError`` for an unknown ``mode``.
    """
    if mode not in WALK_FORWARD_MODES:
        known = ", ".join(WALK_FORWARD_MODES)
        raise ValueError(f"unknown walk-forward mode {mode!r}; known: {known}")
    if folds < 1 or end <= start:
        return []
    segments = split_windows(start, end, folds + 1)
    bounds = [seg_start for seg_start, _seg_end in segments]
    bounds.append(end)
    return [
        FoldSpans(
            index=k,
            is_start=start if mode == "anchored" else bounds[k],
            is_end=bounds[k + 1],
            oos_start=_next_day_start(bounds[k + 1]),
            oos_end=bounds[k + 2],
        )
        for k in range(folds)
    ]


@dataclass(frozen=True, slots=True)
class SweepRun:
    """One backtest within a sweep: a parameter combo over one window's metrics.

    ``window`` is 0 for a plain grid sweep and the 0-based window index under
    walk-forward; ``start``/``end`` are that window's actual date bounds.
    """

    params: ParamCombo
    metrics: PerformanceMetrics
    window: int
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """The full set of runs a sweep produced, rankable by a headline metric."""

    strategy: str
    symbols: list[str]
    runs: list[SweepRun] = field(default_factory=list)
    # Combos skipped because the strategy rejected them (e.g. sma fast >= slow),
    # paired with the constructor's error message — surfaced, never silent.
    skipped: list[tuple[ParamCombo, str]] = field(default_factory=list)
    # Windows dropped because no symbol had data in them (ADR-0032) — e.g. an early
    # window predating a whole universe's listings. Reported, never silent.
    empty_windows: list[str] = field(default_factory=list)

    def ranked(self, by: str = "sharpe") -> list[SweepRun]:
        """Runs sorted best-first by ``by`` ('sharpe' or 'total_return').

        The sort is stable, so ties keep their grid-expansion order — the result
        is fully deterministic. Raises ``ValueError`` for an unknown key.
        """
        key = _rank_key(by)
        return sorted(self.runs, key=lambda run: key(run.metrics), reverse=True)


def _build_strategy(name: str, combo: ParamCombo) -> Strategy:
    """Construct a parameterized strategy from the registry factory.

    The registry factory *is* the strategy class (``STRATEGIES[name](**combo)``
    builds a configured instance); its declared type takes no args, so we widen
    it to accept the combo's keyword parameters.
    """
    factory = cast("Callable[..., Strategy]", STRATEGIES[name])
    return factory(**combo)


def _run_combo(
    strategy: str,
    combo: ParamCombo,
    adapter: DataAdapter,
    tickers: list[str],
    start: datetime,
    end: datetime,
    *,
    cash: float,
    risk: RiskConfig,
) -> tuple[PerformanceMetrics, int]:
    """Run one combo over one span and return its metrics + equity-curve length.

    A *fresh* :class:`~trading.broker.SimulatedBroker` and
    :class:`~trading.risk.Guardrails` per call, so no portfolio or kill-switch
    state ever leaks between runs (ADR-0016). The curve length is returned so
    callers can tell a real result from a span that produced (almost) no bars.

    A span in which *no* symbol has data raises
    :class:`~trading.engine.EmptyUniverseError` from the engine (ADR-0032). That is
    fatal to one run but not to a sweep: an early walk-forward fold can legitimately
    predate a whole universe's listings. Callers catch it and record the span as
    unusable, which is why this does not swallow it here.
    """
    broker = SimulatedBroker(Portfolio(cash=cash))
    engine = Engine(adapter, broker, Guardrails(risk))
    result = engine.run(_build_strategy(strategy, combo), tickers, start, end)
    return compute(result), len(result.equity_curve)


def _partition_grid(
    strategy: str,
    grid: Mapping[str, Sequence[object]],
) -> tuple[list[ParamCombo], list[tuple[ParamCombo, str]]]:
    """Split the expanded grid into runnable combos and (combo, reason) skips.

    A combination the strategy constructor rejects (e.g. ``sma_crossover`` with
    ``fast >= slow``) is skipped and reported, never run and never fatal.
    """
    runnable: list[ParamCombo] = []
    skipped: list[tuple[ParamCombo, str]] = []
    for combo in expand_grid(grid):
        try:
            # Construct once to fail fast on an invalid combo before running any
            # span; a fresh instance per run is built inside _run_combo.
            _build_strategy(strategy, combo)
        except (ValueError, TypeError) as exc:
            skipped.append((combo, str(exc)))
        else:
            runnable.append(combo)
    return runnable, skipped


def _require_known_strategy(strategy: str) -> None:
    """Raise ``KeyError`` naming the registry if ``strategy`` is not registered."""
    if strategy not in STRATEGIES:
        known = ", ".join(sorted(STRATEGIES))
        raise KeyError(f"unknown strategy {strategy!r}; known strategies: {known}")


def run_sweep(
    strategy: str,
    grid: Mapping[str, Sequence[object]],
    adapter: DataAdapter,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    cash: float = 1_000.0,
    risk: RiskConfig | None = None,
    windows: int = 1,
) -> SweepSummary:
    """Run ``strategy`` over every grid combination (x every window) and rank.

    For each combination the strategy is built with those parameters and run
    through a *fresh* :class:`~trading.broker.SimulatedBroker` and
    :class:`~trading.risk.Guardrails` (so runs never share state), once per
    walk-forward window. A combination the strategy constructor rejects (e.g.
    ``sma_crossover`` with ``fast >= slow``) is recorded in ``skipped`` and its
    runs omitted, rather than aborting the whole sweep.

    ``risk`` defaults to the enforced :class:`~trading.config.RiskConfig`
    defaults; pass ``RiskConfig.unlimited()`` to sweep unconstrained. Determinism
    is inherited from ``adapter`` — nothing here consults a clock or RNG.

    Every metric this returns is **in-sample**: the same bars that ranked a combo
    also produced its numbers. For an out-of-sample estimate use
    :func:`run_walk_forward` (ADR-0026).
    """
    _require_known_strategy(strategy)

    tickers = list(symbols)
    risk_config = risk if risk is not None else RiskConfig()
    spans = split_windows(start, end, windows)
    runnable, skipped = _partition_grid(strategy, grid)

    runs: list[SweepRun] = []
    empty_windows: list[str] = []
    for combo in runnable:
        for window_index, (win_start, win_end) in enumerate(spans):
            try:
                metrics, _points = _run_combo(
                    strategy,
                    combo,
                    adapter,
                    tickers,
                    win_start,
                    win_end,
                    cash=cash,
                    risk=risk_config,
                )
            except EmptyUniverseError as exc:
                # A window predating the universe's listings is not a sweep failure
                # (ADR-0032); drop that window's run and report it once.
                note = f"window {window_index} has no data for any symbol: {exc}"
                if note not in empty_windows:
                    empty_windows.append(note)
                continue
            runs.append(
                SweepRun(
                    params=dict(combo),
                    metrics=metrics,
                    window=window_index,
                    start=win_start,
                    end=win_end,
                )
            )

    return SweepSummary(
        strategy=strategy,
        symbols=tickers,
        runs=runs,
        skipped=skipped,
        empty_windows=empty_windows,
    )


# A span needs at least two equity points for a return (hence any ratio) to exist;
# below that the metric block is structurally zero and must not read as a result.
_MIN_USABLE_POINTS = 2


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One fold's honest record: what IS chose, and how it then did on OOS.

    ``params`` is the single combination the *in-sample* span selected;
    ``in_sample_metrics`` are that winner's IS numbers (optimistic by construction)
    and ``out_of_sample_metrics`` are the one untouched OOS run of that same
    combination (ADR-0026). ``candidates`` is how many combos were scored in-sample
    to pick it, and the ``*_points`` counts are each span's equity-curve length, so
    a near-empty span is visible instead of masquerading as a flat result.
    """

    index: int
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime
    params: ParamCombo
    in_sample_metrics: PerformanceMetrics
    out_of_sample_metrics: PerformanceMetrics
    candidates: int
    in_sample_points: int
    out_of_sample_points: int


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    """Every fold of a walk-forward plus the aggregate IS -> OOS honesty figures.

    The aggregate properties spell out ``in_sample`` / ``out_of_sample`` in full so
    a reader can never mistake an optimized number for an unbiased one. All of them
    are derived from :attr:`folds`, so the dataclass stays a pure record.

    Degenerate input is surfaced, never silent: ``skipped`` carries grid
    combinations the strategy constructor rejected (as in :class:`SweepSummary`),
    ``unusable_folds`` carries ``(fold_index, reason)`` for folds that could not
    produce an OOS test at all, and ``warnings`` carries range-level or
    too-little-data notes.
    """

    strategy: str
    symbols: list[str]
    mode: str
    rank_by: str
    folds: list[WalkForwardFold] = field(default_factory=list)
    skipped: list[tuple[ParamCombo, str]] = field(default_factory=list)
    unusable_folds: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def fold_count(self) -> int:
        """How many folds completed an IS selection *and* its one OOS run."""
        return len(self.folds)

    @property
    def mean_in_sample_sharpe(self) -> float:
        """Mean Sharpe of the fold winners on their own (optimized) IS spans."""
        return self._mean(lambda f: f.in_sample_metrics.sharpe)

    @property
    def mean_out_of_sample_sharpe(self) -> float:
        """Mean Sharpe across the untouched OOS spans — the headline honest figure."""
        return self._mean(lambda f: f.out_of_sample_metrics.sharpe)

    @property
    def median_out_of_sample_sharpe(self) -> float:
        """Median OOS Sharpe: the same story with one lucky fold's leverage removed."""
        return self._median(lambda f: f.out_of_sample_metrics.sharpe)

    @property
    def mean_in_sample_total_return(self) -> float:
        """Mean total return of the fold winners on their own IS spans."""
        return self._mean(lambda f: f.in_sample_metrics.total_return)

    @property
    def mean_out_of_sample_total_return(self) -> float:
        """Mean total return across the OOS spans."""
        return self._mean(lambda f: f.out_of_sample_metrics.total_return)

    @property
    def median_out_of_sample_total_return(self) -> float:
        """Median total return across the OOS spans."""
        return self._median(lambda f: f.out_of_sample_metrics.total_return)

    @property
    def sharpe_degradation(self) -> float:
        """Mean IS Sharpe minus mean OOS Sharpe — positive means OOS was worse.

        The single number that says how much of the sweep's apparent edge was
        parameter fitting. 0.0 with no folds.
        """
        return self.mean_in_sample_sharpe - self.mean_out_of_sample_sharpe

    @property
    def total_return_degradation(self) -> float:
        """Mean IS total return minus mean OOS total return (positive = OOS worse)."""
        return self.mean_in_sample_total_return - self.mean_out_of_sample_total_return

    @property
    def sharpe_retention(self) -> float | None:
        """Fraction of the mean IS Sharpe that survived out of sample.

        ``mean_out_of_sample_sharpe / mean_in_sample_sharpe``: 1.0 means the edge
        held, 0.5 means half of it was fitting, negative means OOS lost money while
        IS made it. ``None`` — not 0.0 — when the mean IS Sharpe is not positive,
        because the ratio is then meaningless rather than zero.
        """
        in_sample = self.mean_in_sample_sharpe
        if in_sample <= 0.0:
            return None
        return self.mean_out_of_sample_sharpe / in_sample

    @property
    def folds_with_positive_out_of_sample_return(self) -> int:
        """How many folds actually made money out of sample (strictly > 0)."""
        return sum(1 for f in self.folds if f.out_of_sample_metrics.total_return > 0.0)

    def _mean(self, read: Callable[[WalkForwardFold], float]) -> float:
        """Mean of ``read`` over the folds, 0.0 when there are none."""
        if not self.folds:
            return 0.0
        return fmean(read(fold) for fold in self.folds)

    def _median(self, read: Callable[[WalkForwardFold], float]) -> float:
        """Median of ``read`` over the folds, 0.0 when there are none."""
        if not self.folds:
            return 0.0
        return median(read(fold) for fold in self.folds)


@dataclass(frozen=True, slots=True)
class _Scored:
    """An in-sample candidate: its combo, metrics, and equity-curve length."""

    combo: ParamCombo
    metrics: PerformanceMetrics
    points: int


def run_walk_forward(
    strategy: str,
    grid: Mapping[str, Sequence[object]],
    adapter: DataAdapter,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    folds: int = 3,
    mode: str = "anchored",
    rank_by: str = "sharpe",
    cash: float = 1_000.0,
    risk: RiskConfig | None = None,
) -> WalkForwardSummary:
    """Walk ``[start, end]`` forward, optimizing on IS and testing once on OOS.

    The central discipline of honest validation (ADR-0026), and the recombination
    ADR-0016 deferred. For each fold produced by :func:`split_folds`:

    1. every runnable grid combination is run over the **in-sample** span,
    2. they are ranked by ``rank_by`` (``'sharpe'`` or ``'total_return'``, the same
       :meth:`SweepSummary.ranked` definitions, stable so ties keep grid order),
    3. the single best combination is then run **exactly once** over the
       out-of-sample span — and nothing about the OOS result feeds back into any
       selection, ever. That one-run rule is what makes the OOS numbers unbiased.

    ``mode`` picks an ``'anchored'`` (expanding, the default) or ``'rolling'``
    (fixed-length, sliding) IS window. Each run gets a fresh broker and guardrails,
    and nothing here reads a clock or an RNG, so the same inputs always yield an
    equal summary.

    Degenerate input is reported on the result rather than raised: a range that
    cannot form even one IS/OOS pair, a grid whose every combination the strategy
    rejects, a fold with no runnable combination, and spans too short to define a
    return all land in ``warnings`` / ``unusable_folds`` / ``skipped``. Only a
    genuinely unusable *argument* raises: an unknown ``strategy`` (``KeyError``), or
    an unknown ``mode`` / ``rank_by`` (``ValueError``).
    """
    _require_known_strategy(strategy)
    key = _rank_key(rank_by)

    tickers = list(symbols)
    risk_config = risk if risk is not None else RiskConfig()
    spans = split_folds(start, end, folds, mode=mode)
    runnable, skipped = _partition_grid(strategy, grid)

    completed: list[WalkForwardFold] = []
    unusable: list[tuple[int, str]] = []
    warnings: list[str] = []

    if not spans:
        warnings.append(
            f"no walk-forward fold could be formed: folds={folds} over "
            f"[{start.isoformat()}, {end.isoformat()}] leaves no room for both an "
            f"in-sample and an out-of-sample span (need folds >= 1 and end > start)"
        )
    if not runnable:
        warnings.append(
            f"no runnable parameter combination: all {len(skipped)} expanded "
            f"combination(s) were rejected by the {strategy!r} constructor"
        )

    for span in spans:
        if not runnable:
            unusable.append((span.index, "no runnable parameter combination in the grid"))
            continue

        # --- in-sample: score the whole grid, then pick exactly one winner -----
        # A span predating the whole universe's listings raises EmptyUniverseError
        # (ADR-0032). That kills one fold, not the sweep: an early anchored fold over
        # a real universe can legitimately sit before every symbol existed.
        scored: list[_Scored] = []
        try:
            for combo in runnable:
                metrics, points = _run_combo(
                    strategy,
                    combo,
                    adapter,
                    tickers,
                    span.is_start,
                    span.is_end,
                    cash=cash,
                    risk=risk_config,
                )
                scored.append(_Scored(combo=combo, metrics=metrics, points=points))
        except EmptyUniverseError as exc:
            unusable.append((span.index, f"in-sample span has no data for any symbol: {exc}"))
            continue
        # A stable argmax: ``max`` keeps the first maximal element, so ties resolve
        # to grid-expansion order and the choice is fully deterministic.
        winner = max(scored, key=lambda candidate: key(candidate.metrics))

        # --- out-of-sample: the winner, ONCE, on data selection never saw ------
        try:
            oos_metrics, oos_points = _run_combo(
                strategy,
                winner.combo,
                adapter,
                tickers,
                span.oos_start,
                span.oos_end,
                cash=cash,
                risk=risk_config,
            )
        except EmptyUniverseError as exc:
            unusable.append((span.index, f"out-of-sample span has no data for any symbol: {exc}"))
            continue

        if winner.points < _MIN_USABLE_POINTS:
            warnings.append(
                f"fold {span.index}: in-sample span produced {winner.points} bar(s); "
                f"the selection was effectively arbitrary"
            )
        if oos_points < _MIN_USABLE_POINTS:
            warnings.append(
                f"fold {span.index}: out-of-sample span produced {oos_points} bar(s); "
                f"its metrics are structurally zero, not a result"
            )

        completed.append(
            WalkForwardFold(
                index=span.index,
                is_start=span.is_start,
                is_end=span.is_end,
                oos_start=span.oos_start,
                oos_end=span.oos_end,
                params=dict(winner.combo),
                in_sample_metrics=winner.metrics,
                out_of_sample_metrics=oos_metrics,
                candidates=len(scored),
                in_sample_points=winner.points,
                out_of_sample_points=oos_points,
            )
        )

    return WalkForwardSummary(
        strategy=strategy,
        symbols=tickers,
        mode=mode,
        rank_by=rank_by,
        folds=completed,
        skipped=skipped,
        unusable_folds=unusable,
        warnings=warnings,
    )
