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

A sweep also knows the one thing a single backtest cannot: **how many trials
competed**. :attr:`SweepSummary.trial_count` and
:meth:`SweepSummary.deflated_winner` carry that count into the report so "best of
24 configurations" stops reading like a finding (ADR-0039). The deflation itself
is pure arithmetic — no RNG — and only the bootstrap in :mod:`trading.metrics`
draws random numbers, always from an explicitly seeded generator.

Both loops annualize on a basis their caller names — ``periods_per_year``, recorded
on the summary they return (KAN-840). It is not a detail: it is the single knob under
Sharpe, Sortino, Calmar, annualized return and turnover, and this module used to take
:func:`~trading.metrics.compute`'s 252.0 default for every trial however the bars were
spaced, so ``trading sweep --interval 5m`` reported a US-equity *daily* year. Because
one constant factor across trials cannot reorder them the ranking always looked
self-consistent, which is why the table stayed wrong through several releases. The
interval itself never arrives here — ADR-0022 keeps the bar length an
adapter-construction property — only the basis does.

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
from trading.config import CostConfig, RiskConfig
from trading.engine import EmptyUniverseError, Engine
from trading.frequency import TRADING_DAYS_PER_YEAR
from trading.metrics import (
    DeflatedSharpe,
    PerformanceMetrics,
    ReturnMoments,
    compute,
    curve_moments,
    deflated_sharpe,
)
from trading.risk import Guardrails
from trading.strategies import STRATEGIES
from trading.types import Portfolio

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from trading.interfaces import DataAdapter, Strategy

# A single parameter combination, e.g. ``{"fast": 5, "slow": 30}``.
ParamCombo = dict[str, object]

# The annualization basis a sweep assumes when its caller names none: the US-equity
# *daily* year, which is what :func:`trading.metrics.compute` defaults to and what a
# ``trading sweep --interval 1d`` on ``--market us_equity`` genuinely is. Spelled as
# the calendar view rather than a bare ``252.0`` so the assumption is legible at the
# point it is made (ADR-0054). The CLI never relies on it — it passes the run's own
# ``Frequency.periods_per_year`` — but a library caller sweeping daily equity bars
# gets the right answer without having to say so.
DEFAULT_PERIODS_PER_YEAR = TRADING_DAYS_PER_YEAR

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

    ``moments`` holds the run's return-series moments (ADR-0039) — five floats, not
    the whole return series — which is exactly what
    :func:`~trading.metrics.deflated_sharpe` needs to discount the eventual winner
    for the number of combinations that competed. It defaults to ``None`` so
    hand-built :class:`SweepRun` fixtures stay valid, and ``None`` means "not
    recorded", never "no dispersion".
    """

    params: ParamCombo
    metrics: PerformanceMetrics
    window: int
    start: datetime
    end: datetime
    moments: ReturnMoments | None = None


@dataclass(frozen=True, slots=True)
class SweepSummary:
    """The full set of runs a sweep produced, rankable by a headline metric.

    ``periods_per_year`` is the annualization basis **every** run in ``runs`` was
    scored on, carried on the summary rather than left for a reader to infer
    (KAN-840). It is what makes the object self-describing: ``metrics.sharpe`` is a
    bare float with no unit attached, so a summary that did not say which year it
    used could only be read correctly by someone who already knew.
    """

    strategy: str
    symbols: list[str]
    runs: list[SweepRun] = field(default_factory=list)
    # Combos skipped because the strategy rejected them (e.g. sma fast >= slow),
    # paired with the constructor's error message — surfaced, never silent.
    skipped: list[tuple[ParamCombo, str]] = field(default_factory=list)
    # Windows dropped because no symbol had data in them (ADR-0032) — e.g. an early
    # window predating a whole universe's listings. Reported, never silent.
    empty_windows: list[str] = field(default_factory=list)
    # The year every run's annualized figures are expressed in. Defaulted so a
    # hand-built summary stays valid and every existing caller is unaffected; the
    # CLI always sets it from the run's own Frequency.
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR

    def ranked(self, by: str = "sharpe") -> list[SweepRun]:
        """Runs sorted best-first by ``by`` ('sharpe' or 'total_return').

        The sort is stable, so ties keep their grid-expansion order — the result
        is fully deterministic. Raises ``ValueError`` for an unknown key.
        """
        key = _rank_key(by)
        return sorted(self.runs, key=lambda run: key(run.metrics), reverse=True)

    @property
    def trial_count(self) -> int:
        """How many runs competed for the top of the ranking (KAN-619, ADR-0039).

        **One completed run is one trial** — a ``(combination, window)`` pair, since
        that is the granularity :meth:`ranked` sorts and therefore the granularity
        at which a winner is *selected*. Combinations the strategy constructor
        rejected never ran and never had a chance to win, so ``skipped`` does not
        count; a window dropped for having no data did not produce a candidate
        either.

        This is what the tool can see. Every earlier invocation of ``trading
        sweep``, every date range tried and abandoned, every strategy compared by
        hand — all invisible, so any correction built on this number is a lower
        bound. :func:`~trading.metrics.assess_significance` prints that caveat
        alongside the figure rather than letting it read as a complete accounting.
        """
        return len(self.runs)

    def trial_sharpes(self) -> list[float]:
        """The annualized Sharpe of every run, in grid-expansion order.

        The input :func:`~trading.metrics.expected_max_sharpe` needs: both the
        *count* of trials and their *spread* determine how high the luckiest
        skill-free candidate would have scored.

        Annualized **on this summary's** :attr:`periods_per_year`, which is why
        :meth:`deflated_winner` may not be handed a different one.
        """
        return [run.metrics.sharpe for run in self.runs]

    def deflated_winner(
        self,
        by: str = "sharpe",
        periods_per_year: float | None = None,
    ) -> DeflatedSharpe | None:
        """Deflate the top-ranked run's Sharpe for the whole search (ADR-0039).

        The answer to "best of 24 configs" reading like a finding: the winner is
        scored against the Sharpe the luckiest of those 24 would have shown with no
        edge at all. ``None`` when there are no runs, or when the winner's moments
        were not recorded (a hand-built summary) — an honest absence, never a
        flattering skip.

        ``periods_per_year`` defaults to :attr:`periods_per_year`, the basis the runs
        were actually scored on, and an explicit value that *disagrees* with it
        raises. That is KAN-840's subtler half. This method used to default to a bare
        252.0 while the CLI passed the run's true basis, so a single calculation
        mixed two years: :func:`~trading.metrics.deflated_sharpe` de-annualized
        :meth:`trial_sharpes` by ``sqrt(periods_per_year)`` while those Sharpes had
        been annualized at 252, leaving ``null_best_sharpe`` pinned to the equity
        daily year and ``observed_sharpe`` following the interval. On a 5m sweep the
        two disagreed by 8.83x *on the same printed block*, and the winner cleared a
        bar 8.83x too low. Uniformly wrong would at least have been monotonic;
        this was incoherent, the way ADR-0054 describes a report pairing an honest
        drawdown with a foreign Sharpe.

        Raising rather than quietly re-annualizing is deliberate: the trial Sharpes
        are already fixed at the basis they were computed on, so "deflate these at a
        different year" has no correct answer to give — a caller bug, in the same
        class as :func:`~trading.metrics.deflated_sharpe` raising on an empty
        ``trial_sharpes``.
        """
        basis = self.periods_per_year if periods_per_year is None else periods_per_year
        if basis != self.periods_per_year:
            raise ValueError(
                f"cannot deflate at {basis:g} bars/year: these {len(self.runs)} trial "
                f"Sharpe(s) are annualized at {self.periods_per_year:g} bars/year, and "
                "mixing the two bases yields a null threshold on one calendar and an "
                "observed Sharpe on another (KAN-840). Re-run the sweep on the basis "
                "you want."
            )
        winners = self.ranked(by)
        if not winners:
            return None
        moments = winners[0].moments
        if moments is None:
            return None
        return deflated_sharpe(moments, self.trial_sharpes(), basis)


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
    costs: CostConfig,
    periods_per_year: float,
) -> tuple[PerformanceMetrics, int, ReturnMoments | None]:
    """Run one combo over one span; return its metrics, bar count, and moments.

    A *fresh* :class:`~trading.broker.SimulatedBroker` and
    :class:`~trading.risk.Guardrails` per call, so no portfolio or kill-switch
    state ever leaks between runs (ADR-0016). The curve length is returned so
    callers can tell a real result from a span that produced (almost) no bars, and
    the return-series moments (ADR-0039) so the winner can later be deflated
    without re-running anything or retaining the whole curve.

    ``periods_per_year`` is **required**, not defaulted, and that is the whole of
    KAN-840: this call used to read ``compute(result)``, taking
    :func:`~trading.metrics.compute`'s 252.0 whatever the bars were spaced at, so a
    ``--interval 5m`` sweep reported a US-equity *daily* year — every Sharpe,
    Sortino, Calmar, annualized return and turnover in the table understated by
    ``sqrt(19656 / 252)`` = 8.83x. Defaulting it here would leave the same silence
    one layer down; the two public entry points default it once, visibly, and
    nothing between them may.

    Note what the interval does *not* do: it never reaches this function as an
    interval. ADR-0022 makes the bar length an adapter-construction property, so the
    adapter already holds it and the ``DataAdapter`` protocol deliberately does not
    expose it. What travels is the annualization *basis* — the one number the
    metrics need — never the frequency, so sweep still knows nothing about bar
    lengths.

    A span in which *no* symbol has data raises
    :class:`~trading.engine.EmptyUniverseError` from the engine (ADR-0032). That is
    fatal to one run but not to a sweep: an early walk-forward fold can legitimately
    predate a whole universe's listings. Callers catch it and record the span as
    unusable, which is why this does not swallow it here.
    """
    broker = SimulatedBroker(Portfolio(cash=cash), costs)
    engine = Engine(adapter, broker, Guardrails(risk))
    result = engine.run(_build_strategy(strategy, combo), tickers, start, end)
    return (
        compute(result, periods_per_year),
        len(result.equity_curve),
        curve_moments(result.equity_curve),
    )


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
    costs: CostConfig | None = None,
    windows: int = 1,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
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

    ``periods_per_year`` is the annualization basis every trial is scored on, and it
    must match the interval the ``adapter`` was built at (KAN-840). It defaults to
    the equity daily year, which is what a daily equity sweep is; ``trading sweep``
    always passes the run's own ``Frequency.periods_per_year``, so ``--interval`` and
    ``--market`` both reach the table. The choice is recorded on
    :attr:`SweepSummary.periods_per_year`, and the ranking is invariant to it — one
    constant factor applied to every trial cannot reorder them, which is exactly why
    the wrong basis went unnoticed for so long while every absolute figure was off.

    Every metric this returns is **in-sample**: the same bars that ranked a combo
    also produced its numbers. For an out-of-sample estimate use
    :func:`run_walk_forward` (ADR-0026).
    """
    _require_known_strategy(strategy)

    tickers = list(symbols)
    risk_config = risk if risk is not None else RiskConfig()
    cost_config = costs if costs is not None else CostConfig()
    spans = split_windows(start, end, windows)
    runnable, skipped = _partition_grid(strategy, grid)

    runs: list[SweepRun] = []
    empty_windows: list[str] = []
    for combo in runnable:
        for window_index, (win_start, win_end) in enumerate(spans):
            try:
                metrics, _points, moments = _run_combo(
                    strategy,
                    combo,
                    adapter,
                    tickers,
                    win_start,
                    win_end,
                    cash=cash,
                    risk=risk_config,
                    costs=cost_config,
                    periods_per_year=periods_per_year,
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
                    moments=moments,
                )
            )

    return SweepSummary(
        strategy=strategy,
        symbols=tickers,
        runs=runs,
        skipped=skipped,
        empty_windows=empty_windows,
        periods_per_year=periods_per_year,
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

    ``periods_per_year`` names the year every IS and OOS figure is annualized in, for
    the same reason :class:`SweepSummary` carries it (KAN-840). It matters more here,
    not less: a sweep at least printed a deflation block whose observed Sharpe openly
    disagreed with the ranking table, whereas a walk-forward prints ``IS sharpe
    +1.45 -> OOS sharpe -1.08`` with nothing on screen to contradict it.
    """

    strategy: str
    symbols: list[str]
    mode: str
    rank_by: str
    folds: list[WalkForwardFold] = field(default_factory=list)
    skipped: list[tuple[ParamCombo, str]] = field(default_factory=list)
    unusable_folds: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The year every fold's annualized figures are expressed in; see SweepSummary.
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR

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
    costs: CostConfig | None = None,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
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

    ``periods_per_year`` is the annualization basis for every IS and OOS figure,
    exactly as in :func:`run_sweep`, and it must match the interval ``adapter`` was
    built at (KAN-840). The *selection* is invariant to it — one constant factor
    across the candidates cannot reorder them — so the winner a fold picks does not
    change; what changes is whether the Sharpes it reports mean anything.

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
    cost_config = costs if costs is not None else CostConfig()
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
                metrics, points, _moments = _run_combo(
                    strategy,
                    combo,
                    adapter,
                    tickers,
                    span.is_start,
                    span.is_end,
                    cash=cash,
                    risk=risk_config,
                    costs=cost_config,
                    periods_per_year=periods_per_year,
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
            oos_metrics, oos_points, _oos_moments = _run_combo(
                strategy,
                winner.combo,
                adapter,
                tickers,
                span.oos_start,
                span.oos_end,
                cash=cash,
                risk=risk_config,
                costs=cost_config,
                periods_per_year=periods_per_year,
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
        periods_per_year=periods_per_year,
    )
