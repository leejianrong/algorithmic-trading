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

Statistical significance (ADR-0039) sits at the bottom of the module: a stationary
block bootstrap confidence interval on the Sharpe ratio, the *paired* "beats the
benchmark in X% of resamples" figure, and the deflated Sharpe that discounts a
sweep winner for the number of combinations that competed for the title. All of
it is deterministic — every function takes an explicit integer ``seed`` and drives
its own :class:`random.Random`; nothing here ever touches the global RNG, so the
same run always yields the same interval.

Regime-split metrics (ADR-0066) sit just above the significance block:
:func:`compute_regime_report` classifies every bar of a run's *own* equity curve
into a volatility regime (high/low, split at the run's own trailing-vol median)
and, independently, a trend regime (trending/mean-reverting, split at the run's
own trailing efficiency-ratio median), then recomputes the *same*
:class:`PerformanceMetrics` restricted to each label — so "Sharpe 1.4 overall"
can be read alongside "Sharpe 2.1 in low-vol bars, -0.3 in high-vol ones" rather
than averaging a 21-year run's dot-com bust, GFC, and bull market into one number.
Purely additive reporting: it does not change what :func:`compute` returns, and a
regime with too few classified bars for :data:`MIN_BOOTSTRAP_OBSERVATIONS` to mean
anything is still computed (never hidden) but flagged, exactly as ADR-0029 flags a
thin trades-per-parameter ratio rather than suppressing it.

The turnover/cost-budget check (ADR-0068, KAN-860) sits right after the
trades-per-parameter block, the same honesty-check neighbourhood: given a stated
annual cost budget (a fraction of equity, e.g. 0.01 for 1%) and the
:class:`~trading.config.CostConfig` the run actually traded under,
:func:`assess_cost_budget` restates the arithmetic CLAUDE.md's cost-model ADRs
already use informally — cost drag equals annual turnover times the one-way rate —
as a computed, always-reported figure rather than something an operator does by
hand. It is a warning, never a guardrail: like ADR-0029's trades-per-parameter
check, it never vetoes an order or aborts a run.

Monte Carlo path shuffling (ADR-0067) sits at the very end: :func:`monte_carlo_shuffle`
draws thousands of random *permutations* of a run's own per-bar returns — every
observed return used exactly once, just reordered, never resampled with replacement
like the ADR-0039 bootstrap above — and asks whether the run's real, path-ordered max
drawdown is typical of that reshuffled population or an outlier of it. It complements
ADR-0039 rather than duplicating it: the bootstrap answers "how uncertain is this
Sharpe estimate", while shuffling answers "did the ORDER these returns happened in
matter". Mean and sample variance do not depend on order, so the annualized Sharpe
computed from a permutation is mathematically identical to the observed one — it is
reported once, for a direct side-by-side against :class:`SharpeInterval`, never as a
resampled "distribution" that would just be the same number with floating-point noise
in the last bit. Max drawdown has no such invariance: it is a genuine property of the
path, which is the entire reason this feature exists.
"""

from __future__ import annotations

import random
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from math import e, sqrt
from statistics import NormalDist, median
from typing import TYPE_CHECKING

from trading.config import CostConfig
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


def _sharpe_of(
    returns: Sequence[float],
    periods_per_year: float,
    rf: float = 0.0,
) -> float:
    """Annualized Sharpe of an already-extracted return series.

    The arithmetic body of :func:`sharpe`, factored out so the bootstrap
    (ADR-0039) can score a *resampled* return series with exactly the same
    definition rather than a lookalike. Keeping one implementation is the whole
    point: a confidence interval computed on a subtly different Sharpe would be an
    interval around a number the report never prints.
    """
    if len(returns) < 2:
        return 0.0
    excess = [r - rf for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    stdev = sqrt(variance)
    if stdev == 0.0:
        return 0.0
    return mean / stdev * sqrt(periods_per_year)


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
    return _sharpe_of(daily_returns(curve), periods_per_year, rf)


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


# --- Turnover / cost-budget check (ADR-0068, KAN-860) ------------------------


def effective_cost_rate_bps(
    fills: Sequence[tuple[object, Fill]],
    costs: CostConfig,
) -> float | None:
    """Notional-weighted average one-way cost rate a run's own fills actually paid.

    ``slippage_bps`` (or, per fill, its :class:`~trading.config.CostConfig.
    symbol_slippage_bps` override — ADR-0063) plus ``taker_fee_bps``, weighted by
    each fill's own notional (``abs(qty * price)``). This is the *modelled* rate,
    reconstructed from the same ``CostConfig`` the broker priced every fill at — a
    backtest fill's slippage is not something to estimate after the fact, it is
    exactly the config's rate for that symbol, deterministically (unlike a live
    fill, where the realized cost is an independent observation; see
    :mod:`trading.divergence` for that question).

    Blending by notional (rather than using the flat ``slippage_bps`` alone) is
    what makes this honest under ``--liquidity-tier-adv`` (ADR-0063): a
    mixed-liquidity universe trades some symbols at a lower tiered rate and some at
    the default, and a single static number would misstate whichever direction the
    universe actually skewed.

    ``None`` when there is no traded notional to weight — a run that entered
    nothing has no rate to speak of, not a rate of zero.
    """
    traded = sum(abs(fill.qty * fill.price) for _ts, fill in fills)
    if traded <= 0.0:
        return None
    tiers = costs.symbol_slippage_bps
    weighted = 0.0
    for _ts, fill in fills:
        notional = abs(fill.qty * fill.price)
        rate = costs.slippage_bps
        if tiers is not None and fill.symbol in tiers:
            rate = tiers[fill.symbol]
        weighted += notional * (rate + costs.taker_fee_bps)
    return weighted / traded


@dataclass(frozen=True, slots=True)
class CostBudgetReport:
    """Whether a run's own turnover fits inside a stated annual cost budget.

    The arithmetic this bench's cost-model ADRs (0060/0061/0063) already state
    informally every time they quote a "predicted drag" — measured directly against
    a real crypto run at 1454% annual turnover and a 25 bps one-way rate, which
    predicted 3.6% of equity lost to cost and measured 4.0 percentage points — is
    ``cost_drag = turnover * one_way_rate``. This makes it a computed, always-
    reported figure (KAN-860) rather than something an operator works out by hand
    before deciding a strategy's turnover is reasonable for its asset class.

    ``effective_rate_bps`` is this run's own notional-weighted rate
    (:func:`effective_cost_rate_bps`) — the blend of ``slippage_bps``/
    ``symbol_slippage_bps``/``taker_fee_bps`` the run's fills actually priced at,
    never a single flat assumption plugged in from outside the run. ``None`` only
    when the run traded nothing, in which case every other field below describes
    "no turnover, no cost, no constraint to violate" rather than an undefined ratio
    (mirroring :func:`trades_per_parameter`'s "unknowable, not failing" convention).

    ``implied_max_turnover`` is the annual turnover ``cost_budget_pct`` allows at
    this rate — ``cost_budget_pct / (effective_rate_bps / 10_000)`` — the number in
    the units CLAUDE.md's cost-model corollary already states it in ("Alpaca crypto
    at 22-25 bps allows ~400% turnover"). ``None`` when the effective rate is
    non-positive: a commission-free, unslipped run has no rate this budget could
    ever be exceeded by, so there is no ceiling to name.

    ``predicted_drag_pct`` is ``turnover * effective_rate_bps / 10_000`` — the same
    arithmetic restated directly as a fraction of equity, which is what
    :attr:`exceeds_budget` actually compares against ``cost_budget_pct``
    (mathematically equivalent to comparing turnover against
    ``implied_max_turnover``, but well-defined even when the rate is zero).
    """

    cost_budget_pct: float
    turnover: float
    effective_rate_bps: float | None
    implied_max_turnover: float | None
    predicted_drag_pct: float | None
    notes: list[str] = field(default_factory=list)

    @property
    def exceeds_budget(self) -> bool:
        """Whether this run's own turnover would cost more than the stated budget.

        ``False`` — never a fabricated violation — when ``predicted_drag_pct`` is
        unknown (the run traded nothing): an absent measurement is not a failing
        one, the same distinction :class:`PerformanceMetrics.underpowered` draws
        for an absent trades-per-parameter ratio (ADR-0029).
        """
        return (
            self.predicted_drag_pct is not None and self.predicted_drag_pct > self.cost_budget_pct
        )


def assess_cost_budget(
    result: BacktestResult,
    costs: CostConfig,
    cost_budget_pct: float,
    periods_per_year: float = 252.0,
) -> CostBudgetReport:
    """Check a run's own turnover against a stated annual cost-drag budget (KAN-860).

    ``costs`` should be the exact :class:`~trading.config.CostConfig` the run's
    broker traded under — including any ``symbol_slippage_bps`` tiering — so
    :func:`effective_cost_rate_bps` reconstructs the rate this run actually paid,
    not a rate assumed from outside it.

    Always returns a report (never ``None``): a run that traded nothing still has a
    well-defined "no turnover, no cost" answer, reported via
    :attr:`CostBudgetReport.notes` rather than an absent object a caller must
    special-case.

    Raises ``ValueError`` for a non-positive ``cost_budget_pct`` — a caller mistake
    (a budget of zero or less admits no turnover at any positive rate, which is not
    a meaningful check to run), not a property of the data.
    """
    if cost_budget_pct <= 0.0:
        raise ValueError(f"cost_budget_pct must be positive, got {cost_budget_pct}")
    annual_turnover = turnover(result.fills, result.equity_curve, periods_per_year)
    rate_bps = effective_cost_rate_bps(result.fills, costs)
    if rate_bps is None:
        return CostBudgetReport(
            cost_budget_pct=cost_budget_pct,
            turnover=annual_turnover,
            effective_rate_bps=None,
            implied_max_turnover=None,
            predicted_drag_pct=None,
            notes=["no cost-budget check: the run traded nothing, so there is no rate to assess"],
        )
    notes: list[str] = []
    implied_max: float | None = None
    if rate_bps > 0.0:
        implied_max = cost_budget_pct / (rate_bps / 10_000.0)
    else:
        notes.append(
            "effective cost rate is 0 bps (no slippage or fee modelled) — turnover "
            "can never exceed a positive budget at a zero rate, so no ceiling is stated"
        )
    return CostBudgetReport(
        cost_budget_pct=cost_budget_pct,
        turnover=annual_turnover,
        effective_rate_bps=rate_bps,
        implied_max_turnover=implied_max,
        predicted_drag_pct=annual_turnover * (rate_bps / 10_000.0),
        notes=notes,
    )


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


# --- Diversified baseline comparison (ADR-0071, KAN-641) ---------------------


@dataclass(frozen=True, slots=True)
class DiversifiedBaselineReport:
    """A naive multi-asset equal-weight run, compared against the strategy.

    ``--benchmark`` (ADR-0037) answers "did this beat one unconstrained
    buy-and-hold symbol"; this answers a harder, more honest question — "did this
    beat doing nothing clever at all, just holding a diversified basket at equal
    weight". CLAUDE.md's own headline finding is that the dumbest strategy on
    ``core10`` beat SPY on return, Sharpe, *and* drawdown, which is
    diversification, not alpha — so a strategy that cannot clear this bar is not
    earning its complexity.

    Self-contained rather than a view over fields already in ``result.json``
    (unlike :class:`BenchmarkComparison`, which is joined against the separate
    top-level ``benchmark_curve``): there is exactly one baseline run behind this
    report, so its own :class:`PerformanceMetrics` — return, Sharpe, drawdown —
    travel alongside the relative :class:`BenchmarkComparison` rather than
    requiring a second lookup.

    ``label`` names the strategy/basket pair for display (e.g.
    ``"equal_weight/core10"``); ``symbols`` is the basket actually traded
    (:attr:`~trading.engine.BacktestResult.symbols`). ``notes`` carries the same
    "never invested" / "invested late" honesty check ADR-0037 already applies to
    ``--benchmark`` (:func:`assess_diversified_baseline`), so a baseline that
    could not fund its own entry cannot print a flattering ``+0.00%``
    unchallenged either.
    """

    label: str
    symbols: tuple[str, ...]
    metrics: PerformanceMetrics
    comparison: BenchmarkComparison
    notes: list[str] = field(default_factory=list)


def _diversified_baseline_notes(
    baseline: BacktestResult, baseline_metrics: PerformanceMetrics
) -> list[str]:
    """The ADR-0037 deployment honesty check, restated as plain-text notes.

    Mirrors ``trading.report._benchmark_deployment_lines`` (same two conditions:
    zero peak exposure, or a first exposed bar later than the first fillable one),
    but returns plain sentences with no CLI-formatting markers — this feeds a
    JSON ``notes`` list as well as the terminal, the same convention
    :class:`RegimeReport`/:class:`SignificanceReport` already use.
    """
    if baseline_metrics.peak_exposure <= 0.0:
        note = (
            "the diversified baseline never took a position — its return is idle "
            "cash, not a market return, and every comparison figure describes a "
            "flat line"
        )
        if baseline.rejections:
            order, reason = baseline.rejections[0]
            note += (
                f" ({len(baseline.rejections)} order(s) rejected; first: "
                f"{order.symbol} {order.side.value} {order.qty:g} — {reason})"
            )
        return [note]
    entered = next(
        (i for i, point in enumerate(baseline.equity_curve) if point.exposure > 0.0), None
    )
    if entered is not None and entered > 1:
        note = (
            f"the diversified baseline held nothing until bar {entered + 1} of "
            f"{len(baseline.equity_curve)} — its return understates the basket "
            "held over the full span"
        )
        if baseline.rejections:
            order, reason = baseline.rejections[0]
            note += (
                f" ({len(baseline.rejections)} order(s) rejected; first: "
                f"{order.symbol} {order.side.value} {order.qty:g} — {reason})"
            )
        return [note]
    return []


def assess_diversified_baseline(
    result: BacktestResult,
    baseline: BacktestResult,
    periods_per_year: float = 252.0,
    *,
    label: str,
) -> DiversifiedBaselineReport:
    """Assemble the diversified-baseline block for one finished baseline run.

    ``baseline`` is a completed run of a naive multi-asset strategy (the CLI's
    default is ``equal_weight`` over ``core10``) under the same cash/costs/dates
    as ``result``, run with unconstrained guardrails exactly as ``--benchmark``
    is (ADR-0037's reasoning applies unchanged: the comparison must not be
    clamped, but a baseline exempt from the venue's fees would flatter itself by
    the fees the strategy paid and it did not).

    Always returns an object; the caller decides whether to render it — the same
    contract :func:`compare_to_benchmark`/:func:`assess_cost_budget` already
    keep.
    """
    baseline_metrics = compute(baseline, periods_per_year)
    comparison = compare_to_benchmark(result.equity_curve, baseline.equity_curve, periods_per_year)
    return DiversifiedBaselineReport(
        label=label,
        symbols=tuple(baseline.symbols),
        metrics=baseline_metrics,
        comparison=comparison,
        notes=_diversified_baseline_notes(baseline, baseline_metrics),
    )


# --- Statistical significance of a Sharpe ratio (ADR-0039) -------------------

# How many resamples a confidence interval is built from. 1,000 is the usual
# floor for a 95% percentile interval: each tail is placed from ~25 draws, which
# is enough to locate it but not enough to quote a third decimal. It is a
# parameter on every entry point so a fast test can drop it and a serious study
# can raise it.
DEFAULT_BOOTSTRAP_RESAMPLES = 1_000

# Block length in bars. Resampling *individual* returns would destroy the serial
# structure a momentum or trend edge lives in and hand back a flatteringly narrow
# interval; ~60 daily bars (a quarter) keeps runs of correlated returns intact.
DEFAULT_BLOCK_LENGTH = 60

# Two-sided coverage of the reported interval.
DEFAULT_CONFIDENCE = 0.95

# The seed every entry point defaults to. Fixed and public rather than drawn from
# a clock: two runs of the same command must produce the same interval, and a test
# that only passes on a re-run is a race, not luck.
DEFAULT_BOOTSTRAP_SEED = 20260808

# Below this many return periods there is nothing to bootstrap. A block bootstrap
# needs enough observations to hold several whole blocks; under ~30 the resampled
# series are near-copies of each other and the resulting interval is not merely
# wide, it is meaningless. The functions return ``None`` rather than a garbage
# interval (ADR-0039).
MIN_BOOTSTRAP_OBSERVATIONS = 30

# A resample must be able to draw at least this many blocks, or it is a rotation
# of the original series rather than a resample of it. This is what caps the block
# length on a short run: a 40-bar series cannot use 60-bar blocks.
MIN_BLOCKS_PER_RESAMPLE = 4

# Euler-Mascheroni, used by the expected-maximum-Sharpe approximation below.
EULER_MASCHERONI = 0.5772156649015329

# The probability a deflated Sharpe must clear before the winner of a search reads
# as a finding rather than the best of N coin flips. Same spirit — and the same
# "judgement call, not a law" caveat — as :data:`MIN_TRADES_PER_PARAMETER`.
DEFLATED_SHARPE_CONFIDENCE = 0.95


def effective_block_length(observations: int, requested: int) -> int:
    """The block length a series of ``observations`` returns can actually support.

    ``requested``, capped so a resample still draws at least
    :data:`MIN_BLOCKS_PER_RESAMPLE` blocks, and never below 1. A 40-bar run that
    asks for 60-bar blocks gets 10, not 60: with blocks as long as the series every
    resample is a near-rotation of the original, every resampled Sharpe comes back
    nearly identical, and the interval collapses into a confident lie. Reducing the
    block length costs some autocorrelation fidelity, and the caller *says so*
    rather than hiding it — the reduction is recorded on the returned value object
    and in the summary's notes.

    Raises ``ValueError`` for a non-positive ``requested`` length: that is a
    programming error, not a property of the data.
    """
    if requested < 1:
        raise ValueError(f"block_length must be >= 1, got {requested}")
    cap = max(1, observations // MIN_BLOCKS_PER_RESAMPLE)
    return max(1, min(requested, cap))


def _stationary_indices(n: int, block_length: int, rng: random.Random) -> list[int]:
    """One resample's worth of indices from the stationary bootstrap.

    Politis & Romano's stationary bootstrap: start at a uniformly random index and
    walk forward, restarting at a fresh uniform index with probability
    ``1 / block_length`` at each step and wrapping around the end of the series.
    Block lengths are therefore geometric with mean ``block_length``, which is what
    makes the resampled series *stationary* — unlike fixed-length blocks, whose
    join points sit at deterministic positions. ``block_length == 1`` degenerates
    to the plain i.i.d. bootstrap.

    Returns exactly ``n`` indices, so a resample has the same length as the
    original series and its Sharpe is on the same footing as the observed one.
    """
    restart_probability = 1.0 / block_length
    indices: list[int] = []
    position = rng.randrange(n)
    for _ in range(n):
        indices.append(position)
        starts_new_block = rng.random() < restart_probability
        position = rng.randrange(n) if starts_new_block else (position + 1) % n
    return indices


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    """Linearly-interpolated percentile of an already-sorted series."""
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty series")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def _validate_bootstrap_args(resamples: int, confidence: float) -> None:
    """Reject caller mistakes loudly; data shortfalls are handled separately."""
    if resamples < 1:
        raise ValueError(f"resamples must be >= 1, got {resamples}")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence}")


@dataclass(frozen=True, slots=True)
class SharpeInterval:
    """A bootstrap confidence interval around a run's annualized Sharpe (ADR-0039).

    ``point`` is the Sharpe the report already prints — the observed one, not the
    bootstrap mean, so the interval brackets the number on the page rather than a
    neighbour of it. ``low``/``high`` are percentiles of the resampled Sharpes.

    Every knob that could change the answer is carried on the object, because a
    confidence interval whose provenance is invisible is a number nobody can check:
    ``block_length`` is the *effective* length actually used (see
    :func:`effective_block_length`), and ``seed`` is the exact integer that
    reproduces this interval.
    """

    point: float
    low: float
    high: float
    confidence: float
    resamples: int
    block_length: int
    requested_block_length: int
    observations: int
    seed: int

    @property
    def width(self) -> float:
        """How wide the interval is, in annualized Sharpe units."""
        return self.high - self.low

    @property
    def straddles_zero(self) -> bool:
        """Whether the data cannot rule out that the strategy has no edge at all.

        ``True`` when the interval contains zero — the single most important thing
        this module can say. A Sharpe of 0.42 with an interval of ``[-0.09, 0.84]``
        is not a measurement of skill; it is a measurement of how little the sample
        settles.
        """
        return self.low <= 0.0 <= self.high

    @property
    def block_length_was_reduced(self) -> bool:
        """Whether the series was too short for the block length that was asked for."""
        return self.block_length < self.requested_block_length


def sharpe_confidence_interval(
    curve: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    block_length: int = DEFAULT_BLOCK_LENGTH,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> SharpeInterval | None:
    """Percentile confidence interval on the annualized Sharpe, by block bootstrap.

    Resamples the run's per-bar returns ``resamples`` times with the stationary
    block bootstrap (:func:`_stationary_indices`), scores each resample with
    :func:`_sharpe_of`, and takes the two symmetric percentiles bracketing
    ``confidence`` of them. Blocks — not individual returns — are the unit of
    resampling, so serial correlation survives; shuffling single returns would
    destroy exactly the structure a trend or momentum edge consists of and report
    an interval far too narrow.

    Deterministic: the RNG is a local :class:`random.Random` seeded with ``seed``,
    never the module-global one, so the same curve and seed always yield the same
    interval.

    ``None`` — never a fabricated interval — when the series is shorter than
    :data:`MIN_BOOTSTRAP_OBSERVATIONS` returns, or has no variance at all (a flat
    curve has a Sharpe of 0.0 by convention, not a distribution). Raises
    ``ValueError`` for a nonsensical ``resamples``/``confidence``/``block_length``,
    which is a caller bug rather than a data shortfall.
    """
    _validate_bootstrap_args(resamples, confidence)
    returns = daily_returns(curve)
    observations = len(returns)
    if observations < MIN_BOOTSTRAP_OBSERVATIONS:
        return None
    if len(set(returns)) == 1:
        # A perfectly flat series: every resample is the same series, so the
        # "interval" would be a zero-width [0, 0] that reads as a measurement.
        return None
    effective = effective_block_length(observations, block_length)
    rng = random.Random(seed)
    scores = sorted(
        _sharpe_of(
            [returns[i] for i in _stationary_indices(observations, effective, rng)],
            periods_per_year,
        )
        for _ in range(resamples)
    )
    tail = (1.0 - confidence) / 2.0
    return SharpeInterval(
        point=_sharpe_of(returns, periods_per_year),
        low=_percentile(scores, tail),
        high=_percentile(scores, 1.0 - tail),
        confidence=confidence,
        resamples=resamples,
        block_length=effective,
        requested_block_length=block_length,
        observations=observations,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class PairedBootstrap:
    """How often the strategy beat its benchmark across *paired* resamples.

    The powerful test, and the easy one to get wrong. Both series are resampled on
    **one shared set of block indices**, so every resample is a coherent
    alternative history in which the same stretches of market happened to both.
    That pairing is what cancels the common market factor and leaves the difference
    in skill; resampling the two independently would compare the strategy in one
    imaginary market against the benchmark in a different one and report a number
    that is confidently wrong.

    ``win_rate`` is the fraction of resamples on which the strategy's Sharpe
    exceeded the benchmark's. ``observed_edge`` is that same difference measured
    once on the real, unresampled data — the thing the win rate expresses
    confidence about.
    """

    win_rate: float
    observed_edge: float
    resamples: int
    block_length: int
    requested_block_length: int
    observations: int
    seed: int

    @property
    def block_length_was_reduced(self) -> bool:
        """Whether the shared span was too short for the requested block length."""
        return self.block_length < self.requested_block_length


def paired_bootstrap(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    block_length: int = DEFAULT_BLOCK_LENGTH,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> PairedBootstrap | None:
    """ "Beats the benchmark in X% of resamples", resampled in lockstep.

    Aligns the two equity curves by timestamp with :func:`aligned_returns` (never
    positionally — ADR-0037), draws **one** index sequence per resample, and
    applies that same sequence to both return series before scoring each with
    :func:`_sharpe_of`. The win rate is the fraction of resamples on which the
    strategy's Sharpe came out higher.

    Deterministic on ``seed`` for the same reason
    :func:`sharpe_confidence_interval` is. ``None`` when the two curves share fewer
    than :data:`MIN_BOOTSTRAP_OBSERVATIONS` return periods — a win rate over a
    handful of shared bars is noise wearing a percentage sign.
    """
    _validate_bootstrap_args(resamples, DEFAULT_CONFIDENCE)
    strategy, bench = aligned_returns(curve, benchmark)
    observations = len(strategy)
    if observations < MIN_BOOTSTRAP_OBSERVATIONS:
        return None
    effective = effective_block_length(observations, block_length)
    rng = random.Random(seed)
    wins = 0
    for _ in range(resamples):
        # ONE index sequence, applied to BOTH series. This single shared list is
        # the entire correctness story of the paired test.
        indices = _stationary_indices(observations, effective, rng)
        strategy_sharpe = _sharpe_of([strategy[i] for i in indices], periods_per_year)
        bench_sharpe = _sharpe_of([bench[i] for i in indices], periods_per_year)
        if strategy_sharpe > bench_sharpe:
            wins += 1
    return PairedBootstrap(
        win_rate=wins / resamples,
        observed_edge=_sharpe_of(strategy, periods_per_year) - _sharpe_of(bench, periods_per_year),
        resamples=resamples,
        block_length=effective,
        requested_block_length=block_length,
        observations=observations,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class ReturnMoments:
    """The first four moments of a return series, computed in one pass.

    Enough to deflate a Sharpe ratio without carrying the whole return series
    around — a sweep keeps one of these per run, not thousands of floats.
    ``mean``/``stdev`` are the sample (``n - 1``) figures :func:`sharpe` already
    uses, so ``mean / stdev`` is exactly the per-bar Sharpe the report prints
    divided by ``√periods_per_year``. ``skew``/``kurtosis`` are the standard
    population moment ratios, and ``kurtosis`` is **not** excess: a normal series
    scores 3.0.
    """

    count: int
    mean: float
    stdev: float
    skew: float
    kurtosis: float


def return_moments(returns: Sequence[float]) -> ReturnMoments | None:
    """Moments of a return series, or ``None`` when they are undefined.

    ``None`` with fewer than two returns, or when the series has no dispersion at
    all (the skew and kurtosis ratios divide by the second moment).
    """
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    deviations = [r - mean for r in returns]
    sum_squares = sum(d * d for d in deviations)
    m2 = sum_squares / n
    if m2 <= 0.0:
        return None
    return ReturnMoments(
        count=n,
        mean=mean,
        stdev=sqrt(sum_squares / (n - 1)),
        skew=sum(d**3 for d in deviations) / n / m2**1.5,
        kurtosis=sum(d**4 for d in deviations) / n / (m2 * m2),
    )


def curve_moments(curve: Sequence[EquityPoint]) -> ReturnMoments | None:
    """:func:`return_moments` of an equity curve's per-bar returns."""
    return return_moments(daily_returns(curve))


def expected_max_sharpe(trials: int, sharpe_stdev: float) -> float:
    """Per-bar Sharpe the *best* of ``trials`` skill-free strategies would still show.

    The null this ticket exists to state: run 24 parameter combinations over the
    same data and the best of them has a positive Sharpe even when not one has any
    edge, purely because you kept the maximum of 24 draws. Bailey & López de
    Prado's expected-maximum approximation puts a number on it —

    ``sigma * [(1 - g) * inv_cdf(1 - 1/N) + g * inv_cdf(1 - 1/(N*e))]``

    where ``sigma`` (``sharpe_stdev``) is the spread of per-bar Sharpes *across the
    trials* and ``g`` is Euler-Mascheroni. The spread matters as much as the count:
    24 near-identical combinations offer far less opportunity to get lucky than 24
    genuinely different ones, and this formula says so.

    Returns 0.0 for a single trial (nothing was selected, so nothing needs
    deflating) or a zero spread (every trial scored the same, so the maximum was
    not a lucky draw). Both are honest zeros, not fallbacks.
    """
    if trials <= 1 or sharpe_stdev <= 0.0:
        return 0.0
    normal = NormalDist()
    return sharpe_stdev * (
        (1.0 - EULER_MASCHERONI) * normal.inv_cdf(1.0 - 1.0 / trials)
        + EULER_MASCHERONI * normal.inv_cdf(1.0 - 1.0 / (trials * e))
    )


def probabilistic_sharpe_ratio(
    moments: ReturnMoments,
    threshold_per_bar: float = 0.0,
) -> float | None:
    """Probability the *true* per-bar Sharpe exceeds ``threshold_per_bar``.

    Bailey & López de Prado's PSR: the observed Sharpe's sampling distribution,
    corrected for the return series' skew and kurtosis — because a Sharpe estimated
    from negatively-skewed, fat-tailed returns is less trustworthy than the same
    Sharpe from clean ones, and a strategy that sells tail risk manufactures
    exactly those returns.

    ``None`` when the variance correction is non-positive (an extreme combination
    of a very high Sharpe and heavy tails makes the standard error imaginary), or
    with fewer than two observations. Never a clipped 0.0/1.0 stand-in.
    """
    if moments.count < 2 or moments.stdev <= 0.0:
        return None
    observed = moments.mean / moments.stdev
    variance_correction = (
        1.0 - moments.skew * observed + (moments.kurtosis - 1.0) / 4.0 * observed * observed
    )
    if variance_correction <= 0.0:
        return None
    z = (observed - threshold_per_bar) * sqrt(moments.count - 1) / sqrt(variance_correction)
    return NormalDist().cdf(z)


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    """A Sharpe ratio discounted for the number of trials that competed for it.

    ``trials`` is what the tool could see — one run for a plain backtest, the
    number of scored combinations for a sweep. ``null_best_sharpe`` is the
    annualized Sharpe the luckiest of those trials would show with no edge at all,
    and ``probability`` is ``P(true Sharpe > null_best_sharpe)``. A winner whose
    probability sits at 0.31 is not a finding; it is the best of N coin flips.

    ``probability`` is ``float | None`` — ``None`` means "undefined on this data",
    a different fact from a low probability, and must never render the same way
    (the ADR-0029/ADR-0037 rule).
    """

    trials: int
    observed_sharpe: float
    null_best_sharpe: float
    probability: float | None
    observations: int
    trial_sharpe_stdev: float | None
    skew: float
    kurtosis: float

    @property
    def significant(self) -> bool:
        """Whether the discounted Sharpe clears :data:`DEFLATED_SHARPE_CONFIDENCE`.

        ``False`` when the probability is unknown: an unmeasurable result is not a
        passing one. The report words the two cases differently.
        """
        return self.probability is not None and self.probability >= DEFLATED_SHARPE_CONFIDENCE


def deflated_sharpe(
    moments: ReturnMoments,
    trial_sharpes: Sequence[float],
    periods_per_year: float = 252.0,
    *,
    prior_trials: int = 0,
) -> DeflatedSharpe | None:
    """Deflate a run's Sharpe for the search that produced it (KAN-619, ADR-0039).

    ``trial_sharpes`` are the **annualized** Sharpes of every trial that competed —
    for a sweep, one per scored combination, *including* the winner. Their count is
    the multiple-comparison correction's ``N`` and their spread is its sigma (see
    :func:`expected_max_sharpe`). ``moments`` describes the winner's own return
    series, which carries the sample size and the non-normality correction.

    A single trial is still a trial: a ``trial_sharpes`` of length one yields a
    null threshold of 0.0, so the result degenerates to the plain probabilistic
    Sharpe against zero rather than silently skipping the check. What the tool
    *cannot* see is every run the operator made in a previous invocation, so the
    correction is always a lower bound — :func:`assess_significance` says so in its
    notes.

    ``prior_trials`` (ADR-0062, KAN-858) widens ``N`` by trials this call cannot see
    directly — the cumulative count a :class:`~trading.ledger.TrialLedger` reports
    for every earlier logged invocation. It is added to ``len(trial_sharpes)``
    *only* for the count that reaches :func:`expected_max_sharpe`; the *spread*
    (``sharpe_stdev``, computed below as ``stdev_per_bar``) is still estimated from
    ``trial_sharpes`` alone, because the ledger records how many trials ran, never
    their individual Sharpes — carrying those forever would make the ledger grow
    without bound and would still be silently wrong the day an old experiment's
    file was deleted. So a ledger-widened correction inherits *this* invocation's
    spread as a stand-in for the historical trials' unknown one — a real
    approximation, not a free upgrade, and :func:`trial_count_note` says so. One
    consequence worth naming: when this invocation is itself a single trial
    (``len(trial_sharpes) == 1``), ``stdev_per_bar`` is ``None`` regardless of how
    large ``prior_trials`` is, so :func:`expected_max_sharpe` still returns 0.0 —
    a ledger of single backtests can grow the visible count without ever supplying
    a spread to price it with. ``0`` (the default) reproduces the pre-ledger
    behaviour exactly: :attr:`DeflatedSharpe.trials` is ``len(trial_sharpes)``, as
    before.

    ``None`` when the moments carry no dispersion. Raises ``ValueError`` on an
    empty ``trial_sharpes`` (a result produced by no trials at all is a caller bug,
    not a data property) or a negative ``prior_trials``.
    """
    trials = len(trial_sharpes)
    if trials < 1:
        raise ValueError("trial_sharpes must hold at least the run being deflated")
    if prior_trials < 0:
        raise ValueError(f"prior_trials must be >= 0, got {prior_trials}")
    if moments.stdev <= 0.0:
        return None
    root = sqrt(periods_per_year)
    per_bar = [s / root for s in trial_sharpes]
    stdev_per_bar: float | None = None
    if trials > 1:
        stdev_per_bar = sqrt(_sample_variance(per_bar, _mean(per_bar)))
    augmented_trials = trials + prior_trials
    threshold = expected_max_sharpe(
        augmented_trials, stdev_per_bar if stdev_per_bar is not None else 0.0
    )
    return DeflatedSharpe(
        trials=augmented_trials,
        observed_sharpe=moments.mean / moments.stdev * root,
        null_best_sharpe=threshold * root,
        probability=probabilistic_sharpe_ratio(moments, threshold),
        observations=moments.count,
        trial_sharpe_stdev=None if stdev_per_bar is None else stdev_per_bar * root,
        skew=moments.skew,
        kurtosis=moments.kurtosis,
    )


def trial_count_note(trials: int, *, prior_trials: int = 0) -> str:
    """The caveat that must accompany every deflated Sharpe (ADR-0039 §4).

    Public and shared rather than inlined where it is first needed, because
    :func:`assess_significance` is not the only caller: ``trading sweep`` deflates
    its winner straight off :meth:`~trading.sweep.SweepSummary.deflated_winner`
    (it kept the trials' moments, not their curves, so there is nothing to
    bootstrap) and must print the *same* sentence. ADR-0039 calls the caveat "not
    optional and not conditional", and two copies of a sentence like that drift.

    ``trials`` is the **augmented** total — :attr:`DeflatedSharpe.trials`, already
    including any ``prior_trials`` — so this reads the same count the deflation was
    actually scored against. When ``prior_trials`` is 0 (the default, and every
    caller before ADR-0062) the wording is exactly what it always was: byte-for-byte
    identical, because a lone invocation with no ledger has nothing new to disclose.
    A positive ``prior_trials`` splits the sentence so a reader can see both halves
    of the count — how many trials this run itself made, and how many were carried
    over from a :class:`~trading.ledger.TrialLedger` — and restates that the spread
    behind the correction is still this invocation's alone (see
    :func:`deflated_sharpe`), so the widened count is a LOWER BOUND twice over: once
    on trials the ledger predates, and once on the spread of the trials it does see.
    """
    if prior_trials <= 0:
        return (
            f"the deflation counts {trials} trial(s) — only those visible in this "
            "invocation. Runs made in earlier invocations, over other date ranges, or on "
            "other strategies are invisible to this tool, so the correction is a LOWER "
            "BOUND on the multiple-comparison problem, never a complete accounting"
        )
    this_invocation = trials - prior_trials
    return (
        f"the deflation counts {trials} trial(s): {this_invocation} from this run plus "
        f"{prior_trials} carried over from earlier logged experiment(s) in the ledger — "
        "the spread behind the correction is still estimated from this invocation's "
        "trials only (the ledger records counts, not each trial's own Sharpe), so this "
        "remains a LOWER BOUND twice over: on the trial count made before the ledger "
        "existed, and on the spread of the trials it does carry forward"
    )


@dataclass(frozen=True, slots=True)
class SignificanceReport:
    """Everything ADR-0039 can say about whether a run's Sharpe means anything.

    Each block is independently ``None`` when it could not be computed, and
    ``notes`` explains *why*, in words a reader can act on — a wide interval and an
    absent one are different facts, exactly as ADR-0029 distinguished an unknown
    trades-per-parameter ratio from a failing one.
    """

    sharpe_interval: SharpeInterval | None = None
    paired: PairedBootstrap | None = None
    deflated: DeflatedSharpe | None = None
    notes: list[str] = field(default_factory=list)


def assess_significance(
    curve: Sequence[EquityPoint],
    benchmark: Sequence[EquityPoint] | None = None,
    periods_per_year: float = 252.0,
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    block_length: int = DEFAULT_BLOCK_LENGTH,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    trial_sharpes: Sequence[float] | None = None,
    prior_trials: int = 0,
) -> SignificanceReport:
    """Assemble the whole significance block for one run (ADR-0039).

    Always returns an object; the *caller* decides whether to render it, exactly as
    :func:`compare_to_benchmark` does. ``benchmark`` turns on the paired win rate.
    ``trial_sharpes`` is the annualized Sharpe of every trial that competed for this
    result — omit it and the run counts as its own single trial, which is the
    truthful reading of a lone ``trading backtest`` invocation.

    ``prior_trials`` (ADR-0062) is the cumulative trial count a
    :class:`~trading.ledger.TrialLedger` reports for every *earlier* logged
    invocation; it widens :func:`deflated_sharpe`'s ``N`` and is folded into the
    printed note via :func:`trial_count_note`. ``0`` (the default) is exactly
    today's behaviour.

    The notes are not decoration. They record the things a reader would otherwise
    have to infer: that a short series forced a smaller block length, that no
    benchmark meant no paired figure, and that the trial count covers only this
    invocation (plus whatever a ledger contributed).
    """
    notes: list[str] = []
    interval = sharpe_confidence_interval(
        curve,
        periods_per_year,
        resamples=resamples,
        block_length=block_length,
        confidence=confidence,
        seed=seed,
    )
    if interval is None:
        notes.append(
            f"no Sharpe confidence interval: {max(len(curve) - 1, 0)} return period(s) is "
            f"below the {MIN_BOOTSTRAP_OBSERVATIONS} a block bootstrap needs, or the curve "
            "has no variance at all"
        )
    elif interval.block_length_was_reduced:
        notes.append(
            f"block length reduced from {interval.requested_block_length} to "
            f"{interval.block_length} bars: {interval.observations} return period(s) cannot "
            f"hold {MIN_BLOCKS_PER_RESAMPLE} blocks of the requested length, and blocks as "
            "long as the series would resample nothing"
        )

    paired: PairedBootstrap | None = None
    if benchmark is None:
        notes.append(
            "no benchmark ran, so there is no paired win rate — the figure that says "
            "whether this strategy beats the alternative, not merely whether it beats zero"
        )
    else:
        paired = paired_bootstrap(
            curve,
            benchmark,
            periods_per_year,
            resamples=resamples,
            block_length=block_length,
            seed=seed,
        )
        if paired is None:
            notes.append(
                "no paired win rate: the strategy and benchmark curves share fewer than "
                f"{MIN_BOOTSTRAP_OBSERVATIONS} return periods"
            )

    deflated: DeflatedSharpe | None = None
    moments = curve_moments(curve)
    if moments is None:
        notes.append(
            "no deflated Sharpe: the return series has fewer than two periods, or no variance"
        )
    else:
        sharpes = (
            [moments.mean / moments.stdev * sqrt(periods_per_year)]
            if trial_sharpes is None
            else list(trial_sharpes)
        )
        deflated = deflated_sharpe(moments, sharpes, periods_per_year, prior_trials=prior_trials)
        notes.append(trial_count_note(len(sharpes) + prior_trials, prior_trials=prior_trials))
    return SignificanceReport(
        sharpe_interval=interval,
        paired=paired,
        deflated=deflated,
        notes=notes,
    )


# --- Regime-split metrics (ADR-0066) ------------------------------------------

# Trailing bars the classifier looks back over to score a bar's volatility and
# trend strength: 20 is about one trading month of daily bars — long enough that
# a single outlier return does not flip the label, short enough that the labels
# still track a multi-year run's actual regime changes rather than smoothing them
# all away. Fixed rather than derived from ``periods_per_year`` (a bar count, not
# a calendar duration) so intraday runs get the same 20-*bar* lookback a daily
# run does; ADR-0066 records that as a deliberate, revisitable choice.
REGIME_WINDOW = 20


def _equity_path(returns: Sequence[float], start: float) -> list[float]:
    """Equity levels implied by compounding ``returns`` back-to-back from ``start``.

    Used only to get a dollar-scaled equity series for a regime slice's max
    drawdown and turnover denominator (traded notional is in real dollars, so the
    denominator must be too) — never exposed as a public curve, because a regime
    slice is a *discontiguous* set of bars and this path does not correspond to
    any actual sequence of calendar dates.
    """
    path = [start]
    equity = start
    for r in returns:
        equity *= 1.0 + r
        path.append(equity)
    return path


def _total_return_of(returns: Sequence[float]) -> float:
    """:func:`total_return`'s arithmetic over a bare return sequence.

    A regime slice is a *discontiguous* subsequence of a run's bars — "every bar
    the run spent in a high-vol regime", not a contiguous span — so there is no
    real :class:`~trading.engine.EquityPoint` curve to hand :func:`total_return`.
    This compounds the regime's own per-bar returns back-to-back instead, which is
    the only sense in which "the regime's total return" is defined: what the
    strategy would have earned had it experienced only those bars, in order.
    """
    if not returns:
        return 0.0
    product = 1.0
    for r in returns:
        product *= 1.0 + r
    return product - 1.0


def _annualized_return_of(returns: Sequence[float], periods_per_year: float) -> float:
    """:func:`annualized_return`'s arithmetic over a bare return sequence."""
    n = len(returns)
    if n <= 0:
        return 0.0
    return float((1.0 + _total_return_of(returns)) ** (periods_per_year / n)) - 1.0


def _max_drawdown_of(returns: Sequence[float], start: float = 1.0) -> float:
    """:func:`max_drawdown`'s arithmetic over a bare return sequence.

    Rebuilds the equity path implied by compounding ``returns`` from ``start``
    (the run's actual starting equity, so the fraction reported matches what
    :func:`max_drawdown` would have shown on a curve that only ever saw these
    bars) and walks it exactly as :func:`max_drawdown` does.
    """
    peak = float("-inf")
    worst = 0.0
    for value in _equity_path(returns, start):
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (peak - value) / peak
            if drawdown > worst:
                worst = drawdown
    return worst


def _sortino_of(returns: Sequence[float], periods_per_year: float, rf: float = 0.0) -> float:
    """:func:`sortino`'s arithmetic over a bare return sequence."""
    if len(returns) < 2:
        return 0.0
    excess = [r - rf for r in returns]
    mean = sum(excess) / len(excess)
    downside_sq = sum(min(r, 0.0) ** 2 for r in excess)
    downside_dev = sqrt(downside_sq / (len(excess) - 1))
    if downside_dev == 0.0:
        return 0.0
    return mean / downside_dev * sqrt(periods_per_year)


def _calmar_of(returns: Sequence[float], periods_per_year: float, start: float = 1.0) -> float:
    """:func:`calmar`'s arithmetic over a bare return sequence."""
    dd = _max_drawdown_of(returns, start)
    if dd == 0.0:
        return 0.0
    return _annualized_return_of(returns, periods_per_year) / dd


def _regime_trade_stats(
    fills: Sequence[tuple[object, Fill]],
    regime_ts: frozenset[datetime],
) -> tuple[int, int, int, float]:
    """``(wins, closes, entries, traded_notional)`` restricted to bars in ``regime_ts``.

    Walks *every* fill in submission order — never just the ones whose bar falls
    in this regime — to keep the running per-symbol quantity and average cost
    correct, mirroring :func:`win_rate` and :func:`entry_count`. A regime slice
    must not pretend the position history outside it never happened: a SELL that
    closes a position opened in a different regime still needs that position's
    true average cost, and a BUY on a warmup or other-regime bar still changes
    what "flat" means for the next entry. Only fills whose own timestamp is in
    ``regime_ts`` are *tallied* into the returned counters; every fill still
    updates the running state.

    ``fills`` is the blotter's ``list[tuple[datetime, Fill]]``, exactly as
    :func:`win_rate`/:func:`entry_count` take it.
    """
    qty: dict[str, float] = {}
    avg_cost: dict[str, float] = {}
    wins = closes = entries = 0
    traded = 0.0
    for ts, fill in fills:
        held = qty.get(fill.symbol, 0.0)
        cost = avg_cost.get(fill.symbol, 0.0)
        in_regime = ts in regime_ts
        if fill.side is Side.BUY:
            if in_regime:
                if held <= SHARE_EPS:
                    entries += 1
                traded += abs(fill.qty * fill.price)
            new_qty = held + fill.qty
            if new_qty > 0:
                avg_cost[fill.symbol] = (held * cost + fill.qty * fill.price) / new_qty
            qty[fill.symbol] = new_qty
        else:
            if in_regime:
                closes += 1
                if fill.price > cost:
                    wins += 1
                traded += abs(fill.qty * fill.price)
            qty[fill.symbol] = held - fill.qty
    return wins, closes, entries, traded


@dataclass(frozen=True, slots=True)
class RegimeMetrics:
    """:class:`PerformanceMetrics` restricted to the bars sharing one regime label.

    ``bar_count`` is the number of *return periods* (not raw bars — one fewer than
    the classified span, same convention :func:`daily_returns` uses) classified
    into this regime; it is the denominator ADR-0066's small-sample warning reads.
    """

    label: str
    bar_count: int
    metrics: PerformanceMetrics

    @property
    def underpowered(self) -> bool:
        """Whether this regime has too few return periods for its Sharpe to mean
        anything.

        Reuses :data:`MIN_BOOTSTRAP_OBSERVATIONS` rather than a fresh threshold —
        a regime Sharpe computed from 8 bars is exactly as unreliable as a
        whole-run Sharpe would be from 8 bars, and ADR-0039 already set that floor
        for "a block bootstrap needs enough observations to mean something". A
        regime this thin gets its :class:`PerformanceMetrics` computed and printed
        (never hidden — the reader decides), but flagged.
        """
        return self.bar_count < MIN_BOOTSTRAP_OBSERVATIONS


@dataclass(frozen=True, slots=True)
class RegimeReport:
    """Two independent regime splits of one run's performance (ADR-0066).

    The volatility axis (``high_vol``/``low_vol``) and the trend axis
    (``trending``/``mean_reverting``) are reported *separately*, not crossed into
    a four-way combination. Crossing them would quarter an already-scarce bar
    count a second time — the whole point of this report is surfacing "only works
    in one regime" on samples that are often already thin, and a chi-squared
    slicing of few thousand bars into four buckets defeats that faster than it
    illuminates anything.

    ``window`` is :data:`REGIME_WINDOW` (or whatever was requested).
    ``vol_threshold``/``trend_threshold`` are the run's own median trailing
    volatility / trailing efficiency ratio — the split points — so a reader can
    see exactly what "high" and "low" meant for *this* run rather than trusting an
    unstated absolute cutoff. Both are ``None``, and every regime slot is
    ``None``, only when the curve has fewer return periods than ``window`` (too
    short to classify a single bar); ``notes`` explains why.
    """

    window: int
    vol_threshold: float | None
    trend_threshold: float | None
    high_vol: RegimeMetrics | None
    low_vol: RegimeMetrics | None
    trending: RegimeMetrics | None
    mean_reverting: RegimeMetrics | None
    notes: list[str] = field(default_factory=list)


def compute_regime_report(
    result: BacktestResult,
    periods_per_year: float = 252.0,
    *,
    window: int = REGIME_WINDOW,
    free_parameters: int | None = None,
) -> RegimeReport:
    """Split ``result``'s :class:`PerformanceMetrics` by two regime axes (ADR-0066).

    The classifier runs over the run's *own* equity-curve returns
    (:func:`daily_returns`) — never a benchmark's, and never anything the
    strategy could not itself have observed causally: each bar's label is a
    function of the trailing ``window`` bars up to and including it, so it never
    reads a bar that had not yet closed.

    Two independent trailing statistics, each over the same ``window``-bar
    lookback:

    - **Volatility.** The sample standard deviation of the trailing ``window``
      returns, annualized by ``sqrt(periods_per_year)`` — exactly :func:`sharpe`'s
      annualization, applied to a rolling window instead of the whole series.
      Bars are labeled ``"high_vol"`` when that trailing figure is at or above the
      run's own median trailing volatility, ``"low_vol"`` otherwise.
    - **Trend.** The Kaufman-style efficiency ratio over the same window:
      ``abs(sum(window returns)) / sum(abs(window returns))``, in ``[0, 1]``. Near
      1 means the window's moves mostly ran the same direction (net displacement
      close to the sum of the moves); near 0 means they largely cancelled (a
      choppy, mean-reverting stretch). Bars are labeled ``"trending"`` when at or
      above the run's own median trailing efficiency ratio, ``"mean_reverting"``
      otherwise.

    Splitting at the run's *own* median (rather than a fixed absolute cutoff, e.g.
    "20% annualized vol") is deliberate: there is no universal threshold that
    means the same thing across a 5-minute crypto run and a 21-year daily equity
    one, so each run supplies its own scale and every classification is relative
    to what that particular run actually experienced. ``vol_threshold``/
    ``trend_threshold`` on the returned :class:`RegimeReport` record exactly what
    that scale was.

    The first ``window - 1`` return periods have no full trailing window and are
    warmup: unclassified on *both* axes, contributing to neither regime's metrics
    (not even a `None`-labeled bucket — they are simply excluded, the same
    "not enough data to say anything" silence :func:`sharpe_confidence_interval`
    keeps rather than fabricating an estimate).

    Each of the four :class:`RegimeMetrics` restricts :func:`PerformanceMetrics`'s
    return-based figures (total/annualized return, Sharpe, Sortino, Calmar, max
    drawdown, exposure) to that regime's return periods, and its trade-based
    figures (win rate, turnover, entry count) to fills whose own bar falls in that
    regime — reconstructing running position/cost state from the *entire* fill
    history for correctness (see :func:`_regime_trade_stats`), so a SELL that
    closes a position opened in a different regime is still priced against its
    true average cost. ``free_parameters`` behaves exactly as it does for
    :func:`compute`: omitted, ``trades_per_parameter`` is ``None`` on every slice.

    A regime with fewer than :data:`MIN_BOOTSTRAP_OBSERVATIONS` return periods
    still gets a fully computed :class:`PerformanceMetrics` (never a suppressed or
    ``None`` one) but :attr:`RegimeMetrics.underpowered` is ``True`` and a note
    names it — the same "compute it, then say the sample is too thin to trust"
    rule ADR-0029 applies to trades-per-parameter and ADR-0039 applies to the
    Sharpe bootstrap.

    Returns a :class:`RegimeReport` with every field ``None`` (but a note
    explaining why) when the curve has fewer than ``window`` return periods —
    too short to classify even one bar.
    """
    curve = result.equity_curve
    returns = daily_returns(curve)
    n = len(returns)
    if n < window:
        return RegimeReport(
            window=window,
            vol_threshold=None,
            trend_threshold=None,
            high_vol=None,
            low_vol=None,
            trending=None,
            mean_reverting=None,
            notes=[
                f"no regime classification: {n} return period(s) is fewer than the "
                f"{window}-bar trailing window every classification needs"
            ],
        )

    vol_series: list[float | None] = [None] * n
    trend_series: list[float | None] = [None] * n
    for i in range(window - 1, n):
        block = returns[i - window + 1 : i + 1]
        mean_block = sum(block) / window
        variance_block = sum((r - mean_block) ** 2 for r in block) / (window - 1)
        vol_series[i] = sqrt(variance_block) * sqrt(periods_per_year)
        gross = sum(abs(r) for r in block)
        trend_series[i] = abs(sum(block)) / gross if gross > 0 else 0.0

    vol_threshold = median(v for v in vol_series if v is not None)
    trend_threshold = median(t for t in trend_series if t is not None)

    vol_labels: list[str | None] = [
        None if v is None else ("high_vol" if v >= vol_threshold else "low_vol") for v in vol_series
    ]
    trend_labels: list[str | None] = [
        None if t is None else ("trending" if t >= trend_threshold else "mean_reverting")
        for t in trend_series
    ]

    def build(labels: list[str | None], target: str) -> RegimeMetrics:
        idx = [i for i, label in enumerate(labels) if label == target]
        regime_returns = [returns[i] for i in idx]
        regime_ts = frozenset(curve[i + 1].ts for i in idx)
        regime_exposures = [curve[i + 1].exposure for i in idx]
        start_equity = curve[0].equity
        wins, closes, entries, traded = _regime_trade_stats(result.fills, regime_ts)
        bar_count = len(idx)
        avg_equity = _mean(_equity_path(regime_returns, start_equity))
        avg_exp = avg_exposure(regime_exposures)
        ann_return = _annualized_return_of(regime_returns, periods_per_year)
        metrics = PerformanceMetrics(
            total_return=_total_return_of(regime_returns),
            annualized_return=ann_return,
            sharpe=_sharpe_of(regime_returns, periods_per_year),
            sortino=_sortino_of(regime_returns, periods_per_year),
            calmar=_calmar_of(regime_returns, periods_per_year, start_equity),
            max_drawdown=_max_drawdown_of(regime_returns, start_equity),
            win_rate=(wins / closes) if closes else 0.0,
            turnover=(
                traded / avg_equity * (periods_per_year / bar_count)
                if bar_count > 0 and avg_equity > 0
                else 0.0
            ),
            avg_exposure=avg_exp,
            peak_exposure=peak_exposure(regime_exposures),
            trade_count=entries,
            trades_per_parameter=(
                entries / free_parameters
                if free_parameters is not None and free_parameters > 0
                else None
            ),
            return_per_unit_exposure=(ann_return / avg_exp) if avg_exp > 0.0 else None,
        )
        return RegimeMetrics(label=target, bar_count=bar_count, metrics=metrics)

    high_vol = build(vol_labels, "high_vol")
    low_vol = build(vol_labels, "low_vol")
    trending = build(trend_labels, "trending")
    mean_reverting = build(trend_labels, "mean_reverting")

    notes = [
        f"{regime.label}: {regime.bar_count} return period(s) is below "
        f"{MIN_BOOTSTRAP_OBSERVATIONS} — its Sharpe/Sortino/Calmar are computed and printed "
        "but should not be read as a measurement (ADR-0066, echoing ADR-0029/ADR-0039)"
        for regime in (high_vol, low_vol, trending, mean_reverting)
        if regime.underpowered
    ]

    return RegimeReport(
        window=window,
        vol_threshold=vol_threshold,
        trend_threshold=trend_threshold,
        high_vol=high_vol,
        low_vol=low_vol,
        trending=trending,
        mean_reverting=mean_reverting,
        notes=notes,
    )


# --- Monte Carlo path shuffling (ADR-0067) ------------------------------------
#
# ADR-0039's bootstrap resamples *with replacement* (each drawn index can repeat,
# some observations never appear) to ask how uncertain a point estimate is. This
# is a different experiment: every one of the SAME observed returns is used
# exactly once per resample, just reordered — a random permutation, never a
# resample — so it asks a question the bootstrap cannot: did the actual SEQUENCE
# these returns arrived in matter, or would any other ordering of the identical
# multiset of returns have told the same story. Max drawdown is the headline
# answer, because it is a path-dependent statistic the Sharpe (mean/stdev of an
# unordered multiset) structurally cannot see reordered at all.


def _shuffled_copy(returns: Sequence[float], rng: random.Random) -> list[float]:
    """One uniformly random permutation of ``returns``.

    Every element of ``returns`` appears in the result exactly once — a reorder,
    never a resample-with-replacement (contrast :func:`_stationary_indices`,
    which draws indices *with* replacement for the block bootstrap and can skip
    or repeat an observation). ``rng.shuffle`` is Fisher-Yates, uniform over all
    ``n!`` orderings, driven entirely by the caller's local ``rng`` — never the
    module-global one.
    """
    shuffled = list(returns)
    rng.shuffle(shuffled)
    return shuffled


def _empirical_percentile(sorted_values: Sequence[float], value: float) -> float:
    """The fraction of an already-sorted empirical distribution at or below ``value``.

    ``value``'s own percentile rank within ``sorted_values`` — 0.0 if it is below
    every entry, 1.0 if it is at or above every entry. Used to place the run's real
    path-ordered max drawdown against the population of shuffled ones: a rank near
    1.0 says the real path was worse than almost every reordering, a rank near 0.0
    says it was better than almost every reordering. ``0.5`` (undefined-but-neutral)
    for an empty distribution, which cannot arise from :func:`monte_carlo_shuffle`
    (it never resamples zero times) but keeps this helper total.
    """
    if not sorted_values:
        return 0.5
    return bisect_right(sorted_values, value) / len(sorted_values)


@dataclass(frozen=True, slots=True)
class MonteCarloShuffleReport:
    """Whether a run's max drawdown depended on the ORDER its returns arrived in
    (ADR-0067, KAN-859).

    Complements :class:`SharpeInterval` rather than duplicating it: that interval
    answers "how uncertain is this Sharpe estimate" by resampling *with*
    replacement; this answers "did this run get an unusually bad — or unusually
    fortunate — CLUSTERING of its own losses" by reshuffling the exact same
    returns into a new order every time, never adding or dropping one.

    ``sharpe`` is the run's own observed annualized Sharpe (:func:`_sharpe_of` on
    the unshuffled returns), printed here for a direct side-by-side against
    :class:`SharpeInterval` — **not** a resampled distribution. Mean and sample
    variance are invariant to the order their inputs are summed in (``sum``/``len``
    do not know or care about order), so scoring the Sharpe on a shuffled sequence
    would reproduce this same value up to floating-point summation-order noise at
    the level of the last one or two bits — a "distribution" that is really one
    number with rounding jitter dressed up as evidence. Reporting only the single
    observed value, once, is the honest rendering of an invariant quantity; see
    ``tests/unit/test_monte_carlo.py`` for the measured floating-point tolerance.

    Every ``shuffled_*``/``actual_*`` field is ``None`` — with ``notes`` saying
    why — only when the curve is shorter than :data:`MIN_BOOTSTRAP_OBSERVATIONS`
    return periods: shuffling a handful of returns has too few distinct orderings
    to say anything about path dependence, the same "too short, computed as an
    honest absence rather than a fabricated number" rule ADR-0039 already applies
    to the Sharpe interval.

    ``shuffled_low``/``shuffled_median``/``shuffled_high`` are percentiles (at
    ``confidence``, the same two-sided convention :class:`SharpeInterval` uses) of
    the max drawdown across ``resamples`` random reorderings of the SAME return
    series. ``actual_max_drawdown`` is the run's own real, path-ordered max
    drawdown (never reordered) and ``actual_percentile`` is where it ranks inside
    that shuffled population, in ``[0.0, 1.0]`` — the single figure that answers
    the card's question directly: 0.97 means the real path was worse than 97% of
    random reorderings of its own returns (an unusually bad clustering of losses,
    or a structural vulnerability); 0.03 means it was better than 97% of them (an
    unusually fortunate sequence a live deployment should not expect to repeat).

    Unlike :class:`SharpeInterval`, there is no ``block_length`` here at all — a
    permutation has no block-size knob; it is not parameterized by anything
    between "keep every return exactly once" and "reorder them", so there is
    nothing to reduce on a short series the way the bootstrap's block length is.
    """

    resamples: int
    seed: int
    confidence: float
    observations: int
    sharpe: float | None
    actual_max_drawdown: float | None
    shuffled_low: float | None
    shuffled_median: float | None
    shuffled_high: float | None
    actual_percentile: float | None
    notes: list[str] = field(default_factory=list)

    @property
    def worse_than_shuffled(self) -> bool | None:
        """Whether the real path's drawdown exceeds nearly every random reordering.

        ``None`` — not ``False`` — when there was nothing to compare (too short).
        """
        if self.actual_max_drawdown is None or self.shuffled_high is None:
            return None
        return self.actual_max_drawdown > self.shuffled_high

    @property
    def better_than_shuffled(self) -> bool | None:
        """Whether the real path's drawdown beats nearly every random reordering.

        ``None`` — not ``False`` — when there was nothing to compare (too short).
        """
        if self.actual_max_drawdown is None or self.shuffled_low is None:
            return None
        return self.actual_max_drawdown < self.shuffled_low


def monte_carlo_shuffle(
    curve: Sequence[EquityPoint],
    periods_per_year: float = 252.0,
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> MonteCarloShuffleReport:
    """Reshuffle a run's own per-bar returns ``resamples`` times (ADR-0067).

    Each resample is an exact random permutation of :func:`daily_returns` —
    :func:`_shuffled_copy`, backed by ``rng.shuffle`` — never a bootstrap
    resample-with-replacement. Every one of the observed returns appears in every
    resample exactly once; only their order changes. :func:`_max_drawdown_of` is
    then scored on each shuffled sequence, and the run's own real (unshuffled)
    max drawdown is placed against that empirical distribution via
    :func:`_empirical_percentile`.

    Deterministic on ``seed``, exactly like :func:`sharpe_confidence_interval`: a
    local :class:`random.Random` is constructed and driven, the module-global RNG
    is never touched, and the same curve/seed/resamples always reproduce the same
    report.

    Always returns a :class:`MonteCarloShuffleReport` — never ``None`` — mirroring
    :class:`RegimeReport`'s convention rather than :class:`SharpeInterval`'s bare
    ``None``: below :data:`MIN_BOOTSTRAP_OBSERVATIONS` return periods every
    ``shuffled_*``/``actual_*`` field is ``None`` and ``notes`` explains why, so a
    caller never has to special-case "no report" versus "an empty one".

    Raises ``ValueError`` for a nonsensical ``resamples``/``confidence`` — a
    caller mistake, not a property of the data (:func:`_validate_bootstrap_args`,
    shared with the bootstrap).
    """
    _validate_bootstrap_args(resamples, confidence)
    returns = daily_returns(curve)
    observations = len(returns)
    if observations < MIN_BOOTSTRAP_OBSERVATIONS:
        return MonteCarloShuffleReport(
            resamples=resamples,
            seed=seed,
            confidence=confidence,
            observations=observations,
            sharpe=None,
            actual_max_drawdown=None,
            shuffled_low=None,
            shuffled_median=None,
            shuffled_high=None,
            actual_percentile=None,
            notes=[
                f"no Monte Carlo shuffle: {observations} return period(s) is below the "
                f"{MIN_BOOTSTRAP_OBSERVATIONS} a meaningful reshuffle needs — a handful of "
                "returns has too few distinct orderings to say anything about path "
                "dependence"
            ],
        )
    rng = random.Random(seed)
    drawdowns = sorted(_max_drawdown_of(_shuffled_copy(returns, rng)) for _ in range(resamples))
    tail = (1.0 - confidence) / 2.0
    actual_dd = _max_drawdown_of(returns)
    return MonteCarloShuffleReport(
        resamples=resamples,
        seed=seed,
        confidence=confidence,
        observations=observations,
        sharpe=_sharpe_of(returns, periods_per_year),
        actual_max_drawdown=actual_dd,
        shuffled_low=_percentile(drawdowns, tail),
        shuffled_median=_percentile(drawdowns, 0.5),
        shuffled_high=_percentile(drawdowns, 1.0 - tail),
        actual_percentile=_empirical_percentile(drawdowns, actual_dd),
        notes=[],
    )
