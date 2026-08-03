"""Performance metrics computed from a backtest's equity curve and fills (V4).

Pure functions over the observable outputs of a run — the equity series and the
fill blotter — so they can be unit-tested on tiny hand-built fixtures without
touching the engine. The Sharpe basis is fixed by Q17: simple daily returns, a
zero risk-free rate, annualized with √252 (252 trading days per year).

Nothing here reads a future bar or reaches into engine internals; callers pass a
``Sequence[EquityPoint]`` (or a whole :class:`BacktestResult` to :func:`compute`)
and a fill blotter. The exposure helpers take a plain per-bar ``list[float]`` so
they're ready for the later exposure-wiring step without a change here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import sqrt
from typing import TYPE_CHECKING

from trading.types import Side

if TYPE_CHECKING:
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


def annualized_return(curve: Sequence[EquityPoint], periods_per_year: int = 252) -> float:
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
    periods_per_year: int = 252,
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
    periods_per_year: int = 252,
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


def calmar(curve: Sequence[EquityPoint], periods_per_year: int = 252) -> float:
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
    periods_per_year: int = 252,
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


def compute(result: BacktestResult, periods_per_year: int = 252) -> PerformanceMetrics:
    """Assemble :class:`PerformanceMetrics` from a run's curve and fills."""
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
    )
