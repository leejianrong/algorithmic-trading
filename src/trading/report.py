"""V4 reporting: a full metrics summary, an equity-curve CSV, and an optional PNG.

``summarize`` renders the headline performance block — total & annualized return,
Sharpe, Sortino, Calmar, max drawdown, average/peak exposure, win rate, and
turnover — alongside the V3 guardrail lines (rejected/clamped orders, halt).
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

from trading.metrics import compute

if TYPE_CHECKING:
    from trading.engine import BacktestResult, EquityPoint
    from trading.metrics import PerformanceMetrics


# Schema version of the canonical machine-readable run artifact emitted by
# ``result_to_dict`` / ``write_result_json``. The web dashboard reads this to
# decide how to parse the document; bump it whenever the shape changes in a way
# a consumer must notice.
RESULT_SCHEMA_VERSION = 1


def summarize(
    result: BacktestResult,
    benchmark: BacktestResult | None = None,
    *,
    periods_per_year: float = 252.0,
) -> str:
    """A human-readable run summary: the metrics block plus guardrail lines.

    When ``benchmark`` is supplied, appends a side-by-side total-return line
    comparing the strategy to the (unconstrained) benchmark run. ``periods_per_year``
    scales the annualized figures (Sharpe/Sortino/Calmar/annualized return) to the
    run's bar frequency; the default of 252.0 keeps daily runs byte-identical.
    """
    metrics = compute(result, periods_per_year)
    lines = [
        f"Symbols:       {', '.join(result.symbols)}",
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
        f"Bars:          {len(result.equity_curve)}",
    ]
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
    return "\n".join(lines)


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
          "halt": {"halted": bool,
                   "halt_ts": iso8601 str | null,
                   "halt_reason": str | null}
        }

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
        "halt": {
            "halted": result.halted,
            "halt_ts": result.halt_ts.isoformat() if result.halt_ts is not None else None,
            "halt_reason": result.halt_reason,
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
