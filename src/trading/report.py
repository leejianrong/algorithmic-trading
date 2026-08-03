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
from pathlib import Path
from typing import TYPE_CHECKING

from trading.metrics import compute

if TYPE_CHECKING:
    from trading.engine import BacktestResult, EquityPoint


def summarize(result: BacktestResult, benchmark: BacktestResult | None = None) -> str:
    """A human-readable run summary: the metrics block plus guardrail lines.

    When ``benchmark`` is supplied, appends a side-by-side total-return line
    comparing the strategy to the (unconstrained) benchmark run.
    """
    metrics = compute(result)
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
        bench_return = compute(benchmark).total_return
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
