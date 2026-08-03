"""Command-line entry point: ``trading backtest …`` and ``trading gen-data …``.

Wires a data source (yfinance or the offline synthetic generator), the simulated
broker, the chosen strategy, and the engine, then prints a summary and writes the
equity curve. ``paper`` mode lands in V5.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import typer

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.data.yfinance_adapter import YFinanceAdapter, cache_filename
from trading.engine import Engine
from trading.interfaces import DataAdapter
from trading.report import summarize, write_equity_csv
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Portfolio

app = typer.Typer(add_completion=False, help="Algorithmic trading test bench.")


@app.callback()
def main() -> None:
    """Algorithmic trading test bench. Use a subcommand (e.g. `backtest`)."""
    # Present so `backtest`/`gen-data` (and later `paper`, V5) stay named
    # subcommands rather than Typer collapsing a lone command into the root.


def _parse_date(label: str, value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        typer.echo(f"error: {label} must be YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(2) from exc


def _parse_symbols(symbols: str) -> list[str]:
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        typer.echo("error: --symbols is empty", err=True)
        raise typer.Exit(2)
    return tickers


def _make_adapter(source: str, cache_dir: Path, seed: int) -> DataAdapter:
    if source == "yfinance":
        return YFinanceAdapter(cache_dir)
    if source == "synthetic":
        return SyntheticAdapter(seed=seed)
    typer.echo(f"error: --source must be 'yfinance' or 'synthetic', got {source!r}", err=True)
    raise typer.Exit(2)


@app.command()
def backtest(
    strategy: str = typer.Option(..., "--strategy", "-s", help="Registered strategy name."),
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated tickers, e.g. AAPL,MSFT."),
    from_: str = typer.Option(..., "--from", help="Start date, YYYY-MM-DD."),
    to: str = typer.Option(..., "--to", help="End date, YYYY-MM-DD."),
    source: str = typer.Option("yfinance", "--source", help="Data source: yfinance | synthetic."),
    seed: int = typer.Option(0, "--seed", help="RNG seed when --source synthetic."),
    cash: float = typer.Option(1_000.0, "--cash", help="Starting cash."),
    max_position: float = typer.Option(
        0.25, "--max-position", help="Per-symbol position cap, fraction of equity."
    ),
    max_gross: float = typer.Option(
        1.0, "--max-gross", help="Max gross exposure, fraction of equity."
    ),
    max_drawdown: float = typer.Option(
        0.20, "--max-drawdown", help="Drawdown kill-switch threshold, fraction from peak."
    ),
    no_guardrails: bool = typer.Option(
        False, "--no-guardrails", help="Disable risk guardrails (fully permissive)."
    ),
    cache_dir: Path = typer.Option(Path(".cache/data"), "--cache-dir"),
    out: Path = typer.Option(Path("results/equity_curve.csv"), "--out"),
) -> None:
    """Backtest a strategy over adjusted daily bars (real or synthetic)."""
    start = _parse_date("--from", from_)
    end = _parse_date("--to", to)
    tickers = _parse_symbols(symbols)

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        risk = (
            RiskConfig.unlimited()
            if no_guardrails
            else RiskConfig(
                max_position_pct=max_position,
                max_gross_exposure=max_gross,
                max_drawdown_pct=max_drawdown,
            )
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    adapter = _make_adapter(source, cache_dir, seed)
    broker = SimulatedBroker(Portfolio(cash=cash))
    result = Engine(adapter, broker, Guardrails(risk)).run(strat, tickers, start, end)

    typer.echo(summarize(result))
    write_equity_csv(result, out)
    typer.echo(f"\nWrote equity curve to {out}")


@app.command(name="gen-data")
def gen_data(
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated tickers to generate."),
    from_: str = typer.Option(..., "--from", help="Start date, YYYY-MM-DD."),
    to: str = typer.Option(..., "--to", help="End date, YYYY-MM-DD."),
    seed: int = typer.Option(0, "--seed", help="RNG seed (same seed → same data)."),
    out_dir: Path = typer.Option(Path(".cache/data"), "--out-dir", help="Where to write CSVs."),
) -> None:
    """Write deterministic synthetic daily bars to CSV, one file per symbol.

    Files use the same naming and columns as the yfinance cache, so a subsequent
    `backtest --source yfinance --cache-dir <out-dir>` reads them offline.
    """
    start = _parse_date("--from", from_)
    end = _parse_date("--to", to)
    tickers = _parse_symbols(symbols)

    adapter = SyntheticAdapter(seed=seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    for symbol in tickers:
        bars = adapter.get_bars(symbol, start, end)
        path = out_dir / cache_filename(symbol, start, end)
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ts", "open", "high", "low", "close", "volume"])
            for bar in bars:
                writer.writerow(
                    [bar.ts.date().isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]
                )
        typer.echo(f"wrote {len(bars)} bars to {path}")


if __name__ == "__main__":
    app()
