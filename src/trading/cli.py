"""Command-line entry point: ``trading backtest …`` (V1).

Wires the yfinance adapter, simulated broker, chosen strategy, and engine, then
prints a summary and writes the equity curve. ``paper`` mode lands in V5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from trading.broker import SimulatedBroker
from trading.data.yfinance_adapter import YFinanceAdapter
from trading.engine import Engine
from trading.report import summarize, write_equity_csv
from trading.strategies import get_strategy
from trading.types import Portfolio

app = typer.Typer(add_completion=False, help="Algorithmic trading test bench.")


@app.callback()
def main() -> None:
    """Algorithmic trading test bench. Use a subcommand (e.g. `backtest`)."""
    # Present so `backtest` (and later `paper`, V5) stay named subcommands rather
    # than Typer collapsing a lone command into the root.


def _parse_date(label: str, value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        typer.echo(f"error: {label} must be YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def backtest(
    strategy: str = typer.Option(..., "--strategy", "-s", help="Registered strategy name."),
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated tickers, e.g. AAPL,MSFT."),
    from_: str = typer.Option(..., "--from", help="Start date, YYYY-MM-DD."),
    to: str = typer.Option(..., "--to", help="End date, YYYY-MM-DD."),
    cash: float = typer.Option(1_000.0, "--cash", help="Starting cash."),
    cache_dir: Path = typer.Option(Path(".cache/data"), "--cache-dir"),
    out: Path = typer.Option(Path("results/equity_curve.csv"), "--out"),
) -> None:
    """Backtest a strategy over historical adjusted daily bars."""
    start = _parse_date("--from", from_)
    end = _parse_date("--to", to)
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        typer.echo("error: --symbols is empty", err=True)
        raise typer.Exit(2)

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    adapter = YFinanceAdapter(cache_dir)
    broker = SimulatedBroker(Portfolio(cash=cash))
    result = Engine(adapter, broker).run(strat, tickers, start, end)

    typer.echo(summarize(result))
    write_equity_csv(result, out)
    typer.echo(f"\nWrote equity curve to {out}")


if __name__ == "__main__":
    app()
