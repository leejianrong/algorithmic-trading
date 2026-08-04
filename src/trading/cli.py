"""Command-line entry point: ``backtest``, ``paper``, ``sweep``, ``dashboard``, and more.

Wires a data source (yfinance or the offline synthetic generator), the simulated
broker, the chosen strategy, and the engine, then prints a summary and writes the
equity curve. ``backtest`` iterates a historical range; ``paper`` (V5) drives the
*same* engine/broker/guardrails over a completed-bar feed on a clock (ADR-0014),
logging each new bar's decision, fills, guardrail actions, and equity.

Three honesty knobs sit on top of that, all opt-in and all off by default:

- ``sweep --folds N`` runs a **true** in-sample -> out-of-sample walk-forward
  (ADR-0026): each fold tunes the grid on IS data, then runs the single winner
  **once** on untouched OOS data, and the summary leads with the OOS figures and
  the IS->OOS degradation. Distinct from ``--windows``, which runs the whole grid
  on every window and is therefore all in-sample.
- ``backtest --min-adv`` screens the universe by average dollar volume measured
  **before** the backtest starts (ADR-0029), so the liquidity decision cannot see
  volume the strategy hasn't reached. Every dropped symbol is printed.
- ``verify-universe`` asks the broker which symbols are actually tradable and
  fractionable (ADR-0028), instead of trusting a curated basket.

The trades-per-parameter sample-size check is wired automatically: every run
reports its entry count, and a run with too few trades for its number of tunable
parameters says so (ADR-0029).
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from trading.broker import SimulatedBroker
from trading.brokers.alpaca import AlpacaBroker
from trading.clock import FakeClock, WallClock
from trading.config import RiskConfig
from trading.data.alpaca_adapter import AlpacaAdapter
from trading.data.csv_adapter import CsvAdapter
from trading.data.fake import FakeAdapter
from trading.data.recent_window import (
    RecentWindowFeed,
    default_is_complete,
    interval_is_complete,
)
from trading.data.synthetic import SyntheticAdapter
from trading.data.yfinance_adapter import YFinanceAdapter, cache_filename
from trading.engine import DEFAULT_PAPER_LOOKBACK, BacktestResult, BarOutcome, Engine, PaperSession
from trading.frequency import DAILY, Frequency
from trading.interfaces import Broker, DataAdapter
from trading.liquidity import DEFAULT_FORMATION_DAYS, screen_by_adv
from trading.metrics import compute as compute_metrics
from trading.report import summarize, write_equity_csv, write_equity_png, write_result_json
from trading.risk import Guardrails
from trading.strategies import free_parameter_count, get_strategy
from trading.sweep import SweepSummary, WalkForwardSummary, run_sweep, run_walk_forward
from trading.types import Portfolio
from trading.universe import get_sector_map, get_universe, validate_universe

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
    # `@name` expands a curated basket (universe.py); a plain comma list is verbatim.
    if symbols.startswith("@"):
        name = symbols[1:].strip()
        try:
            return get_universe(name)
        except KeyError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        typer.echo("error: --symbols is empty", err=True)
        raise typer.Exit(2)
    return tickers


def _apply_liquidity_screen(
    adapter: DataAdapter,
    tickers: list[str],
    start: datetime,
    min_adv: float,
    formation_days: int,
) -> list[str]:
    """Screen ``tickers`` by pre-backtest ADV, print the verdict, return the keepers.

    The screen reads only bars from before ``start`` (ADR-0029), so it cannot leak
    future volume into the universe decision. Every dropped symbol is printed with
    its reason; a screen that removed everything is a hard error (exit 2) rather
    than an empty run that looks like a strategy that never traded.
    """
    screen = screen_by_adv(adapter, tickers, start, min_adv=min_adv, formation_days=formation_days)
    typer.echo(screen.describe() + "\n")
    if not screen.kept:
        typer.echo(
            f"error: no symbol met the ${min_adv:,.0f} ADV floor — "
            "lower --min-adv or widen --symbols",
            err=True,
        )
        raise typer.Exit(2)
    return screen.kept


def _parse_frequency(interval: str) -> Frequency:
    """Resolve ``--interval`` (e.g. ``1d``/``1h``/``30m``) or exit 2 on a bad label."""
    try:
        return Frequency.parse(interval)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _make_adapter(
    source: str, cache_dir: Path, seed: int, frequency: Frequency = DAILY
) -> DataAdapter:
    if source == "yfinance":
        if frequency.is_intraday:
            typer.echo(
                f"error: --source yfinance is daily-only; the {frequency.label!r} interval "
                "needs --source alpaca or synthetic (raw intraday bars).",
                err=True,
            )
            raise typer.Exit(2)
        return YFinanceAdapter(cache_dir)
    if source == "synthetic":
        return SyntheticAdapter(seed=seed, frequency=frequency)
    if source == "csv":
        if frequency.is_intraday:
            typer.echo(
                f"error: --source csv is daily-only; the {frequency.label!r} interval "
                "needs --source alpaca or synthetic (raw intraday bars).",
                err=True,
            )
            raise typer.Exit(2)
        # Bring-your-own-data: reads <cache_dir>/<SYMBOL>.csv in the standard schema.
        return CsvAdapter(cache_dir)
    if source == "alpaca":
        try:
            return AlpacaAdapter(interval=frequency.delta)
        except (ValueError, ImportError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
    typer.echo(
        f"error: --source must be 'yfinance', 'synthetic', 'csv', or 'alpaca', got {source!r}",
        err=True,
    )
    raise typer.Exit(2)


def _parse_sector_map(spec: str) -> dict[str, str] | None:
    """Parse ``SYM:sector,SYM:sector`` into a symbol->sector map (None if empty).

    ``@name`` instead loads a curated basket's map (universe.py). An unknown basket
    is re-raised as ValueError so the caller's existing ``except ValueError`` path
    surfaces it as a clean CLI error (exit 2), matching the malformed-spec case.
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec.startswith("@"):
        try:
            return get_sector_map(spec[1:].strip())
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
    result: dict[str, str] = {}
    for pair in spec.split(","):
        symbol, sep, sector = pair.partition(":")
        symbol, sector = symbol.strip().upper(), sector.strip()
        if not sep or not symbol or not sector:
            raise ValueError(f"--sector-map entry {pair!r} must look like SYMBOL:sector")
        result[symbol] = sector
    return result


def _build_risk(
    *,
    no_guardrails: bool,
    max_position: float,
    max_gross: float,
    max_drawdown: float,
    target_vol: float | None,
    sector_map: str,
    max_sector_exposure: float | None,
) -> RiskConfig:
    """Assemble the run's RiskConfig, or the permissive opt-out when disabled.

    Raises ValueError (surfaced as a clean CLI error by the caller) on an invalid
    limit or a malformed --sector-map.
    """
    if no_guardrails:
        return RiskConfig.unlimited()
    return RiskConfig(
        max_position_pct=max_position,
        max_gross_exposure=max_gross,
        max_drawdown_pct=max_drawdown,
        target_volatility=target_vol,
        sector_map=_parse_sector_map(sector_map),
        max_sector_exposure=max_sector_exposure,
    )


def _make_paper_broker(name: str, live: bool, cash: float) -> Broker:
    """Select the paper execution venue: the simulator, or the live Alpaca broker.

    Alpaca is real paper trading, so it requires --live and valid credentials; a
    missing key or the absent SDK surfaces as a clean CLI error, not a traceback.
    """
    if name == "simulated":
        return SimulatedBroker(Portfolio(cash=cash))
    if name == "alpaca":
        if not live:
            typer.echo("error: --broker alpaca requires --live (real paper trading).", err=True)
            raise typer.Exit(2)
        try:
            return AlpacaBroker(clock=WallClock())
        except (ValueError, ImportError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
    typer.echo(f"error: --broker must be 'simulated' or 'alpaca', got {name!r}", err=True)
    raise typer.Exit(2)


@app.command()
def backtest(
    strategy: str = typer.Option(..., "--strategy", "-s", help="Registered strategy name."),
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated tickers, e.g. AAPL,MSFT."),
    from_: str = typer.Option(..., "--from", help="Start date, YYYY-MM-DD."),
    to: str = typer.Option(..., "--to", help="End date, YYYY-MM-DD."),
    source: str = typer.Option(
        "yfinance", "--source", help="Data source: yfinance | synthetic | csv | alpaca."
    ),
    interval: str = typer.Option(
        "1d",
        "--interval",
        help="Bar frequency: 1d | 1h | 30m | 5m | 1m. Sub-daily needs --source alpaca|synthetic.",
    ),
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
    target_vol: float | None = typer.Option(
        None,
        "--target-vol",
        help="Annualized volatility target (e.g. 0.10) that scales gross exposure; off by default.",
    ),
    max_sector_exposure: float | None = typer.Option(
        None,
        "--max-sector-exposure",
        help="Per-sector gross cap as a fraction of equity (needs --sector-map); off by default.",
    ),
    sector_map: str = typer.Option(
        "",
        "--sector-map",
        help="Symbol->sector map as SYM:sector,SYM:sector (used with --max-sector-exposure).",
    ),
    benchmark: str = typer.Option(
        "", "--benchmark", help="Benchmark symbol for a buy-and-hold comparison, e.g. SPY."
    ),
    min_adv: float | None = typer.Option(
        None,
        "--min-adv",
        help=(
            "Liquidity floor: drop symbols whose average dollar volume BEFORE --from "
            "is under this (e.g. 20000000). Off by default."
        ),
    ),
    adv_window: int = typer.Option(
        DEFAULT_FORMATION_DAYS,
        "--adv-window",
        help="Calendar days of pre-backtest history the --min-adv screen measures over.",
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
    freq = _parse_frequency(interval)

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        risk = _build_risk(
            no_guardrails=no_guardrails,
            max_position=max_position,
            max_gross=max_gross,
            max_drawdown=max_drawdown,
            target_vol=target_vol,
            sector_map=sector_map,
            max_sector_exposure=max_sector_exposure,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    adapter = _make_adapter(source, cache_dir, seed, freq)
    if min_adv is not None:
        tickers = _apply_liquidity_screen(adapter, tickers, start, min_adv, adv_window)
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

    # The strategy's tunable-argument count turns on the trades-per-parameter
    # sample-size check and its warning (ADR-0029).
    free_params = free_parameter_count(strat)
    typer.echo(
        summarize(
            result,
            bench_result,
            periods_per_year=freq.periods_per_year,
            free_parameters=free_params,
        )
    )
    write_equity_csv(result, out, bench_result)
    typer.echo(f"\nWrote equity curve to {out}")

    # The canonical machine-readable artifact the dashboard consumes, alongside
    # the CSV. Metrics are computed once here at the run's frequency (default
    # 252/yr for daily keeps the numbers identical).
    metrics = compute_metrics(result, freq.periods_per_year, free_parameters=free_params)
    result_json = out.parent / "result.json"
    write_result_json(
        result,
        result_json,
        mode="backtest",
        frequency=freq.label,
        metrics=metrics,
        benchmark_curve=bench_result.equity_curve if bench_result is not None else None,
    )
    typer.echo(f"Wrote result JSON to {result_json}")
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
    source: str = typer.Option(
        "yfinance", "--source", help="Data source: yfinance | synthetic | csv | alpaca."
    ),
    interval: str = typer.Option(
        "1d",
        "--interval",
        help="Bar frequency: 1d | 1h | 30m | 5m | 1m. Sub-daily needs --source alpaca|synthetic.",
    ),
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
    target_vol: float | None = typer.Option(
        None,
        "--target-vol",
        help="Annualized volatility target (e.g. 0.10) that scales gross exposure; off by default.",
    ),
    max_sector_exposure: float | None = typer.Option(
        None,
        "--max-sector-exposure",
        help="Per-sector gross cap as a fraction of equity (needs --sector-map); off by default.",
    ),
    sector_map: str = typer.Option(
        "",
        "--sector-map",
        help="Symbol->sector map as SYM:sector,SYM:sector (used with --max-sector-exposure).",
    ),
    broker_name: str = typer.Option(
        "simulated",
        "--broker",
        help="Execution venue: simulated | alpaca (alpaca is real paper trading, needs --live).",
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
    freq = _parse_frequency(interval)

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        risk = _build_risk(
            no_guardrails=no_guardrails,
            max_position=max_position,
            max_gross=max_gross,
            max_drawdown=max_drawdown,
            target_vol=target_vol,
            sector_map=sector_map,
            max_sector_exposure=max_sector_exposure,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    adapter = _make_adapter(source, cache_dir, seed, freq)
    broker = _make_paper_broker(broker_name, live, cash)
    engine = Engine(adapter, broker, Guardrails(risk))

    # The clock and feed are the *only* difference between backtest and paper
    # (ADR-0002/0014). Live: wall clock over a recent-window feed, runs until
    # interrupted. Once: materialize the [from, to] bars into an in-memory adapter
    # and a fake clock parked just past the range so every bar reads as complete —
    # the loop drains them one _step at a time and stops, offline and deterministic.
    # Sub-daily bars need the interval-aware completeness policy (ADR-0022); daily
    # keeps the default policy so the daily path stays byte-identical to V5.
    is_complete = interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete
    lookback = DEFAULT_PAPER_LOOKBACK
    run_kwargs: dict[str, int] = {}
    if live:
        clock: WallClock | FakeClock = WallClock()
        feed = RecentWindowFeed(adapter, clock, is_complete)
    else:
        series = {s: adapter.get_bars(s, start, end) for s in tickers}
        all_bars = [bar for bars in series.values() for bar in bars]
        total = len({bar.ts for bar in all_bars})
        lookback = max(DEFAULT_PAPER_LOOKBACK, total + 1)
        clock = FakeClock(end + timedelta(days=1))
        feed = RecentWindowFeed(FakeAdapter(all_bars), clock, is_complete)
        run_kwargs = {"max_empty_polls": 1}

    session = PaperSession(engine, strat, tickers, feed, clock, lookback=lookback, frequency=freq)

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

    # The canonical machine-readable artifact the dashboard consumes, alongside the
    # CSV. Metrics are computed at the run's frequency (default 252/yr for daily).
    free_params = free_parameter_count(strat)
    metrics = compute_metrics(result, freq.periods_per_year, free_parameters=free_params)
    result_json = out / "result.json"
    write_result_json(result, result_json, mode="paper", frequency=freq.label, metrics=metrics)

    typer.echo(
        "\n"
        + summarize(
            result,
            periods_per_year=freq.periods_per_year,
            free_parameters=free_params,
        )
    )
    typer.echo(f"\nProcessed {len(session.session_log)} completed bar(s).")
    typer.echo(
        f"Session log: {log_path}\nRunning state: {state_path}\n"
        f"Equity curve: {csv_path}\nResult JSON: {result_json}"
    )


def _coerce_param_value(token: str) -> object:
    """Coerce one grid value token to int, else float, else leave it a string.

    ``"5"`` -> ``5`` (int), ``"0.95"`` -> ``0.95`` (float), ``"fast"`` -> ``"fast"``.
    Integer-looking tokens stay ints so parameters like ``fast``/``slow`` reach the
    strategy as the ints it expects.
    """
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def _parse_grid(params: list[str]) -> dict[str, list[object]]:
    """Parse repeatable ``--param name=v1,v2,...`` options into a grid.

    Each option contributes one axis; values are comma-separated and coerced by
    :func:`_coerce_param_value`. A malformed spec (no ``=``, empty name, or no
    values) or a duplicated name exits with code 2.
    """
    grid: dict[str, list[object]] = {}
    for spec in params:
        name, sep, raw = spec.partition("=")
        name = name.strip()
        if not sep or not name:
            typer.echo(f"error: --param must be name=v1,v2,..., got {spec!r}", err=True)
            raise typer.Exit(2)
        values = [_coerce_param_value(v.strip()) for v in raw.split(",") if v.strip()]
        if not values:
            typer.echo(f"error: --param {name!r} has no values", err=True)
            raise typer.Exit(2)
        if name in grid:
            typer.echo(f"error: --param {name!r} given more than once", err=True)
            raise typer.Exit(2)
        grid[name] = values
    return grid


# The metric columns written for every sweep run, in order (attr, header).
_SWEEP_METRIC_COLUMNS: list[tuple[str, str]] = [
    ("sharpe", "sharpe"),
    ("total_return", "total_return"),
    ("annualized_return", "annualized_return"),
    ("max_drawdown", "max_drawdown"),
    ("win_rate", "win_rate"),
    ("avg_exposure", "avg_exposure"),
    ("peak_exposure", "peak_exposure"),
]


def _write_sweep_csv(summary: SweepSummary, out: Path, rank_by: str, param_keys: list[str]) -> None:
    """Write the ranked sweep results to ``out`` — one row per run, best first."""
    out.parent.mkdir(parents=True, exist_ok=True)
    ranked = summary.ranked(by=rank_by)
    header = ["rank", *param_keys, "window", "win_start", "win_end"] + [
        name for _attr, name in _SWEEP_METRIC_COLUMNS
    ]
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for rank, run in enumerate(ranked, start=1):
            row: list[object] = [rank]
            row.extend(run.params.get(key, "") for key in param_keys)
            row.extend([run.window, run.start.date().isoformat(), run.end.date().isoformat()])
            row.extend(
                round(getattr(run.metrics, attr), 6) for attr, _name in _SWEEP_METRIC_COLUMNS
            )
            writer.writerow(row)


def _format_sweep_table(summary: SweepSummary, rank_by: str, param_keys: list[str]) -> str:
    """Render the ranked runs as a plain-text table, ordered by ``rank_by``."""
    ranked = summary.ranked(by=rank_by)
    headers = ["rank", *param_keys, "window", "sharpe", "total_return", "max_drawdown"]
    rows: list[list[str]] = []
    for rank, run in enumerate(ranked, start=1):
        cells = [str(rank)]
        cells.extend(_format_param(run.params.get(key)) for key in param_keys)
        cells.append(str(run.window))
        cells.append(f"{run.metrics.sharpe:.3f}")
        cells.append(f"{run.metrics.total_return * 100:.2f}%")
        cells.append(f"{run.metrics.max_drawdown * 100:.2f}%")
        rows.append(cells)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)


def _format_param(value: object) -> str:
    """Compact rendering of one parameter value for the table (empty for None)."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


@app.command()
def sweep(
    strategy: str = typer.Option(..., "--strategy", "-s", help="Registered strategy name."),
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated tickers, e.g. AAA,BBB."),
    from_: str = typer.Option(..., "--from", help="Start date, YYYY-MM-DD."),
    to: str = typer.Option(..., "--to", help="End date, YYYY-MM-DD."),
    param: list[str] = typer.Option(
        [], "--param", "-p", help="Repeatable grid axis: name=v1,v2,... (e.g. fast=5,10,20)."
    ),
    rank_by: str = typer.Option(
        "sharpe", "--rank-by", help="Ranking metric: sharpe | total_return."
    ),
    windows: int = typer.Option(
        1,
        "--windows",
        help=(
            "Plain per-window sweep: split [from, to] into N windows and run the whole "
            "grid on each (1 = off). This is all in-sample — see --folds for real "
            "out-of-sample validation."
        ),
    ),
    folds: int = typer.Option(
        0,
        "--folds",
        help=(
            "TRUE walk-forward (ADR-0026): N folds, each tuning the grid in-sample then "
            "running the single winner ONCE out-of-sample. 0 = off."
        ),
    ),
    wf_mode: str = typer.Option(
        "anchored",
        "--wf-mode",
        help="Walk-forward in-sample window: anchored (expanding) | rolling (sliding).",
    ),
    source: str = typer.Option(
        "yfinance", "--source", help="Data source: yfinance | synthetic | csv | alpaca."
    ),
    interval: str = typer.Option(
        "1d",
        "--interval",
        help="Bar frequency: 1d | 1h | 30m | 5m | 1m. Sub-daily needs --source alpaca|synthetic.",
    ),
    seed: int = typer.Option(0, "--seed", help="RNG seed when --source synthetic."),
    cash: float = typer.Option(1_000.0, "--cash", help="Starting cash per run."),
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
    target_vol: float | None = typer.Option(
        None,
        "--target-vol",
        help="Annualized volatility target (e.g. 0.10) that scales gross exposure; off by default.",
    ),
    max_sector_exposure: float | None = typer.Option(
        None,
        "--max-sector-exposure",
        help="Per-sector gross cap as a fraction of equity (needs --sector-map); off by default.",
    ),
    sector_map: str = typer.Option(
        "",
        "--sector-map",
        help="Symbol->sector map as SYM:sector,SYM:sector (used with --max-sector-exposure).",
    ),
    cache_dir: Path = typer.Option(Path(".cache/data"), "--cache-dir"),
    out: Path = typer.Option(Path("results/sweep.csv"), "--out", help="Results CSV path."),
) -> None:
    """Grid-sweep a strategy's parameters over a date range, ranked by a metric.

    Runs the backtest engine once per parameter combination (an OUTER loop, not an
    engine feature — ADR-0016), computes the same metrics as ``backtest``, prints a
    ranked table, and writes a results CSV. Deterministic and offline-capable with
    ``--source synthetic``. ``--windows N`` adds a simple per-window walk-forward.
    """
    start = _parse_date("--from", from_)
    end = _parse_date("--to", to)
    tickers = _parse_symbols(symbols)
    freq = _parse_frequency(interval)
    grid = _parse_grid(param)
    if rank_by not in {"sharpe", "total_return"}:
        typer.echo(
            f"error: --rank-by must be 'sharpe' or 'total_return', got {rank_by!r}", err=True
        )
        raise typer.Exit(2)

    if strategy not in _known_strategy_names():
        typer.echo(
            f"error: unknown strategy {strategy!r}; known: {', '.join(_known_strategy_names())}",
            err=True,
        )
        raise typer.Exit(2)

    try:
        risk = _build_risk(
            no_guardrails=no_guardrails,
            max_position=max_position,
            max_gross=max_gross,
            max_drawdown=max_drawdown,
            target_vol=target_vol,
            sector_map=sector_map,
            max_sector_exposure=max_sector_exposure,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if folds > 0 and windows > 1:
        typer.echo(
            "error: --folds (true walk-forward) and --windows (plain per-window sweep) "
            "are different validation schemes; pass only one",
            err=True,
        )
        raise typer.Exit(2)
    if wf_mode not in {"anchored", "rolling"}:
        typer.echo(f"error: --wf-mode must be 'anchored' or 'rolling', got {wf_mode!r}", err=True)
        raise typer.Exit(2)

    adapter = _make_adapter(source, cache_dir, seed, freq)

    if folds > 0:
        _run_walk_forward_command(
            strategy=strategy,
            grid=grid,
            adapter=adapter,
            tickers=tickers,
            start=start,
            end=end,
            folds=folds,
            mode=wf_mode,
            rank_by=rank_by,
            cash=cash,
            risk=risk,
            out=out,
        )
        return

    summary = run_sweep(
        strategy, grid, adapter, tickers, start, end, cash=cash, risk=risk, windows=windows
    )

    param_keys = list(grid)
    if not summary.runs:
        typer.echo("No runs produced — every parameter combination was invalid.")
    else:
        typer.echo(
            f"Sweep: strategy={strategy} symbols={','.join(tickers)} "
            f"combos={len(summary.runs)} ranked by {rank_by}\n"
        )
        typer.echo(_format_sweep_table(summary, rank_by, param_keys))
        _write_sweep_csv(summary, out, rank_by, param_keys)
        typer.echo(f"\nWrote sweep results to {out}")

    for combo, reason in summary.skipped:
        pretty = ", ".join(f"{k}={_format_param(v)}" for k, v in combo.items())
        typer.echo(f"skipped {{{pretty}}}: {reason}")


@app.command("verify-universe")
def verify_universe(
    symbols: str = typer.Option(
        ..., "--symbols", help="Comma-separated tickers or @basket (e.g. @blue20)."
    ),
) -> None:
    """Check a universe against the broker: tradable AND fractionable? (ADR-0028)

    A curated basket (ADR-0024) is a judgement call; only the broker knows what it
    will actually trade. This asks Alpaca per symbol and prints the usable set plus
    every dropped name with its reason. Symbols whose lookup *failed* are reported
    as unverified — unknown, not rejected — and still excluded, because a name you
    could not confirm is one you cannot size a real order in.

    Needs ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` and the optional ``alpaca-py``
    SDK; it targets the paper endpoint. Exits 1 when the universe is not clean, so
    a script can gate on it.
    """
    tickers = _parse_symbols(symbols)
    try:
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
    except (ImportError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    result = validate_universe(tickers, client)
    for line in result.report_lines():
        typer.echo(line)
    if not result.is_clean:
        raise typer.Exit(1)


def _known_strategy_names() -> list[str]:
    """Sorted registry names, for a friendly error before running the sweep."""
    from trading.strategies import STRATEGIES

    return sorted(STRATEGIES)


def _write_walk_forward_csv(summary: WalkForwardSummary, path: Path) -> None:
    """One row per fold: its spans, winning params, and IS vs OOS metrics.

    The out-of-sample columns are prefixed ``oos_`` and the in-sample ones ``is_``
    so a reader can never mistake a tuned number for a validated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "fold",
                "is_start",
                "is_end",
                "oos_start",
                "oos_end",
                "params",
                "is_sharpe",
                "is_total_return",
                "oos_sharpe",
                "oos_total_return",
                "oos_max_drawdown",
            ]
        )
        for fold in summary.folds:
            params = ", ".join(f"{k}={_format_param(v)}" for k, v in fold.params.items())
            writer.writerow(
                [
                    fold.index,
                    fold.is_start.date().isoformat(),
                    fold.is_end.date().isoformat(),
                    fold.oos_start.date().isoformat(),
                    fold.oos_end.date().isoformat(),
                    params,
                    f"{fold.in_sample_metrics.sharpe:.4f}",
                    f"{fold.in_sample_metrics.total_return:.6f}",
                    f"{fold.out_of_sample_metrics.sharpe:.4f}",
                    f"{fold.out_of_sample_metrics.total_return:.6f}",
                    f"{fold.out_of_sample_metrics.max_drawdown:.6f}",
                ]
            )


def _run_walk_forward_command(
    *,
    strategy: str,
    grid: dict[str, list[object]],
    adapter: DataAdapter,
    tickers: list[str],
    start: datetime,
    end: datetime,
    folds: int,
    mode: str,
    rank_by: str,
    cash: float,
    risk: RiskConfig,
    out: Path,
) -> None:
    """Run and print a true in-sample -> out-of-sample walk-forward (ADR-0026).

    Prints one line per fold (which parameters in-sample picked, and how they then
    did out-of-sample) followed by the aggregate the whole exercise exists to
    produce: mean OOS performance and the IS->OOS degradation. The out-of-sample
    figures are the honest ones; the in-sample figures are shown only so the gap
    between them is visible.
    """
    summary = run_walk_forward(
        strategy,
        grid,
        adapter,
        tickers,
        start,
        end,
        folds=folds,
        mode=mode,
        rank_by=rank_by,
        cash=cash,
        risk=risk,
    )

    typer.echo(
        f"Walk-forward: strategy={strategy} symbols={','.join(tickers)} "
        f"folds={summary.fold_count} mode={mode} tuned on {rank_by}\n"
    )
    if not summary.folds:
        typer.echo("No folds produced — nothing was validated.")
    else:
        for fold in summary.folds:
            params = ", ".join(f"{k}={_format_param(v)}" for k, v in fold.params.items())
            typer.echo(
                f"fold {fold.index}  "
                f"IS {fold.is_start.date()}..{fold.is_end.date()} -> "
                f"OOS {fold.oos_start.date()}..{fold.oos_end.date()}  "
                f"[{params or 'defaults'}]  "
                f"IS sharpe {fold.in_sample_metrics.sharpe:+.2f} -> "
                f"OOS sharpe {fold.out_of_sample_metrics.sharpe:+.2f}"
            )
        retention = summary.sharpe_retention
        retention_text = "n/a" if retention is None else f"{retention * 100:.0f}%"
        typer.echo(
            f"\nOUT-OF-SAMPLE mean sharpe {summary.mean_out_of_sample_sharpe:+.2f} "
            f"(in-sample {summary.mean_in_sample_sharpe:+.2f}; "
            f"degradation {summary.sharpe_degradation:+.2f}, retained {retention_text})"
        )
        typer.echo(
            f"{summary.folds_with_positive_out_of_sample_return}/{summary.fold_count} "
            "fold(s) profitable out of sample — this is the number that counts; "
            "the in-sample figures are tuned and always flatter."
        )
        _write_walk_forward_csv(summary, out)
        typer.echo(f"\nWrote walk-forward results to {out}")

    for combo, reason in summary.skipped:
        pretty = ", ".join(f"{k}={_format_param(v)}" for k, v in combo.items())
        typer.echo(f"skipped {{{pretty}}}: {reason}")
    for warning in summary.warnings:
        typer.echo(f"warning: {warning}", err=True)


@app.command()
def dashboard(
    result: Path = typer.Option(
        Path("results/result.json"),
        "--result",
        help="Path to the run's result.json (written by `backtest`/`paper`).",
    ),
    static: Path | None = typer.Option(
        None,
        "--static",
        help="Render a self-contained HTML dashboard to this path (pure offline, no deps).",
    ),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Serve the dashboard over HTTP (needs the optional 'dashboard' extra).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for --serve."),
    port: int = typer.Option(8000, "--port", help="Bind port for --serve."),
) -> None:
    """Visualize a run's result.json: a static HTML export or a live server.

    Exactly one of ``--static`` / ``--serve`` per invocation. ``--static`` writes a
    single self-contained HTML file (no external references, no extra dependencies).
    ``--serve`` runs the FastAPI dashboard server — install it with
    ``pip install 'algo-trading-bench[dashboard]'``.
    """
    from trading.dashboard import server as dashboard_server
    from trading.dashboard.payload import load_payload
    from trading.dashboard.static_export import write_html

    if (static is not None) == serve:
        typer.echo("error: pass exactly one of --static or --serve", err=True)
        raise typer.Exit(2)

    if static is not None:
        try:
            payload = load_payload(result)
        except FileNotFoundError as exc:
            typer.echo(f"error: result.json not found at {result}", err=True)
            raise typer.Exit(2) from exc
        except (ValueError, KeyError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
        written = write_html(payload, static)
        typer.echo(f"Wrote dashboard HTML to {written}")
        return

    # --serve: hand off to the (lazy-FastAPI) server; a missing extra raises a clear
    # ImportError naming the install, which we surface as a clean CLI error.
    try:
        typer.echo(f"Serving dashboard for {result} at http://{host}:{port} (Ctrl-C to stop)")
        dashboard_server.serve(result, host=host, port=port)
    except ImportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
