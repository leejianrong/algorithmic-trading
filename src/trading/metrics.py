"""Performance metrics computed from a backtest's equity curve and fills (V4).

Pure functions over the observable outputs of a run — the equity series and the
fill blotter — so they can be unit-tested on tiny hand-built fixtures without
touching the engine. The Sharpe basis is fixed by Q17: simple per-bar returns, a
zero risk-free rate, annualized with √``periods_per_year``. That factor defaults
to ``252.0`` (252 trading days), so every daily caller is unchanged; an intraday
run passes its :attr:`~trading.frequency.Frequency.periods_per_year` so annualized
figures scale to the bar cadence (ADR-0022).

Nothing here reads a future bar or reaches into engine internals; callers pass a
``Sequence[EquityPoint]`` (or a whole :class:`BacktestResult` to :func:`compute`)
and a fill blotter. The exposure helpers take a plain per-bar ``list[float]`` so
they're ready for the later exposure-wiring step without a change here.

Benchmark-relative figures (ADR-0037) — beta, alpha, correlation, and the
information ratio — live here too, alongside return-per-unit-of-exposure. They
follow the same conventions as everything above: a zero risk-free rate (Q17),
sample (``n - 1``) moments, and ``periods_per_year`` as the single annualization
knob (ADR-0022). Every one of them is ``float | None``, never a stand-in ``0.0``:
a beta of zero and "there was no benchmark" must never be confusable, so no
:class:`BenchmarkComparison` exists at all when no benchmark ran, and an
individual statistic inside one is ``None`` when it is mathematically undefined.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import sqrt
from typing import TYPE_CHECKING

from trading.types import SHARE_EPS, Side

if TYPE_CHECKING:
    from datetime import datetime

    from trading.engine import BacktestResult, EquityPoint
    from trading.types import Fill


def daily_returns(curve: Sequence[EquityPoint]) -> list[float]:
    """Simple per-step returns of the equity series: ``e[i]/e[i-1] - 1``.

    Empty for a curve shorter than two points. A zero (or negative) prior
    equity would make the ratio undefined, so such a step yields ``0.0`` rather
    than a NaN/inf that would poison downstream stats.
    """
    returns: list[float] = []
    for prev, curr in pairwise(curve):
        if prev.equity <= 0:
            returns.append(0.0)
        else:
            returns.append(curr.equity / prev.equity - 1.0)
    return returns


def total_return(curve: Sequence[EquityPoint]) -> float:
    """Overall return over the curve: ``last/first - 1`` (0.0 if degenerate)."""
    if len(curve) < 2 or curve[0].equity <= 0:
        return 0.0
    return curve[-1].equity / curve[0].equity - 1.0


def annualized_return(curve: Sequence[EquityPoint], periods_per_year: float = 252.0) -> float:
    """Geometric annualized return.

    ``(1 + total) ** (periods_per_year / n) - 1`` where ``n`` is the number of
    return periods (one fewer than the number of points). Returns 0.0 when there
    are no return periods.
    """
    n = len(curve) - 1
    if n <= 0:
        return 0.0
    return float((1.0 + total_return(curve)) ** (periods_per_year / n)) - 1.0


def sharpe(
    curve: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    rf: float = 0.0,
) -> float:
    """Annualized Sharpe ratio of the daily returns (Q17).

    Mean excess daily return over its sample standard deviation, scaled by
    ``√periods_per_year``. Returns 0.0 when there are fewer than two returns or
    the standard deviation is zero, so no NaN/inf leaks out.
    """
    returns = daily_returns(curve)
    if len(returns) < 2:
        return 0.0
    excess = [r - rf for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    stdev = sqrt(variance)
    if stdev == 0.0:
        return 0.0
    return mean / stdev * sqrt(periods_per_year)


def sortino(
    curve: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    rf: float = 0.0,
) -> float:
    """Annualized Sortino ratio: mean excess return over *downside* deviation.

    Like :func:`sharpe` but the denominator penalizes only harmful volatility.
    Downside deviation is the sample standard deviation (``n - 1`` basis, matching
    :func:`sharpe`) computed over the shortfalls ``min(r - rf, 0)`` of every return
    in the series — upside steps contribute a zero, not a drop from the count.
    Returns 0.0 when there are fewer than two returns or there is no downside at
    all, so no NaN/inf leaks out (consistent with :func:`sharpe`'s zero-stdev rule).
    """
    returns = daily_returns(curve)
    if len(returns) < 2:
        return 0.0
    excess = [r - rf for r in returns]
    mean = sum(excess) / len(excess)
    downside_sq = sum(min(r, 0.0) ** 2 for r in excess)
    downside_dev = sqrt(downside_sq / (len(excess) - 1))
    if downside_dev == 0.0:
        return 0.0
    return mean / downside_dev * sqrt(periods_per_year)


def calmar(curve: Sequence[EquityPoint], periods_per_year: float = 252.0) -> float:
    """Calmar ratio: annualized return divided by max drawdown.

    A reward-per-unit-of-worst-pain figure. Returns 0.0 when the max drawdown is
    zero (a monotonic-up or degenerate curve), avoiding a divide-by-zero blow-up.
    """
    dd = max_drawdown(curve)
    if dd == 0.0:
        return 0.0
    return annualized_return(curve, periods_per_year) / dd


def turnover(
    fills: Sequence[tuple[object, Fill]],
    curve: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
) -> float:
    """Annualized portfolio turnover: traded notional over average equity.

    Sums the absolute notional (``qty * price``) of every fill, divides by the mean
    equity across the curve to express it as a fraction of the book, then annualizes
    by ``periods_per_year / n_bars`` so a half-year run and a full-year run of the
    same trading intensity report the same rate. Returns 0.0 for an empty curve or
    non-positive average equity.
    """
    n = len(curve)
    if n == 0:
        return 0.0
    avg_equity = sum(point.equity for point in curve) / n
    if avg_equity <= 0:
        return 0.0
    traded = sum(abs(fill.qty * fill.price) for _ts, fill in fills)
    return traded / avg_equity * (periods_per_year / n)


def max_drawdown(curve: Sequence[EquityPoint]) -> float:
    """Largest peak-to-trough decline as a positive fraction.

    Walks the curve tracking the running peak; the drawdown at each point is
    ``(peak - equity) / peak``. Returns the maximum such value — 0.0 for a
    monotonic-up (or empty) curve.
    """
    peak = float("-inf")
    worst = 0.0
    for point in curve:
        if point.equity > peak:
            peak = point.equity
        if peak > 0:
            drawdown = (peak - point.equity) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def win_rate(fills: Sequence[tuple[object, Fill]]) -> float:
    """Fraction of closing trades that were profitable.

    Reconstructs a running average cost per symbol from the fills in submission
    order, mirroring the portfolio's own accounting: a BUY blends the average
    cost over the enlarged position; a SELL is a *closing* trade that leaves the
    average cost unchanged. A SELL counts as a win when its fill price exceeds
    the symbol's average cost at that moment.

    Pairing assumption: every SELL closes (part of) the position built by the
    preceding BUYs in that symbol, valued at the running average cost — not
    FIFO/LIFO lot matching. ``win_rate = wins / closing_trades``, and 0.0 when
    there are no closing trades.

    ``fills`` is the blotter's ``list[tuple[datetime, Fill]]``; only the
    :class:`~trading.types.Fill` in each pair is read.
    """
    qty: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    wins = 0
    closes = 0
    for _ts, fill in fills:
        held = qty.get(fill.symbol, 0.0)
        cost = avg_cost.get(fill.symbol, 0.0)
        if fill.side is Side.BUY:
            new_qty = held + fill.qty
            if new_qty > 0:
                avg_cost[fill.symbol] = (held * cost + fill.qty * fill.price) / new_qty
            qty[fill.symbol] = new_qty
        else:
            closes += 1
            if fill.price > cost:
                wins += 1
            qty[fill.symbol] = held - fill.qty
    if closes == 0:
        return 0.0
    return wins / closes


def avg_exposure(exposures: list[float]) -> float:
    """Mean of a per-bar gross-exposure series (0.0 when empty)."""
    if not exposures:
        return 0.0
    return sum(exposures) / len(exposures)


def peak_exposure(exposures: list[float]) -> float:
    """Maximum of a per-bar gross-exposure series (0.0 when empty)."""
    if not exposures:
        return 0.0
    return max(exposures)


# Below this many trades per free parameter, a result is not evidence — it is an
# anecdote fitted to noise, and the report says so. The 30-50 range is the common
# practitioner floor for statistical significance in a parameter search; 30 is the
# lenient end of it (ADR-0029).
MIN_TRADES_PER_PARAMETER = 30.0


def entry_count(fills: Sequence[tuple[object, Fill]]) -> int:
    """Number of *position-opening* trades across the blotter.

    A round trip is two fills (a buy in, a sell out) and a rebalance can add
    several more, so counting raw fills would inflate the trade count several-fold
    — and trade count is the denominator of a statistical-significance claim, which
    makes over-counting the flattering direction. This counts only the fills that
    take a symbol from flat to held, reconstructing the running position per symbol
    from the blotter in submission order (the same reconstruction
    :func:`win_rate` does).

    Positions still open at the end of the run count: an entry is a decision the
    strategy made, whether or not it has been closed yet.
    """
    held: dict[str, float] = {}
    entries = 0
    for _ts, fill in fills:
        current = held.get(fill.symbol, 0.0)
        if fill.side is Side.BUY:
            if current <= SHARE_EPS:
                entries += 1
            held[fill.symbol] = current + fill.qty
        else:
            held[fill.symbol] = current - fill.qty
    return entries


def trades_per_parameter(
    fills: Sequence[tuple[object, Fill]],
    free_parameters: int | None,
) -> float | None:
    """Entries per tunable strategy parameter, or ``None`` when unknowable.

    The overfitting question this answers: a strategy with 4 knobs and 12 trades
    has not been validated, it has been decorated. Returns
    ``entry_count / free_parameters``.

    ``None`` when ``free_parameters`` is ``None`` (the caller did not say) — an
    honest absence, never a stand-in ``0.0`` that would read as a *failed* check
    rather than an *absent* one. A strategy with zero free parameters (e.g.
    ``buy_and_hold``) cannot be overfit by parameter search, so it reports
    ``None`` too rather than dividing by zero.
    """
    if free_parameters is None or free_parameters <= 0:
        return None
    return entry_count(fills) / free_parameters


# --- Exposure-adjusted and benchmark-relative figures (ADR-0037) -------------


def return_per_unit_exposure(
    curve: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
) -> float | None:
    """Annualized return divided by average gross exposure — ``None`` if never invested.

    The comparability fix. A strategy that averages 17% invested and one that
    averages 90% invested are not comparable on raw return: the first is mostly a
    pile of cash, and cash earns nothing here (there is no interest-on-cash model).
    Dividing the annualized return by :func:`avg_exposure` restates it as the
    return earned *per dollar actually at risk*, which is the number that ranks
    two such strategies on the same axis.

    Returns ``None`` — not ``0.0`` — when average exposure is zero or negative
    (a book that was never invested), because the ratio is undefined rather than
    bad. See ADR-0037 for the linearity caveat: this is a comparability lens, not
    a promise that levering the strategy to full investment would earn it.
    """
    average = avg_exposure([point.exposure for point in curve])
    if average <= 0.0:
        return None
    return annualized_return(curve, periods_per_year) / average


def align_curves(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
) -> list[tuple[datetime, float, float]]:
    """The two equity series restricted to the timestamps they actually share.

    Returns ``(ts, strategy_equity, benchmark_equity)`` triples in timestamp order,
    one per shared timestamp. This is the whole correctness story for every
    benchmark-relative figure below: positionally zipping two curves of different
    length — or with different gaps — would pair bar *i* of one against an
    unrelated bar *i* of the other and fabricate a correlation out of the offset.
    Curves are keyed by timestamp and intersected instead, so a benchmark that
    starts late, ends early, or is missing a day contributes only where it really
    lines up.

    Duplicate timestamps within a curve collapse to the last occurrence; engine
    curves have one point per bar, so this is a defensive nicety, not a case that
    arises.
    """
    own_by_ts = {point.ts: point.equity for point in curve}
    bench_by_ts = {point.ts: point.equity for point in benchmark}
    shared = sorted(own_by_ts.keys() & bench_by_ts.keys())
    return [(ts, own_by_ts[ts], bench_by_ts[ts]) for ts in shared]


def _step_returns(values: Sequence[float]) -> list[float]:
    """Simple step-to-step returns of a bare equity series (see :func:`daily_returns`)."""
    return [0.0 if prev <= 0 else curr / prev - 1.0 for prev, curr in pairwise(values)]


def aligned_returns(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
) -> tuple[list[float], list[float]]:
    """Paired per-step returns over the shared timestamps of the two curves.

    Both lists have the same length by construction. Returns are taken *after*
    alignment, between consecutive shared timestamps, so both sides always measure
    the same calendar span. Where the benchmark has a gap, the step that bridges it
    is longer than one bar on both sides — an honest pairing of the same interval,
    at the cost of a slightly uneven sampling grid (ADR-0037).
    """
    rows = align_curves(curve, benchmark)
    return _step_returns([row[1] for row in rows]), _step_returns([row[2] for row in rows])


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _sample_variance(values: Sequence[float], mean: float) -> float:
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def _sample_covariance(
    xs: Sequence[float], ys: Sequence[float], mean_x: float, mean_y: float
) -> float:
    products = ((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return sum(products) / (len(xs) - 1)


def _beta_of(strategy: Sequence[float], bench: Sequence[float]) -> float | None:
    if len(strategy) < 2:
        return None
    mean_b = _mean(bench)
    var_b = _sample_variance(bench, mean_b)
    if var_b == 0.0:
        return None
    return _sample_covariance(strategy, bench, _mean(strategy), mean_b) / var_b


def _correlation_of(strategy: Sequence[float], bench: Sequence[float]) -> float | None:
    if len(strategy) < 2:
        return None
    mean_s, mean_b = _mean(strategy), _mean(bench)
    sd_s = sqrt(_sample_variance(strategy, mean_s))
    sd_b = sqrt(_sample_variance(bench, mean_b))
    if sd_s == 0.0 or sd_b == 0.0:
        return None
    return _sample_covariance(strategy, bench, mean_s, mean_b) / (sd_s * sd_b)


def _alpha_of(
    strategy: Sequence[float],
    bench: Sequence[float],
    periods_per_year: float,
    rf: float,
) -> float | None:
    slope = _beta_of(strategy, bench)
    if slope is None:
        return None
    per_period = (_mean(strategy) - rf) - slope * (_mean(bench) - rf)
    return per_period * periods_per_year


def _information_ratio_of(
    strategy: Sequence[float],
    bench: Sequence[float],
    periods_per_year: float,
) -> float | None:
    if len(strategy) < 2:
        return None
    active = [s - b for s, b in zip(strategy, bench, strict=True)]
    mean_active = _mean(active)
    tracking_error = sqrt(_sample_variance(active, mean_active))
    if tracking_error == 0.0:
        return None
    return mean_active / tracking_error * sqrt(periods_per_year)


def beta(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
) -> float | None:
    """Sensitivity of the strategy's returns to the benchmark's.

    ``cov(r_s, r_b) / var(r_b)`` over the aligned returns, on the sample
    (``n - 1``) basis :func:`sharpe` and :func:`sortino` already use. ``None``
    when there are fewer than two aligned return periods, or when the benchmark
    has zero variance (a flat benchmark makes the slope undefined, not zero).
    """
    strategy, bench = aligned_returns(curve, benchmark)
    return _beta_of(strategy, bench)


def correlation(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
) -> float | None:
    """Pearson correlation of the aligned per-bar returns, or ``None`` if undefined.

    ``None`` when there are fewer than two aligned return periods or either side
    has zero variance.
    """
    strategy, bench = aligned_returns(curve, benchmark)
    return _correlation_of(strategy, bench)


def alpha(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    rf: float = 0.0,
) -> float | None:
    """Annualized Jensen's alpha: return not explained by benchmark exposure.

    Per-period ``mean(r_s - rf) - beta * mean(r_b - rf)``, scaled to a year by
    *multiplying* by ``periods_per_year``. That arithmetic scaling — rather than
    the geometric compounding :func:`annualized_return` uses — is the standard
    CAPM convention and the only one consistent with alpha being a mean-excess
    quantity; ADR-0037 records the choice. ``rf`` is zero by default, matching the
    Sharpe basis fixed by Q17. ``None`` whenever :func:`beta` is ``None``.
    """
    strategy, bench = aligned_returns(curve, benchmark)
    return _alpha_of(strategy, bench, periods_per_year, rf)


def information_ratio(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
) -> float | None:
    """Annualized active return over its tracking error.

    Mean of the active return ``r_s - r_b`` divided by that series' sample
    standard deviation (the tracking error), scaled by ``√periods_per_year`` —
    exactly :func:`sharpe`'s shape, with the benchmark in place of the risk-free
    rate. ``None`` with fewer than two aligned return periods, or when the
    tracking error is zero (a strategy that *is* the benchmark has no active
    return to reward).
    """
    strategy, bench = aligned_returns(curve, benchmark)
    return _information_ratio_of(strategy, bench, periods_per_year)


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """How a run related to its benchmark, on the bars the two actually shared.

    A relation between two runs, deliberately *not* a field of
    :class:`PerformanceMetrics`: it belongs beside the benchmark curve, not inside
    the strategy's own numbers. The object exists only when a benchmark ran, so a
    ``None`` where one of these is expected is the unambiguous "no benchmark"
    signal — never a zeroed-out comparison. Inside it, each statistic is
    independently ``None`` when it is mathematically undefined (too few shared
    bars, a zero-variance series), which is a *different* fact and reads
    differently in the report.

    ``shared_bars`` is the number of timestamps the two curves had in common. A
    caller that compares it against the strategy's own bar count can tell a full
    overlap from a partial one; the report prints that caveat.
    """

    shared_bars: int
    beta: float | None
    alpha: float | None
    correlation: float | None
    information_ratio: float | None


def compare_to_benchmark(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    rf: float = 0.0,
) -> BenchmarkComparison:
    """Assemble the whole benchmark-relative block, aligning the curves once.

    Equivalent to calling :func:`beta`, :func:`alpha`, :func:`correlation`, and
    :func:`information_ratio` individually, but it intersects the timestamps a
    single time. Always returns an object: the *caller* decides whether a
    benchmark existed at all.
    """
    rows = align_curves(curve, benchmark)
    strategy = _step_returns([row[1] for row in rows])
    bench = _step_returns([row[2] for row in rows])
    return BenchmarkComparison(
        shared_bars=len(rows),
        beta=_beta_of(strategy, bench),
        alpha=_alpha_of(strategy, bench, periods_per_year, rf),
        correlation=_correlation_of(strategy, bench),
        information_ratio=_information_ratio_of(strategy, bench, periods_per_year),
    )


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Headline performance figures for a run.

    ``avg_exposure`` / ``peak_exposure`` are the mean and maximum of the per-bar
    gross-exposure series carried on the equity curve (:attr:`EquityPoint.exposure`).
    """

    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    win_rate: float
    turnover: float
    avg_exposure: float
    peak_exposure: float
    # Sample-size honesty (ADR-0029). ``trade_count`` is position-opening entries,
    # not raw fills. ``trades_per_parameter`` is ``None`` when the caller did not
    # supply a free-parameter count, or when the strategy has none to overfit.
    # Both default so existing positional/keyword construction stays valid.
    trade_count: int = 0
    trades_per_parameter: float | None = None
    # The exposure-adjusted return (ADR-0037), defaulted so existing
    # positional/keyword construction stays valid. It needs no benchmark — it is
    # the comparability lens between a lightly-invested strategy and a
    # fully-invested one — and is ``None`` only when the book was never invested.
    # The *benchmark-relative* figures deliberately do not live here: they
    # describe a relation between two runs, not a property of this one
    # (:class:`BenchmarkComparison`).
    return_per_unit_exposure: float | None = None

    @property
    def underpowered(self) -> bool:
        """Whether the sample is too small to support a parameter search.

        ``True`` only when a ratio is known *and* falls below
        :data:`MIN_TRADES_PER_PARAMETER`. An unknown ratio is not a failure, so
        this stays ``False`` — the report distinguishes the two in its wording.
        """
        ratio = self.trades_per_parameter
        return ratio is not None and ratio < MIN_TRADES_PER_PARAMETER


def compute(
    result: BacktestResult,
    periods_per_year: float = 252.0,
    *,
    free_parameters: int | None = None,
) -> PerformanceMetrics:
    """Assemble :class:`PerformanceMetrics` from a run's curve and fills.

    ``free_parameters`` is the strategy's count of tunable constructor arguments
    (see :func:`trading.strategies.free_parameter_count`). Supplying it turns on
    the trades-per-parameter significance figure; omitting it leaves that figure
    ``None`` and changes nothing else, so every existing caller is unaffected.

    Benchmark-relative figures are *not* computed here — they need a second run,
    so :func:`compare_to_benchmark` takes both curves and returns its own value
    object (ADR-0037).
    """
    curve = result.equity_curve
    exposures = [point.exposure for point in curve]
    return PerformanceMetrics(
        total_return=total_return(curve),
        annualized_return=annualized_return(curve, periods_per_year),
        sharpe=sharpe(curve, periods_per_year),
        sortino=sortino(curve, periods_per_year),
        calmar=calmar(curve, periods_per_year),
        max_drawdown=max_drawdown(curve),
        win_rate=win_rate(result.fills),
        turnover=turnover(result.fills, curve, periods_per_year),
        avg_exposure=avg_exposure(exposures),
        peak_exposure=peak_exposure(exposures),
        trade_count=entry_count(result.fills),
        trades_per_parameter=trades_per_parameter(result.fills, free_parameters),
        return_per_unit_exposure=return_per_unit_exposure(curve, periods_per_year),
    )
