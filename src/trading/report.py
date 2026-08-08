"""V4 reporting: a full metrics summary, an equity-curve CSV, and an optional PNG.

``summarize`` renders the headline performance block — total & annualized return,
Sharpe, Sortino, Calmar, max drawdown, average/peak exposure, win rate, and
turnover — alongside the V3 guardrail lines (rejected/clamped orders, halt) and,
when the caller supplies the strategy's free-parameter count, the
trades-per-parameter sample-size check with a warning when it is too thin to be
evidence (ADR-0029).
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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading.metrics import MIN_TRADES_PER_PARAMETER, compute

if TYPE_CHECKING:
    from trading.engine import BacktestResult, EquityPoint
    from trading.metrics import PerformanceMetrics


# Schema version of the canonical machine-readable run artifact emitted by
# ``result_to_dict`` / ``write_result_json``. The web dashboard reads this to
# decide how to parse the document; bump it whenever the shape changes in a way
# a consumer must notice. Purely *additive* keys are not such a change — a v1
# reader keeps working untouched — and the dashboard's check is exact equality
# (``payload._check_schema``), so a gratuitous bump would reject every result.json
# already on disk. ADR-0031's halt episodes are additive and left it at 1.
RESULT_SCHEMA_VERSION = 1


def summarize(
    result: BacktestResult,
    benchmark: BacktestResult | None = None,
    *,
    periods_per_year: float = 252.0,
    free_parameters: int | None = None,
) -> str:
    """A human-readable run summary: the metrics block plus guardrail lines.

    When ``benchmark`` is supplied, appends a side-by-side total-return line
    comparing the strategy to the (unconstrained) benchmark run. ``periods_per_year``
    scales the annualized figures (Sharpe/Sortino/Calmar/annualized return) to the
    run's bar frequency; the default of 252.0 keeps daily runs byte-identical.

    ``free_parameters`` (from
    :func:`trading.strategies.free_parameter_count`) adds the trades-per-parameter
    significance line and, when the sample is too thin to support that many knobs,
    an explicit warning (ADR-0029). Omitted, the block is unchanged.
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
        bench_return = compute(benchmark, periods_per_year).total_return
        delta = metrics.total_return - bench_return
        lines.append(
            f"Benchmark ({bench_symbol}): {bench_return * 100:+.2f}% "
            f"(strategy {delta * 100:+.2f}% vs benchmark)"
        )
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


def result_to_dict(
    result: BacktestResult,
    *,
    mode: str,
    frequency: str = "1d",
    metrics: PerformanceMetrics | None = None,
    benchmark_curve: list[EquityPoint] | None = None,
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
          "symbols": list[str],
          "starting_cash": float,
          "final_equity": float,
          "total_return": float,      # final/starting - 1
          "equity_curve": [           # one record per bar
            {"ts": iso8601 str, "equity": float, "exposure": float}, ...
          ],
          "benchmark_curve": same shape as equity_curve, or null,
          "metrics": dataclasses.asdict(metrics) or null,
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
          "halt": {"halted": bool,          # a halt occurred during the run
                   "halt_ts": iso8601 str | null,     # the FIRST halt
                   "halt_reason": str | null,
                   # ADR-0031, additive: every halt stretch, in order. Exactly one
                   # open-ended entry under the default permanent latch.
                   "episode_count": int,
                   "episodes": [
                     {"halt_ts": iso8601 str, "resume_ts": iso8601 str | null,
                      "reason": str}, ...
                   ]}
        }

    The ``episode_count``/``episodes`` keys (ADR-0031) and the top-level ``absent``
    list (ADR-0032) are purely additive: every pre-existing key keeps its exact
    meaning and value — ``symbols`` is still the *requested* universe — so
    ``RESULT_SCHEMA_VERSION`` does **not** move (see the constant's note). A v1
    reader that ignores ``absent`` behaves exactly as it did.

    Parameters
    ----------
    result:
        The completed :class:`~trading.engine.BacktestResult`.
    mode:
        ``"backtest"`` or ``"paper"`` — which driver produced the run.
    frequency:
        A plain interval label (default ``"1d"``). Kept a free string so a later
        intraday lane forward-fits without a schema change.
    metrics:
        An already-computed :class:`~trading.metrics.PerformanceMetrics`, or
        ``None``. Serialized generically via :func:`dataclasses.asdict` so new
        metric fields flow through automatically; ``None`` emits ``null``. This
        function never computes metrics itself.
    benchmark_curve:
        An optional aligned benchmark equity curve, or ``None`` (emits ``null``).
    """
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "mode": mode,
        "frequency": frequency,
        "symbols": list(result.symbols),
        "starting_cash": result.starting_cash,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "equity_curve": _equity_curve_to_list(result.equity_curve),
        "benchmark_curve": (
            _equity_curve_to_list(benchmark_curve) if benchmark_curve is not None else None
        ),
        "metrics": asdict(metrics) if metrics is not None else None,
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


def write_result_json(
    result: BacktestResult,
    path: Path,
    *,
    mode: str,
    frequency: str = "1d",
    metrics: PerformanceMetrics | None = None,
    benchmark_curve: list[EquityPoint] | None = None,
) -> None:
    """Serialize ``result`` via :func:`result_to_dict` and write it to ``path``.

    Writes pretty-printed JSON (``indent=2``). Creates parent directories as
    needed. See :func:`result_to_dict` for the schema and parameter meanings.
    """
    payload = result_to_dict(
        result,
        mode=mode,
        frequency=frequency,
        metrics=metrics,
        benchmark_curve=benchmark_curve,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2)
