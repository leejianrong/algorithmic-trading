"""Minimal V1 reporting: a text summary and an equity-curve CSV.

Full metrics — Sharpe, max drawdown, exposure, benchmark — arrive in V4. V1
reports only what the buy-and-hold acceptance test needs: final equity, total
return, and the curve itself.
"""

from __future__ import annotations

import csv
from pathlib import Path

from trading.engine import BacktestResult


def summarize(result: BacktestResult) -> str:
    """A short human-readable run summary."""
    lines = [
        f"Symbols:       {', '.join(result.symbols)}",
        f"Starting cash: ${result.starting_cash:,.2f}",
        f"Final equity:  ${result.final_equity:,.2f}",
        f"Total return:  {result.total_return * 100:+.2f}%",
        f"Bars:          {len(result.equity_curve)}",
    ]
    if result.rejections:
        lines.append(f"Rejected:      {len(result.rejections)} order(s)")
    return "\n".join(lines)


def write_equity_csv(result: BacktestResult, path: Path) -> None:
    """Write the equity curve as ``ts,equity`` rows (one per trading day)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "equity"])
        for point in result.equity_curve:
            writer.writerow([point.ts.isoformat(), f"{point.equity:.6f}"])
