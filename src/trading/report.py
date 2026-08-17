"""V4 reporting: a full metrics summary, an equity-curve CSV, and an optional PNG.

``summarize`` renders the headline performance block — total & annualized return,
Sharpe, Sortino, Calmar, max drawdown, average/peak exposure, win rate, and
turnover — alongside the V3 guardrail lines (rejected/clamped orders, halt) and,
when the caller supplies the strategy's free-parameter count, the
trades-per-parameter sample-size check with a warning when it is too thin to be
evidence (ADR-0029).
When a benchmark run is supplied it also renders the benchmark-relative block —
beta, annualized alpha, correlation, information ratio, and the exposure-adjusted
return of both sides (ADR-0037). Without a benchmark not one of those lines
appears and the summary is byte-identical to what it has always been.
When the caller supplies a :class:`~trading.metrics.SignificanceReport` it also
renders the ADR-0039 block — the bootstrap confidence interval on Sharpe, the
paired beats-the-benchmark win rate, and the trial-count deflation — and again,
omitting it leaves the summary byte-identical.
When the caller supplies a :class:`~trading.metrics.RegimeReport` it also renders
the ADR-0066 regime-split block — the same headline figures restricted to the
run's own high/low-volatility and trending/mean-reverting bars, alongside (never
instead of) the whole-run numbers — and again, omitting it leaves the summary
byte-identical.
When the caller supplies a :class:`~trading.metrics.MonteCarloShuffleReport` it
also renders the ADR-0067 path-shuffle block — the run's own max drawdown placed
against the distribution of max drawdowns from thousands of random reorderings of
the same per-bar returns, alongside the run's Sharpe (unchanged by any reordering,
so printed once rather than as a resampled distribution) — and again, omitting it
leaves the summary byte-identical.
``write_equity_csv`` writes one
row per trading day with an ``exposure`` column and, when a benchmark run is
supplied, a ``benchmark_equity`` column aligned by timestamp. ``write_equity_png``
plots the curve; matplotlib is an optional dependency imported lazily inside the
function so importing this module never requires it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading.calendar import US_EQUITY, get_calendar
from trading.frequency import Frequency
from trading.metrics import (
    DEFLATED_SHARPE_CONFIDENCE,
    MIN_BOOTSTRAP_OBSERVATIONS,
    MIN_TRADES_PER_PARAMETER,
    compare_to_benchmark,
    compute,
)

if TYPE_CHECKING:
    from trading.engine import BacktestResult, EquityPoint
    from trading.metrics import (
        BenchmarkComparison,
        DeflatedSharpe,
        MonteCarloShuffleReport,
        PairedBootstrap,
        PerformanceMetrics,
        RegimeMetrics,
        RegimeReport,
        SharpeInterval,
        SignificanceReport,
    )


# Schema version of the canonical machine-readable run artifact emitted by
# ``result_to_dict`` / ``write_result_json``. The web dashboard reads this to
# decide how to parse the document; bump it whenever the shape changes in a way
# a consumer must notice. Purely *additive* keys are not such a change — a v1
# reader keeps working untouched — and the dashboard's check is exact equality
# (``payload._check_schema``), so a gratuitous bump would reject every result.json
# already on disk. ADR-0031's halt episodes are additive and left it at 1, and so
# is ADR-0057's ``market``.
RESULT_SCHEMA_VERSION = 1

# The market a run is assumed to have traded when nobody says otherwise: the only
# one this bench traded before EPIC-87. It is ``US_EQUITY.name`` rather than a
# string literal so there is exactly one spelling of "us_equity" in the codebase —
# the calendar registry's (ADR-0054/0057).
DEFAULT_MARKET = US_EQUITY.name


def summarize(
    result: BacktestResult,
    benchmark: BacktestResult | None = None,
    *,
    periods_per_year: float = 252.0,
    free_parameters: int | None = None,
    significance: SignificanceReport | None = None,
    regimes: RegimeReport | None = None,
    monte_carlo: MonteCarloShuffleReport | None = None,
) -> str:
    """A human-readable run summary: the metrics block plus guardrail lines.

    When ``benchmark`` is supplied, appends a side-by-side total-return line
    comparing the strategy to the (unconstrained) benchmark run, followed by the
    benchmark-relative block: beta, annualized alpha, correlation, information
    ratio, and the exposure-adjusted return of both sides (ADR-0037). Every one of
    those lines is gated on the benchmark, so a run without one prints exactly the
    block it always has. ``periods_per_year``
    scales the annualized figures (Sharpe/Sortino/Calmar/annualized return) to the
    run's bar frequency; the default of 252.0 keeps daily runs byte-identical.

    ``free_parameters`` (from
    :func:`trading.strategies.free_parameter_count`) adds the trades-per-parameter
    significance line and, when the sample is too thin to support that many knobs,
    an explicit warning (ADR-0029). Omitted, the block is unchanged.

    ``significance`` (from :func:`trading.metrics.assess_significance`) adds the
    ADR-0039 block: the bootstrap confidence interval on Sharpe, the paired
    beats-the-benchmark win rate, and the trial-count deflation. It is
    caller-supplied rather than computed here because a bootstrap is the most
    expensive thing in the report by an order of magnitude, and because a run that
    does not ask for it must print exactly the bytes it always has.

    ``regimes`` (from :func:`trading.metrics.compute_regime_report`, ADR-0066)
    appends the regime-split block: the same headline figures restricted to the
    run's own high/low-volatility and trending/mean-reverting bars. Caller-supplied
    for the same reason ``significance`` is — computing it here would mean a run
    that never asked for it pays the cost, and its bytes must stay byte-identical.

    ``monte_carlo`` (from :func:`trading.metrics.monte_carlo_shuffle`, ADR-0067)
    appends the path-shuffle block: the run's own max drawdown placed against
    thousands of random reorderings of its own returns, and the run's Sharpe
    printed once (it does not change under reordering). Caller-supplied and never
    derived here, for the same reason ``significance``/``regimes`` are not.
    """
    metrics = compute(result, periods_per_year, free_parameters=free_parameters)
    lines = [f"Symbols:       {', '.join(result.symbols)}"]
    lines.extend(_absent_lines(result))
    lines.extend(
        [
            f"Starting cash: ${result.starting_cash:,.2f}",
            f"Final equity:  ${result.final_equity:,.2f}",
            f"Total return:  {metrics.total_return * 100:+.2f}%",
            f"Annualized:    {metrics.annualized_return * 100:+.2f}%",
            f"Sharpe:        {metrics.sharpe:.2f}",
            f"Sortino:       {metrics.sortino:.2f}",
            f"Calmar:        {metrics.calmar:.2f}",
            f"Max drawdown:  {metrics.max_drawdown * 100:.2f}%",
            f"Avg exposure:  {metrics.avg_exposure * 100:.2f}%",
            f"Peak exposure: {metrics.peak_exposure * 100:.2f}%",
            f"Win rate:      {metrics.win_rate * 100:.2f}%",
            f"Turnover:      {metrics.turnover * 100:.2f}%",
            f"Trades:        {metrics.trade_count} entry/entries",
            f"Bars:          {len(result.equity_curve)}",
        ]
    )
    if metrics.trades_per_parameter is not None:
        lines.append(
            f"Trades/param:  {metrics.trades_per_parameter:.1f} "
            f"({metrics.trade_count} entries / {free_parameters} free parameter(s))"
        )
        if metrics.underpowered:
            lines.append(
                f"  ⚠ under {MIN_TRADES_PER_PARAMETER:.0f} trades per free parameter — "
                "too small a sample to distinguish edge from noise; widen the "
                "universe or the date range before trusting these numbers"
            )
    if benchmark is not None:
        bench_symbol = ", ".join(benchmark.symbols)
        bench_metrics = compute(benchmark, periods_per_year)
        bench_return = bench_metrics.total_return
        delta = metrics.total_return - bench_return
        lines.append(
            f"Benchmark ({bench_symbol}): {bench_return * 100:+.2f}% "
            f"(strategy {delta * 100:+.2f}% vs benchmark)"
        )
        lines.extend(_benchmark_deployment_lines(benchmark, bench_metrics))
        comparison = compare_to_benchmark(
            result.equity_curve, benchmark.equity_curve, periods_per_year
        )
        lines.extend(
            _benchmark_relative_lines(comparison, metrics, bench_metrics, len(result.equity_curve))
        )
    if significance is not None:
        lines.extend(significance_lines(significance))
    if regimes is not None:
        lines.extend(regime_lines(regimes))
    if monte_carlo is not None:
        lines.extend(monte_carlo_lines(monte_carlo))
    if result.rejections:
        lines.append(f"Rejected:      {len(result.rejections)} order(s)")
    if result.clamps:
        lines.append(f"Clamped:       {len(result.clamps)} order(s)")
    if result.halted and result.halt_ts is not None:
        reason = result.halt_reason or "new entries halted"
        lines.append(f"Halt:          fired at {result.halt_ts.isoformat()} ({reason})")
        lines.extend(_halt_episode_lines(result))
    return "\n".join(lines)


def _absent_lines(result: BacktestResult) -> list[str]:
    """The shrunk-universe caveat: which requested symbols contributed no bars.

    Empty for a run where every symbol had data, so those summaries stay
    byte-identical. When it is not empty, it sits directly under the ``Symbols:``
    line rather than with the guardrail counters at the bottom: absence is not an
    event that happened *during* the run like a clamp or a rejection, it is a
    correctness caveat on every figure below it — the numbers describe
    :attr:`~trading.engine.BacktestResult.traded_symbols`, not the universe the
    operator asked for (ADR-0032).

    Each symbol prints its machine-readable reason code alongside the human
    detail, because "had not listed yet" and "we could not ask" call for
    different responses.
    """
    if not result.absent:
        return []
    traded = result.traded_symbols
    lines = [
        f"Traded:        {', '.join(traded) if traded else '(none)'}",
        f"  ⚠ {len(result.absent)} of {len(result.symbols)} requested symbol(s) "
        "contributed no bars; every figure below covers the reduced universe",
    ]
    lines.extend(f"    {entry.symbol} [{entry.reason}]: {entry.detail}" for entry in result.absent)
    return lines


def _benchmark_rejection_lines(benchmark: BacktestResult) -> list[str]:
    """The benchmark's own rejected orders — invisible before ADR-0037's amendment.

    ``summarize`` counts the *strategy's* rejections and has never looked at the
    benchmark's, which is what let a benchmark that could not fund its entry print
    a confident ``+0.00%``. The first rejection carries the diagnosis (almost
    always an insufficient-cash entry), so it is quoted verbatim rather than
    summarized into a count.
    """
    if not benchmark.rejections:
        return []
    order, reason = benchmark.rejections[0]
    return [
        f"    {len(benchmark.rejections)} benchmark order(s) rejected; first: "
        f"{order.symbol} {order.side.value} {order.qty:g} — {reason}"
    ]


def _benchmark_deployment_lines(
    benchmark: BacktestResult, bench_metrics: PerformanceMetrics
) -> list[str]:
    """Say so when the benchmark did not actually hold the market (ADR-0037 amended).

    A benchmark that never took a position still has an equity curve, a total
    return, and a tidy ``+0.00%`` — the return on idle cash, printed in the slot
    where a market return belongs. That is the exact failure mode this bench exists
    to prevent, and it is worse than a wrong number because the paired bootstrap
    (ADR-0039) reads the same curve and turns "beats buy-and-hold" into "beats
    cash" without changing a word of its output.

    Two conditions, both derived from the run rather than from a threshold:
    zero peak exposure means it never held anything at all, and a first exposed bar
    later than the first fillable one (index 1 — an order placed on bar 0 fills at
    bar 1's open, ADR-0001) means it sat in cash for a while first. Neither line
    appears for a benchmark that entered on the first opportunity and held, so
    every healthy run's summary is byte-identical.
    """
    if bench_metrics.peak_exposure <= 0.0:
        return [
            "  ⚠ the benchmark never took a position — the figure above is the return on "
            "idle cash, not a market return, and every benchmark-relative figure below "
            "describes a flat line",
            *_benchmark_rejection_lines(benchmark),
        ]
    entered = next(
        (i for i, point in enumerate(benchmark.equity_curve) if point.exposure > 0.0), None
    )
    if entered is not None and entered > 1:
        return [
            f"  ⚠ the benchmark held nothing until bar {entered + 1} of "
            f"{len(benchmark.equity_curve)} — the earlier bars are idle cash, so its "
            "return understates buy-and-hold over the full span",
            *_benchmark_rejection_lines(benchmark),
        ]
    return []


def _stat(value: float | None) -> str:
    """A ratio-shaped statistic, or ``n/a`` when it is undefined.

    ``n/a`` rather than ``0.00``: a beta the data cannot support is not a beta of
    zero, and the two must never look alike on the page (ADR-0037).
    """
    return "n/a" if value is None else f"{value:.2f}"


def _stat_pct(value: float | None) -> str:
    """A percentage-shaped statistic, or ``n/a`` when it is undefined."""
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _benchmark_relative_lines(
    comparison: BenchmarkComparison,
    metrics: PerformanceMetrics,
    bench_metrics: PerformanceMetrics,
    strategy_bars: int,
) -> list[str]:
    """The benchmark-relative block (ADR-0037), printed only when a benchmark ran.

    Leads with the overlap caveat when the benchmark did not cover every strategy
    bar, because every figure underneath then describes the shared span rather than
    the run — the same "say what the numbers actually cover" rule ADR-0032 applies
    to a shrunk universe. With fewer than two shared bars there is nothing to
    compute and the block reduces to one line saying so, rather than four ``n/a``s.

    The closing exposure-adjusted line is the point of the whole block: a strategy
    that averaged 17% invested and a benchmark that is fully invested by
    construction are not comparable on raw return, and this restates both per unit
    of capital actually at risk.
    """
    if comparison.shared_bars < 2:
        return [
            f"Bench overlap: {comparison.shared_bars} shared bar(s) with the benchmark — "
            "too few to compute beta, alpha, correlation, or information ratio"
        ]
    lines: list[str] = []
    if comparison.shared_bars < strategy_bars:
        lines.append(
            f"Bench overlap: {comparison.shared_bars} of {strategy_bars} strategy bars; "
            "the benchmark-relative figures below cover only the shared span"
        )
    lines.extend(
        [
            f"Beta:          {_stat(comparison.beta)}",
            f"Alpha (ann.):  {_stat_pct(comparison.alpha)}",
            f"Correlation:   {_stat(comparison.correlation)}",
            f"Info ratio:    {_stat(comparison.information_ratio)}",
            f"Ret/exposure:  {_stat_pct(metrics.return_per_unit_exposure)} vs benchmark "
            f"{_stat_pct(bench_metrics.return_per_unit_exposure)} "
            f"(annualized return per unit of avg exposure; "
            f"{metrics.avg_exposure * 100:.2f}% vs "
            f"{bench_metrics.avg_exposure * 100:.2f}% invested)",
        ]
    )
    return lines


def _sharpe_interval_lines(interval: SharpeInterval) -> list[str]:
    """The Sharpe confidence interval, and the warning that matters most.

    A point estimate reads as a measurement; an interval reads as what it is. When
    the interval straddles zero the run has *not* measured an edge, and the report
    says exactly that instead of leaving the reader to compare two numbers in their
    head — the same rule ADR-0029 applied to a thin trade count.
    """
    lines = [
        f"Sharpe {interval.confidence * 100:.0f}% CI: "
        f"[{interval.low:+.2f}, {interval.high:+.2f}]  "
        f"(stationary block bootstrap: {interval.resamples} resamples, "
        f"{interval.block_length}-bar blocks, {interval.observations} return periods, "
        f"seed {interval.seed})"
    ]
    if interval.block_length_was_reduced:
        lines.append(
            f"  note: block length cut from {interval.requested_block_length} to "
            f"{interval.block_length} bars — the series is too short to hold blocks that "
            "long, so less autocorrelation is preserved than intended"
        )
    if interval.straddles_zero:
        lines.append(
            "  ⚠ the interval straddles zero — this sample cannot distinguish the "
            "strategy from having no edge at all; the point estimate is not a finding"
        )
    return lines


def _paired_lines(paired: PairedBootstrap) -> list[str]:
    """The paired beats-the-benchmark win rate (the powerful figure, ADR-0039)."""
    return [
        f"Beats bench:   {paired.win_rate * 100:.1f}% of {paired.resamples} PAIRED resamples "
        f"(strategy Sharpe > benchmark Sharpe on the same blocks; "
        f"observed edge {paired.observed_edge:+.2f} over {paired.observations} shared periods)"
    ]


def _deflated_lines(deflated: DeflatedSharpe) -> list[str]:
    """The trial-count deflation (KAN-619): what the winner is worth after the search."""
    lines = [
        f"Trials:        {deflated.trials} scored; the luckiest skill-free one would show "
        f"Sharpe {deflated.null_best_sharpe:+.2f} (observed {deflated.observed_sharpe:+.2f})"
    ]
    if deflated.probability is None:
        lines.append(
            "Deflated:      n/a — the skew/kurtosis correction is undefined on this "
            "return series, so no probability can be stated"
        )
        return lines
    lines.append(f"Deflated:      P(true Sharpe > that null best) = {deflated.probability:.2f}")
    if not deflated.significant:
        lines.append(
            f"  ⚠ below {DEFLATED_SHARPE_CONFIDENCE:.2f} — after discounting for "
            f"{deflated.trials} trial(s), this Sharpe is not distinguishable from the best "
            "of that many skill-free runs"
        )
    return lines


def significance_lines(significance: SignificanceReport) -> list[str]:
    """Render the whole ADR-0039 significance block, notes included.

    Each of the three figures is rendered only when it could be computed; anything
    that could not is explained by a ``note:`` line rather than silently missing,
    because "we did not measure this" and "we measured it and it was zero" are the
    two things this bench refuses to conflate.
    """
    lines: list[str] = []
    if significance.sharpe_interval is not None:
        lines.extend(_sharpe_interval_lines(significance.sharpe_interval))
    if significance.paired is not None:
        lines.extend(_paired_lines(significance.paired))
    if significance.deflated is not None:
        lines.extend(_deflated_lines(significance.deflated))
    lines.extend(f"  note: {note}" for note in significance.notes)
    return lines


def summarize_significance(significance: SignificanceReport | None) -> str:
    """:func:`significance_lines` as one block of text, or ``""`` when absent.

    The entry point a non-``summarize`` caller (the sweep command) uses to print
    the same block under its own ranking table, so the two commands never grow
    divergent wordings for the same statistic.
    """
    if significance is None:
        return ""
    return "\n".join(significance_lines(significance))


def _regime_metrics_lines(label: str, regime: RegimeMetrics) -> list[str]:
    """One regime slice's headline figures, on the same one-line-per-figure shape
    the whole-run block above uses.

    Prints the figures whether or not the slice :attr:`~RegimeMetrics.underpowered`
    — the reader decides what to trust, exactly as ADR-0029 does for a thin
    trades-per-parameter ratio — but a too-thin slice gets an explicit warning
    line naming the floor it missed, rather than a caveat buried in ``notes``.
    """
    m = regime.metrics
    lines = [
        f"  {label:<14} bars={regime.bar_count:<5} "
        f"return={m.total_return * 100:+7.2f}%  ann={m.annualized_return * 100:+7.2f}%  "
        f"Sharpe={m.sharpe:+.2f}  Sortino={m.sortino:+.2f}  Calmar={m.calmar:+.2f}  "
        f"maxDD={m.max_drawdown * 100:6.2f}%"
    ]
    if regime.underpowered:
        lines.append(
            f"    ⚠ {regime.bar_count} return period(s) is below "
            f"{MIN_BOOTSTRAP_OBSERVATIONS} — too thin to read this slice's Sharpe/Sortino/"
            "Calmar as a measurement"
        )
    return lines


def regime_lines(regimes: RegimeReport) -> list[str]:
    """Render the whole ADR-0066 regime-split block, notes included.

    Two independent splits — volatility (high/low) and trend (trending/mean-
    reverting) — each restricted to the run's *own* :class:`PerformanceMetrics`,
    on top of (never instead of) the whole-run figures ``summarize`` already
    printed. When the curve was too short to classify even one bar, every slot is
    ``None`` and the block is just the one explanatory note.
    """
    if regimes.vol_threshold is None or regimes.trend_threshold is None:
        return [f"  note: {note}" for note in regimes.notes]
    assert regimes.high_vol is not None
    assert regimes.low_vol is not None
    assert regimes.trending is not None
    assert regimes.mean_reverting is not None
    lines = [
        f"Regimes (window={regimes.window} bars; vol split @ "
        f"{regimes.vol_threshold * 100:.2f}% ann., trend split @ "
        f"{regimes.trend_threshold:.2f} efficiency ratio):"
    ]
    lines.extend(_regime_metrics_lines("high_vol", regimes.high_vol))
    lines.extend(_regime_metrics_lines("low_vol", regimes.low_vol))
    lines.extend(_regime_metrics_lines("trending", regimes.trending))
    lines.extend(_regime_metrics_lines("mean_reverting", regimes.mean_reverting))
    lines.extend(f"  note: {note}" for note in regimes.notes)
    return lines


def monte_carlo_lines(report: MonteCarloShuffleReport) -> list[str]:
    """Render the whole ADR-0067 Monte Carlo path-shuffle block, notes included.

    When the curve was too short to shuffle meaningfully, every field is ``None``
    and this is just the explanatory ``note:`` line — the same "say why, don't go
    silent" rule ADR-0039/ADR-0066 already follow.
    """
    if report.actual_max_drawdown is None:
        return [f"  note: {note}" for note in report.notes]
    assert report.shuffled_low is not None
    assert report.shuffled_median is not None
    assert report.shuffled_high is not None
    assert report.actual_percentile is not None
    assert report.sharpe is not None
    lines = [
        f"Monte Carlo shuffle ({report.resamples} random reorderings of "
        f"{report.observations} return period(s), seed {report.seed}):",
        f"  Sharpe (order-invariant): {report.sharpe:+.2f}  — mean/stdev do not depend "
        "on the order returns are summed in, so reordering cannot change this; it is a "
        "single value, not a resampled distribution (cf. the bootstrap CI above, which "
        "is about estimation uncertainty, not path order)",
        f"  Max drawdown — actual path:            {report.actual_max_drawdown * 100:6.2f}%",
        f"  Max drawdown — shuffled {report.confidence * 100:.0f}% range: "
        f"[{report.shuffled_low * 100:.2f}%, {report.shuffled_high * 100:.2f}%]  "
        f"(median {report.shuffled_median * 100:.2f}%)",
        f"  Actual path's drawdown sits at the {report.actual_percentile * 100:.1f} "
        "percentile of the shuffled distribution",
    ]
    if report.worse_than_shuffled:
        lines.append(
            "  ⚠ the actual path's drawdown is worse than nearly every random reordering "
            "of the SAME returns — this run's sequence saw an unusually bad CLUSTERING of "
            "losses (bad luck in ordering, or a structural vulnerability), not merely an "
            "unlucky total amount of loss"
        )
    elif report.better_than_shuffled:
        lines.append(
            "  ⚠ the actual path's drawdown is better than nearly every random reordering "
            "of the SAME returns — this run's own sequence was unusually FORTUNATE; a live "
            "deployment should not expect to be this lucky again"
        )
    lines.extend(f"  note: {note}" for note in report.notes)
    return lines


def summarize_monte_carlo(report: MonteCarloShuffleReport | None) -> str:
    """:func:`monte_carlo_lines` as one block of text, or ``""`` when absent.

    Mirrors :func:`summarize_significance`'s shape, for the same reason: a caller
    other than ``summarize`` that wants the identical wording without duplicating it.
    """
    if report is None:
        return ""
    return "\n".join(monte_carlo_lines(report))


def _halt_episode_lines(result: BacktestResult) -> list[str]:
    """The halt-episode count line, when opt-in recovery actually did something.

    A run under the default permanent latch has exactly one open-ended episode, so
    the count adds nothing the ``Halt:`` line above does not already say and the
    summary stays byte-identical (ADR-0031). Once a halt has re-armed — or tripped
    more than once — the single boolean is misleading on its own, so the episode
    count and each stretch's span are spelled out.
    """
    episodes = result.halt_episodes
    resumed = [e for e in episodes if e.resume_ts is not None]
    if len(episodes) <= 1 and not resumed:
        return []
    lines = [
        f"Halt episodes: {len(episodes)} ({len(resumed)} re-armed, "
        f"{len(episodes) - len(resumed)} still in force at the end)"
    ]
    for index, episode in enumerate(episodes, start=1):
        until = episode.resume_ts.isoformat() if episode.resume_ts is not None else "(in force)"
        lines.append(f"  #{index} {episode.halt_ts.isoformat()} → {until}  ({episode.reason})")
    return lines


def write_equity_csv(
    result: BacktestResult, path: Path, benchmark: BacktestResult | None = None
) -> None:
    """Write the equity curve, one row per trading day.

    Columns: ``ts,equity,exposure``. When ``benchmark`` is supplied, a trailing
    ``benchmark_equity`` column carries the benchmark's equity aligned to each
    row's timestamp (blank on a day the benchmark has no bar).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["ts", "equity", "exposure"]
    bench_by_ts: dict[str, float] = {}
    if benchmark is not None:
        header.append("benchmark_equity")
        bench_by_ts = {p.ts.isoformat(): p.equity for p in benchmark.equity_curve}
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for point in result.equity_curve:
            ts = point.ts.isoformat()
            row = [ts, f"{point.equity:.6f}", f"{point.exposure:.6f}"]
            if benchmark is not None:
                bench = bench_by_ts.get(ts)
                row.append(f"{bench:.6f}" if bench is not None else "")
            writer.writerow(row)


def write_equity_png(
    result: BacktestResult, path: Path, benchmark: BacktestResult | None = None
) -> None:
    """Plot the equity curve (and benchmark, if any) to a PNG.

    matplotlib is an optional dependency: it is imported lazily here so the rest
    of the bench runs without it. A clear error is raised when it is unavailable.
    """
    try:
        import matplotlib  # type: ignore[import-not-found]

        matplotlib.use("Agg")  # headless: no display required.
        import matplotlib.pyplot as plt  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise RuntimeError(
            "matplotlib is required to write a PNG plot; install it "
            "(e.g. `uv pip install matplotlib`) or omit --plot"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    curve: list[EquityPoint] = result.equity_curve
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(
        [p.ts for p in curve],
        [p.equity for p in curve],
        label=", ".join(result.symbols),
    )
    if benchmark is not None:
        bench_curve = benchmark.equity_curve
        ax.plot(
            [p.ts for p in bench_curve],
            [p.equity for p in bench_curve],
            label=f"benchmark ({', '.join(benchmark.symbols)})",
            linestyle="--",
        )
    ax.set_xlabel("date")
    ax.set_ylabel("equity ($)")
    ax.set_title("Equity curve")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _equity_curve_to_list(curve: list[EquityPoint]) -> list[dict[str, Any]]:
    """Serialize an equity curve to ``{"ts", "equity", "exposure"}`` records."""
    return [
        {"ts": point.ts.isoformat(), "equity": point.equity, "exposure": point.exposure}
        for point in curve
    ]


def _resolve_periods_per_year(
    frequency: str, override: float | None, market: str = DEFAULT_MARKET
) -> float:
    """Annualization factor for a run, from an explicit value or interval + market.

    The interval label alone is not enough: ``"5m"`` is 19,656 bars a year on the
    US-equity session and 105,120 on a market that never closes, so parsing the
    label on a fixed calendar is how a crypto run ends up with an equity Sharpe
    (ADR-0054's own recorded gap, closed by ADR-0057). ``market`` names a registered
    :class:`~trading.calendar.MarketCalendar` and an **unknown one raises**, exactly
    as :func:`~trading.calendar.get_calendar` does — falling back to equity here
    would reinstate the silent default the calendar registry exists to refuse.

    ``frequency`` remains a free string in the schema, so an unrecognized *label*
    still falls back rather than raising — to that market's daily basis (252 on
    equity, unchanged; 365 on a 24/7 market). A label is a reporting detail; the
    market is not.
    """
    if override is not None:
        return override
    calendar = get_calendar(market)
    try:
        return Frequency.parse(frequency, calendar=calendar).periods_per_year
    except ValueError:
        return calendar.periods_per_year(timedelta(days=1))


def _benchmark_metrics_block(
    result: BacktestResult,
    benchmark_curve: list[EquityPoint] | None,
    benchmark_metrics: BenchmarkComparison | None,
    periods_per_year: float,
) -> dict[str, Any] | None:
    """The serialized benchmark-relative block, or ``None`` when no benchmark ran.

    ``result.json`` already holds both equity series, so a document with a
    ``benchmark_curve`` but no comparison would be withholding a relation it had
    every input for — the block is therefore derived here when the caller did not
    supply one. An explicit ``benchmark_metrics`` always wins; with no benchmark
    curve and no explicit block the value is ``null`` (ADR-0037).
    """
    if benchmark_metrics is None:
        if benchmark_curve is None:
            return None
        benchmark_metrics = compare_to_benchmark(
            result.equity_curve, benchmark_curve, periods_per_year
        )
    return asdict(benchmark_metrics)


def result_to_dict(
    result: BacktestResult,
    *,
    mode: str,
    frequency: str = "1d",
    market: str = DEFAULT_MARKET,
    metrics: PerformanceMetrics | None = None,
    benchmark_curve: list[EquityPoint] | None = None,
    benchmark_metrics: BenchmarkComparison | None = None,
    periods_per_year: float | None = None,
    significance: SignificanceReport | None = None,
    regimes: RegimeReport | None = None,
    monte_carlo: MonteCarloShuffleReport | None = None,
) -> dict[str, Any]:
    """Build the canonical, JSON-serializable dict describing a completed run.

    This is the single machine-readable contract a run emits — the forthcoming
    web dashboard reads exactly this shape. Fills, guardrail clamps, and
    rejections (previously only in the human text summary) are surfaced here so
    downstream consumers never have to parse prose. The returned dict round-trips
    through :func:`json.dumps` with the stock encoder: every value is a JSON
    primitive, list, or dict — datetimes are ISO-8601 strings and enums are their
    ``.value``.

    Schema (top-level keys)::

        {
          "schema_version": int,      # RESULT_SCHEMA_VERSION constant
          "mode": str,                # "backtest" | "paper"
          "frequency": str,           # interval label, e.g. "1d" (free string)
          "market": str,              # MarketCalendar name; ADR-0057, additive
          "symbols": list[str],
          "starting_cash": float,
          "final_equity": float,
          "total_return": float,      # final/starting - 1
          "equity_curve": [           # one record per bar
            {"ts": iso8601 str, "equity": float, "exposure": float}, ...
          ],
          "benchmark_curve": same shape as equity_curve, or null,
          "metrics": dataclasses.asdict(metrics) or null,
          "benchmark_metrics": {   # ADR-0037, additive; null when no benchmark ran
            "shared_bars": int,          # timestamps the two curves had in common
            "beta": float | null, "alpha": float | null,
            "correlation": float | null, "information_ratio": float | null
          } | null,
          "fills": [
            {"ts": iso8601 str, "symbol": str, "side": str,
             "qty": float, "price": float, "commission": float}, ...
          ],
          "clamps": [                 # orders a guardrail cap trimmed down
            {"symbol": str, "original_qty": float, "clamped_qty": float,
             "side": str, "reason": str}, ...
          ],
          "rejections": [             # orders a guardrail/broker vetoed
            {"symbol": str, "qty": float, "side": str, "reason": str}, ...
          ],
          "absent": [                 # requested symbols that contributed no bars
            {"symbol": str, "reason": str, "detail": str}, ...   # ADR-0032, additive
          ],
          "significance": {   # ADR-0039, additive; null when it was not computed
            "sharpe_interval": {"point": float, "low": float, "high": float,
                                "confidence": float, "resamples": int,
                                "block_length": int, "requested_block_length": int,
                                "observations": int, "seed": int} | null,
            "paired": {"win_rate": float, "observed_edge": float, "resamples": int,
                       "block_length": int, "requested_block_length": int,
                       "observations": int, "seed": int} | null,
            "deflated": {"trials": int, "observed_sharpe": float,
                         "null_best_sharpe": float, "probability": float | null,
                         "observations": int, "trial_sharpe_stdev": float | null,
                         "skew": float, "kurtosis": float} | null,
            "notes": list[str]
          } | null,
          "halt": {"halted": bool,          # a halt occurred during the run
                   "halt_ts": iso8601 str | null,     # the FIRST halt
                   "halt_reason": str | null,
                   # ADR-0031, additive: every halt stretch, in order. Exactly one
                   # open-ended entry under the default permanent latch.
                   "episode_count": int,
                   "episodes": [
                     {"halt_ts": iso8601 str, "resume_ts": iso8601 str | null,
                      "reason": str}, ...
                   ]},
          "regimes": {   # ADR-0066, additive; KEY OMITTED ENTIRELY when not computed
            "window": int, "vol_threshold": float | null, "trend_threshold": float | null,
            "high_vol": {"label": str, "bar_count": int,
                         "metrics": dataclasses.asdict(PerformanceMetrics)} | null,
            "low_vol": same shape as "high_vol" | null,
            "trending": same shape as "high_vol" | null,
            "mean_reverting": same shape as "high_vol" | null,
            "notes": list[str]
          },   # present only when the caller supplied a RegimeReport
          "monte_carlo": {   # ADR-0067, additive; KEY OMITTED ENTIRELY when not computed
            "resamples": int, "seed": int, "confidence": float, "observations": int,
            "sharpe": float | null,               # order-invariant; not a distribution
            "actual_max_drawdown": float | null,  # the run's own real path-ordered value
            "shuffled_low": float | null, "shuffled_median": float | null,
            "shuffled_high": float | null,
            "actual_percentile": float | null,    # actual's rank in the shuffled population
            "notes": list[str]
          }   # present only when the caller supplied a MonteCarloShuffleReport
        }

    The ``episode_count``/``episodes`` keys (ADR-0031), the top-level ``absent``
    list (ADR-0032), the top-level ``benchmark_metrics`` block plus the
    ``metrics.return_per_unit_exposure`` field (ADR-0037), the top-level
    ``significance`` block (ADR-0039), the top-level ``market`` name (ADR-0057),
    the top-level ``regimes`` block (ADR-0066), and the top-level ``monte_carlo``
    block (ADR-0067) are purely additive: every pre-existing key keeps its exact
    meaning and value — ``symbols`` is still the *requested* universe, ``metrics``
    is still exactly ``dataclasses.asdict`` of what the caller passed — so
    ``RESULT_SCHEMA_VERSION`` does **not** move (see the constant's note). A v1
    reader that ignores them behaves exactly as it did. ``regimes`` and
    ``monte_carlo`` are the two keys among these that are **omitted** rather than
    emitted as ``null`` when absent (every earlier one keeps the
    always-present-null shape established by ``significance``) — chosen so a run
    that never passes the corresponding flag writes the byte-identical document it
    always has. That always-null shape was only ever safe for the ADR that first
    introduced a given top-level key: once a baseline `result.json` hash is pinned
    (as it was the moment ADR-0066 shipped), any *later* additive key must be
    omitted rather than nulled, or it moves that already-pinned hash for every run
    that never asked for the new feature. A v1 reader already tolerates a missing
    key exactly as it tolerates a ``null`` one, so nothing downstream distinguishes
    the two conventions.

    ``benchmark_metrics`` sits at the top level rather than inside ``metrics``
    because it describes a *relation between two runs*, not a property of this
    one; it belongs beside ``benchmark_curve``. It is ``null`` whenever no
    benchmark ran, and a ``null`` statistic inside a present block means
    "undefined on this data" — which is not the same fact as "no benchmark".

    Parameters
    ----------
    result:
        The completed :class:`~trading.engine.BacktestResult`.
    mode:
        ``"backtest"`` or ``"paper"`` — which driver produced the run.
    frequency:
        A plain interval label (default ``"1d"``). Kept a free string so a later
        intraday lane forward-fits without a schema change.
    market:
        The name of the :class:`~trading.calendar.MarketCalendar` the run traded
        (default ``"us_equity"``). Recorded so a reader never has to *remember*
        which market produced a document: ``frequency`` alone cannot say whether
        ``"1d"`` meant 252 bars a year or 365, and every risk-adjusted figure in
        ``metrics`` depends on that (ADR-0054/0057). It is also what resolves the
        annualization for the derived ``benchmark_metrics`` block below, so an
        unknown name raises rather than defaulting to equity.
    metrics:
        An already-computed :class:`~trading.metrics.PerformanceMetrics`, or
        ``None``. Serialized generically via :func:`dataclasses.asdict` so new
        metric fields flow through automatically; ``None`` emits ``null``. This
        function never computes metrics itself.
    benchmark_curve:
        An optional aligned benchmark equity curve, or ``None`` (emits ``null``).
        When present and ``benchmark_metrics`` is omitted, the benchmark-relative
        block is derived here from the two curves this function already holds
        (ADR-0037), so a caller gets it without a second computation.
    benchmark_metrics:
        An already-computed :class:`~trading.metrics.BenchmarkComparison`. Wins
        over the derivation above when supplied.
    periods_per_year:
        Annualization factor for that derivation. ``None`` (the default) resolves
        it from the ``frequency`` label **on the ``market``'s calendar**, so a
        daily equity run keeps the 252 basis, an intraday one scales correctly,
        and a 24/7 run is not silently annualized on the equity session — all
        without the caller repeating itself.
    significance:
        An already-computed :class:`~trading.metrics.SignificanceReport`
        (ADR-0039), or ``None`` (emits ``null``). Unlike ``benchmark_metrics`` this
        is **never derived here**: a bootstrap costs thousands of Sharpe
        computations, so writing a ``result.json`` must not silently pay for one
        the caller did not ask for.
    regimes:
        An already-computed :class:`~trading.metrics.RegimeReport` (ADR-0066), or
        ``None``. Never derived here, for the same reason ``significance`` is not:
        a run that did not ask for the regime split must not silently pay for it.
        Unlike ``significance`` — present as ``null`` even when absent — the
        ``regimes`` key is **omitted entirely** when ``None``, so a run that never
        passes ``--regimes`` emits the exact bytes it always has (pinned by a CLI
        golden); the key appears only once a caller actually supplies a report.
    monte_carlo:
        An already-computed :class:`~trading.metrics.MonteCarloShuffleReport`
        (ADR-0067), or ``None``. Never derived here, for the same reason
        ``significance``/``regimes`` are not: a run that did not ask for the
        path-shuffle must not silently pay for thousands of reshuffles. Like
        ``regimes`` (and unlike ``significance``, whose always-null convention
        predates a pinned baseline hash), the ``monte_carlo`` key is **omitted
        entirely** when ``None`` rather than emitted as ``null``, so a run that
        never passes ``--monte-carlo`` emits the exact bytes it always has.
    """
    payload: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "mode": mode,
        "frequency": frequency,
        # Which market's year the metrics below were annualized on. An interval
        # label cannot carry that, and a reader who has to remember it will
        # eventually remember it wrong (ADR-0057).
        "market": market,
        "symbols": list(result.symbols),
        "starting_cash": result.starting_cash,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "equity_curve": _equity_curve_to_list(result.equity_curve),
        "benchmark_curve": (
            _equity_curve_to_list(benchmark_curve) if benchmark_curve is not None else None
        ),
        "metrics": asdict(metrics) if metrics is not None else None,
        # The relation between the two curves above, additive under ADR-0037.
        # ``null`` means no benchmark ran; a ``null`` *inside* it means the
        # statistic is undefined on the shared span. Never a stand-in 0.0.
        "benchmark_metrics": _benchmark_metrics_block(
            result,
            benchmark_curve,
            benchmark_metrics,
            _resolve_periods_per_year(frequency, periods_per_year, market),
        ),
        "fills": [
            {
                "ts": ts.isoformat(),
                "symbol": fill.symbol,
                "side": fill.side.value,
                "qty": fill.qty,
                "price": fill.price,
                "commission": fill.commission,
            }
            for ts, fill in result.fills
        ],
        "clamps": [
            {
                "symbol": original.symbol,
                "original_qty": original.qty,
                "clamped_qty": clamped.qty,
                "side": original.side.value,
                "reason": reason,
            }
            for original, clamped, reason in result.clamps
        ],
        "rejections": [
            {
                "symbol": order.symbol,
                "qty": order.qty,
                "side": order.side.value,
                "reason": reason,
            }
            for order, reason in result.rejections
        ],
        # A shrunk universe is a caveat on every number in this document, so it is
        # machine-readable too rather than only in the text summary (ADR-0032).
        "absent": [
            {"symbol": entry.symbol, "reason": entry.reason, "detail": entry.detail}
            for entry in result.absent
        ],
        # Whether the Sharpe above means anything (ADR-0039). ``null`` means the
        # caller did not ask for a bootstrap — NOT that the run was insignificant.
        "significance": asdict(significance) if significance is not None else None,
        "halt": {
            "halted": result.halted,
            "halt_ts": result.halt_ts.isoformat() if result.halt_ts is not None else None,
            "halt_reason": result.halt_reason,
            "episode_count": len(result.halt_episodes),
            "episodes": [
                {
                    "halt_ts": episode.halt_ts.isoformat(),
                    "resume_ts": (
                        episode.resume_ts.isoformat() if episode.resume_ts is not None else None
                    ),
                    "reason": episode.reason,
                }
                for episode in result.halt_episodes
            ],
        },
    }
    # The regime-split block (ADR-0066). Omitted entirely — not even a ``null`` —
    # when the caller did not supply one, so a run without ``--regimes`` writes the
    # exact bytes it always has, unlike ``significance``'s always-present ``null``.
    if regimes is not None:
        payload["regimes"] = asdict(regimes)
    # The Monte Carlo path-shuffle block (ADR-0067). Same omitted-entirely
    # convention as ``regimes``, for the same reason: a run without
    # ``--monte-carlo`` must write the exact bytes it always has.
    if monte_carlo is not None:
        payload["monte_carlo"] = asdict(monte_carlo)
    return payload


def write_result_json(
    result: BacktestResult,
    path: Path,
    *,
    mode: str,
    frequency: str = "1d",
    market: str = DEFAULT_MARKET,
    metrics: PerformanceMetrics | None = None,
    benchmark_curve: list[EquityPoint] | None = None,
    benchmark_metrics: BenchmarkComparison | None = None,
    periods_per_year: float | None = None,
    significance: SignificanceReport | None = None,
    regimes: RegimeReport | None = None,
    monte_carlo: MonteCarloShuffleReport | None = None,
) -> None:
    """Serialize ``result`` via :func:`result_to_dict` and write it to ``path``.

    Writes pretty-printed JSON (``indent=2``). Creates parent directories as
    needed. See :func:`result_to_dict` for the schema and parameter meanings.
    """
    payload = result_to_dict(
        result,
        mode=mode,
        frequency=frequency,
        market=market,
        metrics=metrics,
        benchmark_curve=benchmark_curve,
        benchmark_metrics=benchmark_metrics,
        periods_per_year=periods_per_year,
        significance=significance,
        regimes=regimes,
        monte_carlo=monte_carlo,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2)
