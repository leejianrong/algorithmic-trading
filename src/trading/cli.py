"""Command-line entry point: ``trading backtest``, ``paper``, and ``gen-data``.

Wires a data source (yfinance or the offline synthetic generator), the simulated
broker, the chosen strategy, and the engine, then prints a summary and writes the
equity curve. ``backtest`` iterates a historical range; ``paper`` (V5) drives the
*same* engine/broker/guardrails over a completed-bar feed on a clock (ADR-0014),
logging each new bar's decision, fills, guardrail actions, and equity.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from trading.broker import SimulatedBroker
from trading.clock import FakeClock, WallClock
from trading.config import RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.recent_window import RecentWindowFeed
from trading.data.synthetic import SyntheticAdapter
from trading.data.yfinance_adapter import YFinanceAdapter, cache_filename
from trading.engine import DEFAULT_PAPER_LOOKBACK, BacktestResult, BarOutcome, Engine, PaperSession
from trading.interfaces import DataAdapter
from trading.report import summarize, write_equity_csv, write_equity_png
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Portfolio

app = typer.Typer(add_completion=False, help="Algorithmic trading test bench.")


@app.callback()
def main() -> None:
    """Algorithmic trading test bench. Use a subcommand (e.g. `backtest`)."""
    # Present so `backtest`/`paper`/`gen-data` stay named subcommands rather than
    # Typer collapsing a lone command into the root.


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
    benchmark: str = typer.Option(
        "", "--benchmark", help="Benchmark symbol for a buy-and-hold comparison, e.g. SPY."
    ),
    plot: bool = typer.Option(
        False, "--plot/--no-plot", help="Also write an equity_curve.png next to the CSV."
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

    # Optional buy-and-hold benchmark on the same dates/source, run UNCONSTRAINED
    # (unlimited guardrails) so the benchmark itself is never clamped (Q24).
    bench_result: BacktestResult | None = None
    bench_symbol = benchmark.strip().upper()
    if bench_symbol:
        bench_broker = SimulatedBroker(Portfolio(cash=cash))
        bench_result = Engine(adapter, bench_broker, Guardrails(RiskConfig.unlimited())).run(
            get_strategy("buy_and_hold"), [bench_symbol], start, end
        )

    typer.echo(summarize(result, bench_result))
    write_equity_csv(result, out, bench_result)
    typer.echo(f"\nWrote equity curve to {out}")
    if plot:
        png = out.with_suffix(".png")
        write_equity_png(result, png, bench_result)
        typer.echo(f"Wrote equity plot to {png}")


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


def _format_bar(outcome: BarOutcome) -> str:
    """One human-readable status line for a newly completed paper bar."""
    day = outcome.ts.date().isoformat()
    if outcome.intents:
        decisions = ", ".join(_format_intent(i) for i in outcome.intents)
    else:
        decisions = "(hold)"
    parts = [f"{day}  decision: {decisions}"]
    if outcome.fills:
        fills = "; ".join(
            f"{f.side.value.upper()} {f.qty:.4f} {f.symbol} @ {f.price:.4f}" for f in outcome.fills
        )
        parts.append(f"fills: {fills}")
    for _original, clamped, reason in outcome.clamps:
        parts.append(f"CLAMP {clamped.symbol}→{clamped.qty:.4f} ({reason})")
    for order, reason in outcome.guardrail_rejections:
        parts.append(f"REJECT {order.side.value.upper()} {order.symbol} ({reason})")
    for order, reason in outcome.broker_rejections:
        parts.append(f"REJECT {order.side.value.upper()} {order.symbol} ({reason})")
    if outcome.halted_now:
        parts.append("HALT: kill switch tripped — new entries blocked")
    parts.append(f"equity: ${outcome.equity:,.2f}  exposure: {outcome.exposure * 100:.1f}%")
    return "  |  ".join(parts)


def _format_intent(intent: object) -> str:
    weight = getattr(intent, "weight", None)
    symbol = getattr(intent, "symbol", "?")
    if weight is not None:  # TargetWeight
        return f"target {symbol} {weight * 100:.0f}%"
    side = getattr(intent, "side", None)
    qty = getattr(intent, "qty", None)
    return f"{getattr(side, 'value', side)} {qty} {symbol}"


def _persist_state(path: Path, outcome: BarOutcome, portfolio: Portfolio) -> None:
    """Overwrite the running-state file with the latest equity and positions."""
    state = {
        "ts": outcome.ts.isoformat(),
        "equity": round(outcome.equity, 6),
        "exposure": round(outcome.exposure, 6),
        "cash": round(portfolio.cash, 6),
        "halted": outcome.halted,
        "positions": {
            symbol: {"qty": round(pos.qty, 6), "avg_price": round(pos.avg_price, 6)}
            for symbol, pos in portfolio.positions.items()
        },
    }
    path.write_text(json.dumps(state, indent=2) + "\n")


@app.command()
def paper(
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
    live: bool = typer.Option(
        False,
        "--live/--once",
        help="Live wall-clock paper trading (runs until interrupted) vs. a bounded "
        "offline replay over [from, to] that terminates (default).",
    ),
    cache_dir: Path = typer.Option(Path(".cache/data"), "--cache-dir"),
    out: Path = typer.Option(Path("results/paper"), "--out", help="Result directory."),
) -> None:
    """Paper-trade a strategy on completed daily bars (same engine as backtest).

    The default ``--once`` mode replays completed bars over ``[from, to]`` with a
    fake clock so it runs offline and terminates — ideal for a demo or CI. ``--live``
    uses the wall clock and a recent-window feed and runs indefinitely.
    """
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
    engine = Engine(adapter, broker, Guardrails(risk))

    # The clock and feed are the *only* difference between backtest and paper
    # (ADR-0002/0014). Live: wall clock over a recent-window feed, runs until
    # interrupted. Once: materialize the [from, to] bars into an in-memory adapter
    # and a fake clock parked just past the range so every bar reads as complete —
    # the loop drains them one _step at a time and stops, offline and deterministic.
    lookback = DEFAULT_PAPER_LOOKBACK
    run_kwargs: dict[str, int] = {}
    if live:
        clock: WallClock | FakeClock = WallClock()
        feed = RecentWindowFeed(adapter, clock)
    else:
        series = {s: adapter.get_bars(s, start, end) for s in tickers}
        all_bars = [bar for bars in series.values() for bar in bars]
        total = len({bar.ts for bar in all_bars})
        lookback = max(DEFAULT_PAPER_LOOKBACK, total + 1)
        clock = FakeClock(end + timedelta(days=1))
        feed = RecentWindowFeed(FakeAdapter(all_bars), clock)
        run_kwargs = {"max_empty_polls": 1}

    session = PaperSession(engine, strat, tickers, feed, clock, lookback=lookback)

    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "paper_session.log"
    state_path = out / "paper_state.json"
    mode = "live" if live else "once"
    typer.echo(f"Paper session ({mode}) — strategy={strategy} symbols={','.join(tickers)}\n")

    with log_path.open("w") as log_fh:

        def reporter(outcome: BarOutcome) -> None:
            line = _format_bar(outcome)
            typer.echo(line)
            log_fh.write(line + "\n")
            log_fh.flush()
            _persist_state(state_path, outcome, broker.portfolio)

        result = session.run(reporter=reporter, **run_kwargs)

    csv_path = out / "equity_curve.csv"
    write_equity_csv(result, csv_path)
    typer.echo("\n" + summarize(result))
    typer.echo(f"\nProcessed {len(session.session_log)} completed bar(s).")
    typer.echo(f"Session log: {log_path}\nRunning state: {state_path}\nEquity curve: {csv_path}")


if __name__ == "__main__":
    app()
