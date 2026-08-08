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
- ``--halt-recovery-drawdown`` / ``--halt-cooldown-bars`` let the drawdown kill
  switch **re-arm** instead of latching for the whole run (ADR-0031) — the
  difference between a protected 20-year backtest and one disabled from 2001
  onward. Both off by default, on ``backtest``, ``paper``, and ``sweep``.
- ``paper --divergence`` runs a counterfactual ``SimulatedBroker`` beside the live
  broker and reports where the venue's fills differ from the modelled ones —
  price, slippage in bps, latency, and rejections (ADR-0038). Off by default; a
  run without it is byte-identical to before the flag existed.
- ``backtest --bootstrap`` puts a stationary-block-bootstrap confidence interval
  around the Sharpe, a paired beats-the-benchmark win rate when ``--benchmark``
  ran, and the trial-count deflation (ADR-0039). Off by default because the
  bootstrap is the most expensive thing in the report by an order of magnitude —
  ~2.7 s on a 21-year daily run — and a cost nobody asked for must never be paid
  silently. Without the flag the summary and ``result.json`` are exactly what
  they were before it existed.

``sweep`` needs no flag for its half of ADR-0039: it already ran every trial, so
the winner's deflation is free and prints under the ranking table. A "best of 24"
Sharpe quoted without the 24 is the number this bench exists not to print.

The trades-per-parameter sample-size check is wired automatically: every run
reports its entry count, and a run with too few trades for its number of tunable
parameters says so (ADR-0029).

This module is also the process's only owner of two things a library must never
touch (ADR-0043): the **signal disposition** — ``paper`` installs a SIGTERM handler
for the length of its session so ``docker stop`` / ``systemd stop`` / ``kill`` take
the same finalizing exit Ctrl-C already takes — and **logging configuration**, via
the global ``--log-level`` / ``--log-format`` options on the app callback.
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import signal
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

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
from trading.divergence import (
    NOTION_ADJUSTED,
    NOTION_RAW,
    DivergenceJournal,
    ShadowBroker,
    render_report,
    write_divergence_csv,
)
from trading.engine import (
    DEFAULT_PAPER_LOOKBACK,
    LIVE_SILENCE_TOLERANCE,
    MIN_LIVE_EMPTY_POLLS,
    BacktestResult,
    BarOutcome,
    EmptyUniverseError,
    Engine,
    PaperSession,
    silence_tolerance_polls,
)
from trading.frequency import DAILY, Frequency
from trading.interfaces import Broker, DataAdapter
from trading.liquidity import DEFAULT_FORMATION_DAYS, screen_by_adv
from trading.logging_config import (
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
    LOG_FORMATS,
    configure_logging,
)
from trading.metrics import (
    DEFAULT_BOOTSTRAP_RESAMPLES,
    DEFAULT_BOOTSTRAP_SEED,
    SignificanceReport,
    assess_significance,
    trial_count_note,
)
from trading.metrics import compute as compute_metrics
from trading.report import (
    summarize,
    summarize_significance,
    write_equity_csv,
    write_equity_png,
    write_result_json,
)
from trading.risk import Guardrails
from trading.strategies import free_parameter_count, get_strategy
from trading.sweep import SweepSummary, WalkForwardSummary, run_sweep, run_walk_forward
from trading.types import Portfolio
from trading.universe import get_sector_map, get_universe, validate_universe

app = typer.Typer(add_completion=False, help="Algorithmic trading test bench.")

# Named, not ``__name__``. Running the CLI as ``python -m trading.cli`` — which is
# how the signal tests drive it, and a perfectly ordinary way to run it — makes
# ``__name__`` equal ``"__main__"``, a logger outside the ``trading`` tree and
# therefore outside what ``--log-level`` governs: every record here would silently
# fall back to the root's WARNING threshold and the session's own INFO lines would
# vanish exactly when they are wanted. Observed, not theorised (ADR-0043).
logger = logging.getLogger("trading.cli")


@app.callback()
def main(
    log_level: str = typer.Option(
        DEFAULT_LOG_LEVEL,
        "--log-level",
        help="Log threshold for this bench's own loggers: DEBUG | INFO | WARNING | "
        "ERROR | CRITICAL. Logs go to stderr; the run's report stays on stdout. "
        "Above WARNING it quiets third-party libraries too; below, it does not "
        "make them louder.",
    ),
    log_format: str = typer.Option(
        DEFAULT_LOG_FORMAT,
        "--log-format",
        help=f"Log record format: {' | '.join(LOG_FORMATS)}. JSON lines for a log "
        "shipper reading an unattended session after the fact; text for a human.",
    ),
) -> None:
    """Algorithmic trading test bench. Use a subcommand (e.g. `backtest`).

    Also the single place logging is configured (ADR-0043). It happens here, in the
    entry point, and never at import: a host application that imports ``trading`` as
    a library keeps its own handlers untouched.
    """
    # This callback is also what keeps `backtest`/`paper`/`gen-data` named
    # subcommands rather than Typer collapsing a lone command into the root.
    try:
        configure_logging(log_level, log_format)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


class SessionTerminated(KeyboardInterrupt):
    """SIGTERM, re-delivered as the interrupt the paper loop already ends on.

    Subclassing :class:`KeyboardInterrupt` is the whole trick: the ``except
    KeyboardInterrupt`` that ADR-0033 added for Ctrl-C catches this unchanged, so a
    stopped session and an interrupted one take *one* path to ``finalize()`` rather
    than two that can drift apart. Only the sentence printed differs, and only so an
    operator reading the log afterwards can tell "someone pressed Ctrl-C" from
    "the orchestrator stopped the container".
    """


class _TerminationGuard:
    """Turns the first SIGTERM into an interrupt and ignores every one after it.

    Two questions, answered once each.

    **Raise, or set a flag the loop checks?** Raise. A live session spends almost
    all of its life inside ``Clock.sleep_until``, waiting out a bar interval — five
    minutes on the Monday divergence run, an hour at ``--interval 1h``. A
    cooperative flag is only read when the loop comes back round, so SIGTERM would
    be noticed up to a whole interval later, and Docker sends SIGKILL ten seconds
    after SIGTERM: the flag would lose the artifacts exactly the way no handler at
    all does. Raising interrupts the sleep immediately (PEP 475 retries a signalled
    ``time.sleep`` *unless* the handler raised). The cost is real and accepted: an
    exception from a handler lands at whatever bytecode boundary the interpreter
    happened to reach, so it can surface from inside any call the loop is making.
    That is survivable here only because of the second answer.

    **What about a signal arriving mid-finalization?** It is dropped. Once the loop
    has been left, this guard is disarmed and every further SIGTERM is logged and
    ignored, so writing ``equity_curve.csv`` and ``result.json`` cannot be
    interrupted half way — truncating the artifacts the first signal was honoured in
    order to save would be a strictly worse outcome than taking a moment longer.
    Finalization is bounded work (assembling an in-memory result and writing four
    small files, milliseconds in practice), so the ten-second grace period is not
    close to binding, and ``kill -9`` remains available to an operator who disagrees.
    """

    def __init__(self) -> None:
        self.armed = True
        self.signals = 0

    def handle(self, signum: int, frame: FrameType | None) -> None:
        self.signals += 1
        if not self.armed:
            # Reached only while finalizing (or after it). Logging from a handler is
            # safe here because ``logging``'s locks are reentrant and this is the
            # main thread; the alternative — silence — leaves an operator whose
            # ``kill`` did nothing with no explanation at all.
            logger.warning(
                "signal %s received while finalizing — ignoring so the artifacts are "
                "written whole; use SIGKILL to stop immediately (and lose them)",
                signum,
            )
            return
        self.armed = False
        raise SessionTerminated

    def disarm(self) -> None:
        """Stop honouring SIGTERM: the loop is over and finalization has begun."""
        self.armed = False


@contextlib.contextmanager
def _sigterm_stops_the_session() -> Iterator[_TerminationGuard]:
    """Install the SIGTERM handler for the length of a session, then restore it.

    Scoped to ``paper`` rather than the whole CLI, and installed at the entry point
    rather than on import, for the same reason logging is: a signal disposition is
    process-global state that belongs to whoever owns ``main``. A killed ``backtest``
    is re-runnable from its inputs; a killed live session is gone, and it is the only
    survivorship-free evidence this bench collects (ADR-0027/0035).

    Degrades quietly rather than failing: :func:`signal.signal` raises ``ValueError``
    off the main thread and the platform may not deliver SIGTERM at all, neither of
    which is a reason to refuse to trade. The session then behaves exactly as it did
    before ADR-0043, and says so once.
    """
    guard = _TerminationGuard()
    try:
        previous = signal.signal(signal.SIGTERM, guard.handle)
    except (ValueError, OSError, AttributeError) as exc:  # pragma: no cover - platform-specific
        logger.warning(
            "SIGTERM handling unavailable (%s); a stop signal will end this session "
            "without writing its artifacts — exit with Ctrl-C instead",
            exc,
        )
        yield guard
        return
    try:
        yield guard
    finally:
        signal.signal(signal.SIGTERM, previous)


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


def _format_span(span: timedelta) -> str:
    """Render a duration in the coarsest whole unit that fits, for an operator line.

    ``timedelta``'s own ``str`` gives ``1:00:00`` and ``4 days, 0:00:00``; a session
    announcing when it will stop should say "60 minutes" and "4 days".
    """
    seconds = span.total_seconds()
    for unit, size in (("day", 86_400.0), ("hour", 3_600.0), ("minute", 60.0)):
        if seconds >= size:
            value = seconds / size
            return f"{value:g} {unit}" + ("" if value == 1.0 else "s")
    return f"{seconds:g} seconds"


def _make_adapter(
    source: str,
    cache_dir: Path,
    seed: int,
    frequency: Frequency = DAILY,
    data_feed: str | None = None,
) -> DataAdapter:
    # The market-data tape is an Alpaca-only notion (ADR-0034); silently ignoring
    # it on another source would let an operator think they chose a feed.
    if data_feed is not None and source != "alpaca":
        typer.echo(f"error: --data-feed applies only to --source alpaca, got {source!r}", err=True)
        raise typer.Exit(2)
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
            return AlpacaAdapter(interval=frequency.delta, feed=data_feed)
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
    halt_recovery_drawdown: float | None = None,
    halt_cooldown_bars: int | None = None,
) -> RiskConfig:
    """Assemble the run's RiskConfig, or the permissive opt-out when disabled.

    Raises ValueError (surfaced as a clean CLI error by the caller) on an invalid
    limit, a malformed --sector-map, or a halt-recovery threshold that is not below
    --max-drawdown (ADR-0031).
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
        halt_recovery_drawdown_pct=halt_recovery_drawdown,
        halt_cooldown_bars=halt_cooldown_bars,
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


def _run_benchmark(
    adapter: DataAdapter,
    symbol: str,
    *,
    cash: float,
    start: datetime,
    end: datetime,
) -> BacktestResult | None:
    """Run the unconstrained buy-and-hold benchmark, or warn and return ``None``.

    The benchmark is a comparison bolted onto a backtest that has *already*
    finished, so a benchmark that cannot be run must cost one warning line, not
    the summary, the equity CSV, and the result.json the operator asked for.
    Previously the exception escaped after the main run had succeeded and the
    whole command died with a traceback.

    Exactly one exception type is tolerated, and the narrowness is deliberate.
    After ADR-0032 every way the benchmark's *data* can fail arrives here as
    :class:`~trading.engine.EmptyUniverseError`: a mistyped ticker, a symbol that
    had not listed in the range, and a transport/credentials/unreadable-file
    failure all end up as an :class:`~trading.engine.AbsentSymbol` inside
    :func:`~trading.engine.load_series`, and a one-symbol universe with nothing
    in it raises. The three failure shapes therefore share one handler *and* stay
    distinguishable, because the error message carries the per-symbol reason.
    Anything else — a broken guardrail, a sizing crash, a misbehaving broker —
    means the bench itself is faulty, which makes the strategy numbers suspect
    too, so it is left to propagate.
    """
    bench_broker = SimulatedBroker(Portfolio(cash=cash))
    engine = Engine(adapter, bench_broker, Guardrails(RiskConfig.unlimited()))
    try:
        bench = engine.run(get_strategy("buy_and_hold"), [symbol], start, end)
    except EmptyUniverseError as exc:
        typer.echo(
            f"warning: benchmark {symbol} could not be run, continuing without it — {exc}",
            err=True,
        )
        return None
    _warn_if_benchmark_never_invested(symbol, bench)
    return bench


def _warn_if_benchmark_never_invested(symbol: str, bench: BacktestResult) -> None:
    """Warn on stderr when the benchmark ran fine but never held anything.

    The other benchmark failure — no data — is an exception and is caught above. A
    benchmark that *runs* and never takes a position is not: an underfunded entry
    is recorded on ``rejections``, not raised (ADR-0004), so before this the run
    printed ``+0.00%`` with total confidence. :func:`~trading.report.summarize`
    carries the same fact into the summary body; this line exists as well because
    stderr is where an operator watching a long run looks, and because the summary
    scrolls past while a warning does not.
    """
    if any(point.exposure > 0.0 for point in bench.equity_curve):
        return
    detail = ""
    if bench.rejections:
        detail = f" — {len(bench.rejections)} order(s) rejected, first: {bench.rejections[0][1]}"
    typer.echo(
        f"warning: benchmark {symbol} never took a position, so its return is idle cash "
        f"rather than a market return{detail}",
        err=True,
    )


def _check_bootstrap_options(*, bootstrap: bool, resamples: int) -> None:
    """Reject a bad ``--bootstrap-resamples`` **before** the backtest runs.

    The bootstrap happens after the engine has finished, so validating it there
    would let a typo throw away a completed multi-year run and write nothing. The
    check is gated on ``--bootstrap`` because the count is meaningless without it,
    exactly as ``--bootstrap-seed`` is.
    """
    if bootstrap and resamples < 1:
        typer.echo(
            f"error: --bootstrap-resamples must be >= 1, got {resamples}",
            err=True,
        )
        raise typer.Exit(2)


def _assess_significance(
    result: BacktestResult,
    benchmark: BacktestResult | None,
    *,
    periods_per_year: float,
    resamples: int,
    seed: int,
) -> SignificanceReport:
    """Run the ADR-0039 bootstrap for a finished backtest, or exit 2 on a bad knob.

    The benchmark's curve is what turns on the *paired* win rate — the figure that
    says whether the strategy beat the alternative rather than merely beating zero —
    so it is passed through whenever ``--benchmark`` produced a run. When it did not
    (absent, or warned away by :func:`_run_benchmark`), ``assess_significance``
    records a note explaining the absence instead of quietly omitting a row.

    Any ``ValueError`` from ``metrics`` is a caller mistake rather than a data
    shortfall (a data shortfall comes back as a ``None`` block and a note), so it
    becomes the same clean exit-2 CLI error every other bad option gets instead of
    a traceback. The one such mistake reachable from here — a resample count below
    1 — is already caught by :func:`_check_bootstrap_options` before the run
    starts; this is the backstop that keeps a future guard in ``metrics`` from
    surfacing raw.
    """
    try:
        return assess_significance(
            result.equity_curve,
            benchmark.equity_curve if benchmark is not None else None,
            periods_per_year,
            resamples=resamples,
            seed=seed,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


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
    halt_recovery_drawdown: float | None = typer.Option(
        None,
        "--halt-recovery-drawdown",
        help=(
            "Re-arm the halted kill switch once drawdown has recovered to at most this "
            "fraction (must be below --max-drawdown); off by default, so the halt latches."
        ),
    ),
    halt_cooldown_bars: int | None = typer.Option(
        None,
        "--halt-cooldown-bars",
        help=(
            "Re-arm the halted kill switch after this many bars in force. With "
            "--halt-recovery-drawdown, whichever triggers first wins. Off by default."
        ),
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
    bootstrap: bool = typer.Option(
        False,
        "--bootstrap/--no-bootstrap",
        help=(
            "Bootstrap the Sharpe: a stationary-block confidence interval, the paired "
            "win rate against --benchmark, and the trial-count deflation (ADR-0039). "
            "Off by default — it costs ~2.7s on a 21-year daily run."
        ),
    ),
    bootstrap_resamples: int = typer.Option(
        DEFAULT_BOOTSTRAP_RESAMPLES,
        "--bootstrap-resamples",
        help="Resamples per bootstrap figure (needs --bootstrap). Cost is linear in this.",
    ),
    bootstrap_seed: int = typer.Option(
        DEFAULT_BOOTSTRAP_SEED,
        "--bootstrap-seed",
        help="Seed for the bootstrap RNG (needs --bootstrap). Printed with the interval, "
        "so the figure is reproducible.",
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
    _check_bootstrap_options(bootstrap=bootstrap, resamples=bootstrap_resamples)

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
            halt_recovery_drawdown=halt_recovery_drawdown,
            halt_cooldown_bars=halt_cooldown_bars,
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
        bench_result = _run_benchmark(adapter, bench_symbol, cash=cash, start=start, end=end)

    # The bootstrap is computed ONCE here and handed to both the text summary and
    # result.json (ADR-0039). Neither derives it: a `result.json` must never
    # silently pay for thousands of Sharpe computations nobody asked for, and a run
    # without --bootstrap has to print exactly the bytes it always did.
    significance = (
        _assess_significance(
            result,
            bench_result,
            periods_per_year=freq.periods_per_year,
            resamples=bootstrap_resamples,
            seed=bootstrap_seed,
        )
        if bootstrap
        else None
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
            significance=significance,
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
        significance=significance,
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
    if outcome.resumed_now:
        parts.append("RESUME: kill switch re-armed — new entries allowed again")
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
    """Overwrite the running-state file with the latest equity and positions.

    Written to a sibling temp file and moved into place with :func:`os.replace`
    (ADR-0048). This runs on **every** bar of a live session, so the odds of a
    crash landing inside the write are not negligible over a day — and a plain
    ``write_text`` truncates first, which means the failure mode is a *truncated*
    state file: the one artifact of a killed session that survived, replaced by
    half of itself. ``os.replace`` is atomic within a filesystem on POSIX, so a
    reader sees either the previous bar's state or this one's, never neither.
    """
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
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    halt_recovery_drawdown: float | None = typer.Option(
        None,
        "--halt-recovery-drawdown",
        help=(
            "Re-arm the halted kill switch once drawdown has recovered to at most this "
            "fraction (must be below --max-drawdown); off by default, so the halt latches."
        ),
    ),
    halt_cooldown_bars: int | None = typer.Option(
        None,
        "--halt-cooldown-bars",
        help=(
            "Re-arm the halted kill switch after this many bars in force. With "
            "--halt-recovery-drawdown, whichever triggers first wins. Off by default."
        ),
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
    data_feed: str | None = typer.Option(
        None,
        "--data-feed",
        help="Alpaca market-data tape: iex | sip. Defaults to iex under --live "
        "--source alpaca (a free data plan refuses recent SIP bars), else the "
        "SDK's consolidated-SIP default.",
    ),
    divergence: bool = typer.Option(
        False,
        "--divergence/--no-divergence",
        help="Run a counterfactual SimulatedBroker beside the live broker and report "
        "where the fills diverge — price, slippage bps, latency, rejections "
        "(ADR-0038). Off by default; enabling it never touches the live path.",
    ),
    lookback: int | None = typer.Option(
        None,
        "--lookback",
        help="How many recent completed bars each poll requests, and therefore how "
        f"much history a --live session warms up on (default {DEFAULT_PAPER_LOOKBACK}). "
        "Under --once this is a floor: the replay always covers the whole range.",
    ),
    max_empty_polls: int | None = typer.Option(
        None,
        "--max-empty-polls",
        help="Stop after this many consecutive polls that reveal no new bar. The "
        "default is derived from --interval: whatever covers "
        f"{_format_span(LIVE_SILENCE_TOLERANCE)} of silence, at least "
        f"{MIN_LIVE_EMPTY_POLLS} polls (5m -> 12, 1d -> 4). Under --once the "
        "default stays 1.",
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
            halt_recovery_drawdown=halt_recovery_drawdown,
            halt_cooldown_bars=halt_cooldown_bars,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    # A live Alpaca feed polls right up to `now`, which a free data plan refuses on
    # the SIP tape (HTTP 403); IEX is what it does serve in real time, so that is
    # the live default while historical/replay runs keep the SIP default (ADR-0034).
    if data_feed is None and live and source == "alpaca":
        data_feed = "iex"
    adapter = _make_adapter(source, cache_dir, seed, freq, data_feed)
    broker = _make_paper_broker(broker_name, live, cash)

    # The clock and feed are the *only* difference between backtest and paper
    # (ADR-0002/0014). Live: wall clock over a recent-window feed, runs until
    # interrupted. Once: materialize the [from, to] bars into an in-memory adapter
    # and a fake clock parked just past the range so every bar reads as complete —
    # the loop drains them one _step at a time and stops, offline and deterministic.
    # Sub-daily bars need the interval-aware completeness policy (ADR-0022); daily
    # keeps the default policy so the daily path stays byte-identical to V5.
    is_complete = interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete
    window = DEFAULT_PAPER_LOOKBACK if lookback is None else lookback

    # How much silence ends the session, decided here because this is where the
    # live/replay distinction lives (ADR-0049). A --once replay knows its whole
    # range up front and drains it in one poll, so a single quiet poll means done;
    # that explicit 1 is what keeps every offline replay byte-identical. A --live
    # session's quiet polls are the market being shut or the feed hiccuping, and the
    # loop's own default of 2 polls meant ten minutes at 5m and two days at 1d — so
    # a daily session died over every weekend and an intraday one died on a
    # twenty-minute data gap. The live default is therefore a duration converted at
    # the bar interval. An operator who knows their venue can override either.
    if max_empty_polls is not None and max_empty_polls < 1:
        typer.echo("error: --max-empty-polls must be at least 1", err=True)
        raise typer.Exit(2)

    run_kwargs: dict[str, int] = {}
    if live:
        clock: WallClock | FakeClock = WallClock()
        feed = RecentWindowFeed(adapter, clock, is_complete)
        quiet_polls = (
            silence_tolerance_polls(freq.delta) if max_empty_polls is None else max_empty_polls
        )
    else:
        series = {s: adapter.get_bars(s, start, end) for s in tickers}
        all_bars = [bar for bars in series.values() for bar in bars]
        total = len({bar.ts for bar in all_bars})
        window = max(window, total + 1)
        clock = FakeClock(end + timedelta(days=1))
        feed = RecentWindowFeed(FakeAdapter(all_bars), clock, is_complete)
        quiet_polls = 1 if max_empty_polls is None else max_empty_polls
    run_kwargs = {"max_empty_polls": quiet_polls}

    # Divergence tracking is a Broker *decorator*, so the engine, the strategy, and
    # the guardrails are untouched and there is no mode branch inside the shared
    # step (ADR-0002/0038). Off by default: with --no-divergence the wrapper is
    # never constructed and the run is exactly what it was before this flag.
    #
    # The price notion is whatever the feed is serving (ADR-0021, and the reason a
    # divergence number can be meaningless): --live polls RecentWindowFeed, which
    # asks for RAW quotes, matching the raw dollars the venue fills in. The --once
    # replay materializes the range through the adapter's default *adjusted* fetch
    # above, so it is labelled as such and never silently mixed with a raw fill.
    #
    # The rows are journaled as they settle (ADR-0048). fill_divergence.csv *is*
    # the measurement a live session exists to collect and nothing else the run
    # leaves behind can reconstruct it, so it is written incrementally rather than
    # only after finalize(): a session that dies without unwinding — kill -9, power
    # loss, a suspended laptop, an unhandled exception — keeps every settled row.
    divergence_csv = out / "fill_divergence.csv"
    tracked_broker = broker
    shadow: ShadowBroker | None = None
    if divergence:
        shadow = ShadowBroker(
            broker,
            clock,
            price_notion=NOTION_RAW if live else NOTION_ADJUSTED,
            journal=DivergenceJournal(divergence_csv),
        )
        tracked_broker = shadow
    engine = Engine(adapter, tracked_broker, Guardrails(risk))

    # Warmup is what separates the two modes' *first* poll (ADR-0042). A --live
    # session opens onto a window of bars that closed before it started: those are
    # history to prime, not a range to trade, or the session fires hundreds of
    # orders priced off stale opens and poisons the fill-divergence sample it
    # exists to collect (ADR-0038). A --once replay is the opposite by definition —
    # replaying [from, to] and trading it *is* the mode — so it opts out, which is
    # what keeps --once byte-identical to every run before this flag existed.
    session = PaperSession(
        engine,
        strat,
        tickers,
        feed,
        clock,
        lookback=window,
        frequency=freq,
        warmup=live,
    )

    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "paper_session.log"
    state_path = out / "paper_state.json"
    mode = "live" if live else "once"
    typer.echo(f"Paper session ({mode}) — strategy={strategy} symbols={','.join(tickers)}\n")
    if live:
        # An unattended session ends by *policy*, and the operator watching it needs
        # to be able to tell that from a crash or a hang before it happens rather
        # than after (ADR-0049). Live-only: --once stdout must not move.
        typer.echo(
            f"Stops after {quiet_polls} consecutive poll(s) with no new bar "
            f"— {_format_span(quiet_polls * freq.delta)} of silence at {freq.label}.\n"
        )
    # The one record that makes an unattended session's log answerable afterwards:
    # what was asked for, and when. Everything per-bar stays on stdout (ADR-0043).
    logger.info(
        "paper session starting: mode=%s strategy=%s symbols=%s interval=%s source=%s "
        "broker=%s lookback=%d max_empty_polls=%d out=%s",
        mode,
        strategy,
        ",".join(tickers),
        freq.label,
        source,
        broker_name,
        window,
        quiet_polls,
        out,
    )

    # SIGTERM is how a container, a service manager, or an operator at another
    # terminal stops this process, and its default disposition would kill the
    # interpreter without unwinding — losing the equity CSV, result.json and the
    # summary exactly as an unhandled Ctrl-C did before ADR-0033. Installed here,
    # around the session *and* the artifact writing that follows it, so a second
    # signal cannot truncate what the first one was honoured to save (ADR-0043).
    with _sigterm_stops_the_session() as termination:
        with log_path.open("w") as log_fh:
            announced = False

            def announce_warmup() -> None:
                """Say what was primed, exactly once, before the first live bar prints.

                A silent warmup is indistinguishable from a session that quietly did
                nothing, and the count is the operator's check that the strategy has
                its lookback before it trades. Wired to the session's ``on_warmup``
                hook so it lands the moment priming finishes -- the session then
                sleeps to the next bar boundary, so waiting for the first bar report
                would leave a 1h live run silent for an hour after startup. Also
                called on both exit paths, so a session that never reaches a live bar
                still says what it saw.
                """
                nonlocal announced
                if announced or not live:
                    return
                announced = True
                span = session.warmup_span
                if span is None:
                    line = (
                        "Warmup: no completed bars were available, so the strategy starts "
                        "with empty history and stays flat until its lookback fills."
                    )
                else:
                    line = (
                        f"Warmup: primed {session.warmup_bars} completed bar(s) "
                        f"{span[0]:%Y-%m-%d %H:%M}..{span[1]:%Y-%m-%d %H:%M} as history; "
                        "no orders submitted for them (ADR-0042)."
                    )
                typer.echo(line + "\n")
                log_fh.write(line + "\n")
                log_fh.flush()
                logger.info("%s", line)

            def reporter(outcome: BarOutcome) -> None:
                announce_warmup()
                line = _format_bar(outcome)
                typer.echo(line)
                log_fh.write(line + "\n")
                log_fh.flush()
                _persist_state(state_path, outcome, broker.portfolio)

            try:
                result = session.run(reporter=reporter, on_warmup=announce_warmup, **run_kwargs)
                announce_warmup()
            except KeyboardInterrupt as exc:
                announce_warmup()
                # A --live session has no natural exit: Ctrl-C *is* how it ends.
                # Letting the interrupt propagate skipped everything below, so the
                # equity CSV, result.json, and the summary were unreachable in live
                # mode even though every bar had been processed and logged
                # (ADR-0033). SIGTERM arrives here as the same exception on purpose
                # (ADR-0043) — one exit path, two names for how it was triggered,
                # because "the orchestrator stopped the container" and "someone
                # pressed Ctrl-C" are different facts to read in a log afterwards.
                trigger = (
                    "SIGTERM received" if isinstance(exc, SessionTerminated) else "Interrupted"
                )
                logger.warning("%s — finalizing the session", trigger)
                typer.echo(f"\n{trigger} — finalizing with the bars processed so far.")
                result = session.finalize()

        # From here on the session is over and the artifacts are being written; a
        # further stop signal is dropped rather than allowed to truncate them.
        termination.disarm()

        csv_path = out / "equity_curve.csv"
        write_equity_csv(result, csv_path)

        # The canonical machine-readable artifact the dashboard consumes, alongside
        # the CSV. Metrics are computed at the run's frequency (252/yr for daily).
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
        artifacts = [
            f"Session log: {log_path}",
            f"Running state: {state_path}",
            f"Equity curve: {csv_path}",
            f"Result JSON: {result_json}",
        ]

        if shadow is not None:
            records = shadow.divergences
            # Replaces the journal atomically with the canonical file: same rows in
            # the same order, plus the ones still open at the end (an order parked
            # at the venue, ADR-0036), which are deliberately never journaled early.
            write_divergence_csv(records, divergence_csv)
            typer.echo("\n" + render_report(shadow.summary, records))
            artifacts.append(f"Fill divergence: {divergence_csv}")

        typer.echo(f"\nProcessed {len(session.session_log)} completed bar(s).")
        typer.echo("\n".join(artifacts))
        logger.info(
            "paper session finished: %d completed bar(s) processed, artifacts written to %s",
            len(session.session_log),
            out,
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


def _sweep_significance_block(summary: SweepSummary, rank_by: str, periods_per_year: float) -> str:
    """The winner's trial-count deflation, rendered exactly as ``backtest`` renders it.

    A sweep's headline is the *maximum* of everything it ran, and a maximum of N
    draws beats zero even when not one of the N has an edge. This scores the winner
    against the Sharpe the luckiest skill-free candidate would have shown, using the
    same :func:`~trading.report.summarize_significance` renderer the backtest
    summary uses so the two commands can never grow divergent wordings for the same
    statistic.

    No bootstrap runs here and none is needed: a sweep keeps each trial's
    :class:`~trading.metrics.ReturnMoments`, not its curve, and the deflation is
    arithmetic on those — so this block costs nothing and is therefore *not* behind
    ``--bootstrap``, unlike the interval on a single backtest.

    ``""`` when the summary has no runs, or when the winner's moments were not
    recorded — an honest absence rather than a fabricated figure.
    """
    deflated = summary.deflated_winner(rank_by, periods_per_year)
    if deflated is None:
        return ""
    return summarize_significance(
        SignificanceReport(deflated=deflated, notes=[trial_count_note(deflated.trials)])
    )


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
    halt_recovery_drawdown: float | None = typer.Option(
        None,
        "--halt-recovery-drawdown",
        help=(
            "Re-arm the halted kill switch once drawdown has recovered to at most this "
            "fraction (must be below --max-drawdown); off by default, so the halt latches."
        ),
    ),
    halt_cooldown_bars: int | None = typer.Option(
        None,
        "--halt-cooldown-bars",
        help=(
            "Re-arm the halted kill switch after this many bars in force. With "
            "--halt-recovery-drawdown, whichever triggers first wins. Off by default."
        ),
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
            halt_recovery_drawdown=halt_recovery_drawdown,
            halt_cooldown_bars=halt_cooldown_bars,
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
        deflation = _sweep_significance_block(summary, rank_by, freq.periods_per_year)
        if deflation:
            typer.echo("\n" + deflation)
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
