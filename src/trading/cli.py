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
- ``backtest --regimes`` splits the metrics by the run's own high/low-volatility
  and trending/mean-reverting bars, restating the same ``PerformanceMetrics``
  restricted to each label (ADR-0066) — a 21-year Sharpe otherwise averages the
  dot-com bust, the GFC, and the 2009-2020 bull run into one number. Off by
  default; a run without the flag is byte-identical to before it existed.
- ``backtest --monte-carlo`` reshuffles the run's own per-bar returns into
  thousands of random reorderings (an exact permutation each time, never a
  resample-with-replacement) and places the run's *actual*, path-ordered max
  drawdown against that distribution — did this run's sequence of losses cluster
  unusually badly, or was it unusually fortunate (ADR-0067)? The Sharpe is printed
  once, unchanged by reordering, beside the ADR-0039 bootstrap CI above it rather
  than as a fabricated "distribution". Off by default; a run without the flag is
  byte-identical to before it existed.

- ``backtest --ledger PATH`` / ``sweep --ledger PATH`` append every invocation to a
  cross-invocation JSONL trial ledger (``trading.ledger.TrialLedger``, ADR-0062) and
  widen the ADR-0039 deflation by its cumulative trial count, so a search made across
  many separate invocations is no longer invisible to the correction. Off by default;
  a path you did not give is a path this tool does not touch, and the file only ever
  grows (append-only, so a crash mid-write under-reports rather than corrupts).
  ``--hypothesis TEXT`` records a pre-registered rationale verbatim alongside the
  count, for KAN-862's still-unbuilt playbook to enforce later.
- ``backtest --liquidity-tier-adv`` charges a lower, more-liquid slippage rate to
  symbols whose pre-run ADV (the same formation-window measurement ``--min-adv``
  uses) clears the floor, and leaves everything else on the market's flat default
  (KAN-861, ADR-0063) — cost is a function of liquidity, not of asset class, so a
  cross-sectional run spanning the whole S&P 500 should not price its 500th name
  like a mega-cap. Off by default; a run without the flag prices every symbol flat,
  exactly as before.
- ``sweep --slippage-sweep 5,10,25,50`` re-runs the sweep's own winning combo at
  every slippage level in the grid, holding the strategy's parameters fixed, and
  prints the interpolated bps level where Sharpe/total return crosses zero — where
  the edge dies as cost rises (KAN-618, ADR-0069). Off by default; the main sweep
  table and CSV are unchanged whether or not it is passed.
- ``backtest --cost-budget-pct`` warns (never aborts) when a run's own predicted
  cost drag -- annual turnover times the effective one-way rate this run actually
  traded under -- exceeds a stated fraction of equity per year (ADR-0068,
  KAN-860). Off by default; a run without the flag pays nothing extra and prints
  exactly the bytes it always did.
- ``backtest --diversified-baseline`` runs a second, independent comparison
  alongside ``--benchmark``: a naive ``equal_weight`` allocation across a
  multi-asset basket (``--baseline-basket``, default ``@core10``), reported the
  same way -- total return, the never-invested/invested-late honesty check, and
  beta/alpha/correlation/information ratio (ADR-0071, KAN-641). A strategy that
  cannot beat naive diversification is not earning its complexity. Off by
  default; a run without the flag prints exactly the bytes it always did.
- ``--symbols @sp500`` (``backtest``/``paper``/``sweep``/``gen-data``, also
  ``--baseline-basket``) resolves the S&P 500 constituents as they actually stood
  on the run's own ``--from`` date, not today's list (ADR-0072, KAN-639) — ranking
  today's survivors over history is the exact hindsight ``blue20`` is already
  documented to have. A **static, point-in-time snapshot resolved once**, not a
  membership that mutates mid-run as names are added/removed (that needs an
  engine-level mutable universe — KAN-633, a separate deferred card). ``--sector-map
  @sp500`` is refused with the existing "unknown basket" error: there is no
  committed sector map for 500 names.

The trades-per-parameter sample-size check is wired automatically: every run
reports its entry count, and a run with too few trades for its number of tunable
parameters says so (ADR-0029).

``--market`` (ADR-0057) is the one choice that reaches all three of EPIC-87's
phase-1 seams at once: the :class:`~trading.calendar.MarketCalendar` every
annualized figure is derived from (ADR-0054), the bar-completeness rule the paper
feed acts on (ADR-0053), and the :class:`~trading.config.RiskConfig` posture the
guardrails enforce (ADR-0055). It defaults to ``us_equity``, so every invocation
that predates it behaves exactly as it did, and it is **explicit** rather than
sniffed from the data — with one guard on top, because forgetting the flag is the
silent failure the whole epic was sequenced around: a crypto-shaped symbol
(``BTC/USD``) under a market that closes is refused, not annualized on 252 days.

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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType

import typer

from trading.broker import SimulatedBroker
from trading.brokers.alpaca import AlpacaBroker
from trading.calendar import CALENDARS, CRYPTO_24_7, US_EQUITY, MarketCalendar, get_calendar
from trading.clock import FakeClock, WallClock
from trading.config import (
    CRYPTO_HALT_COOLDOWN_BARS,
    CRYPTO_TAKER_FEE_BPS,
    LIQUID_TIER_SLIPPAGE_BPS,
    CostConfig,
    RiskConfig,
)
from trading.data.alpaca_adapter import AlpacaAdapter
from trading.data.csv_adapter import CsvAdapter
from trading.data.fake import FakeAdapter
from trading.data.recent_window import (
    CompletenessPolicy,
    RecentWindowFeed,
    default_is_complete,
    interval_is_complete,
)
from trading.data.sp500_membership import PointInTimeSP500
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
from trading.ledger import TrialLedger, TrialRecord
from trading.liquidity import (
    DEFAULT_FORMATION_DAYS,
    DEFAULT_TIER_ADV_FLOOR,
    classify_liquidity_tier,
    liquidity_tier_rates,
    screen_by_adv,
)
from trading.logging_config import (
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
    LOG_FORMATS,
    configure_logging,
)
from trading.metrics import (
    DEFAULT_BOOTSTRAP_RESAMPLES,
    DEFAULT_BOOTSTRAP_SEED,
    CostBudgetReport,
    DiversifiedBaselineReport,
    SignificanceReport,
    assess_cost_budget,
    assess_diversified_baseline,
    assess_significance,
    compute_regime_report,
    monte_carlo_shuffle,
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
from trading.sweep import (
    CostSensitivitySummary,
    EdgeDeath,
    NeighborStability,
    SweepSummary,
    WalkForwardSummary,
    combo_key,
    run_cost_sensitivity_sweep,
    run_sweep,
    run_walk_forward,
)
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


# `@sp500` is deliberately NOT a `universe.BASKETS` entry (ADR-0072, KAN-639): a
# static basket is a fixed symbol list, but the whole point of this sigil is that
# its answer depends on *when* you ask (`PointInTimeSP500.members_as_of`). Handled
# in `_parse_symbols` before falling through to `universe.get_universe`, so it
# stays a plain lookup miss (KeyError) for `universe.py` and cannot collide with a
# real basket name added there later.
SP500_SIGIL = "sp500"


def _parse_sp500_universe(as_of: datetime | None) -> list[str]:
    """Resolve `@sp500` to the S&P 500 membership as of ``as_of`` (ADR-0072).

    Ranking today's constituents over history is the exact survivorship trap
    ADR-0027 documents: the removed names are disproportionately the losers, and
    excluding them inflates the result. This asks `sp500_membership.py`
    (ADR-0064) for who was actually in the index on the run's own start date, not
    today's list. ``as_of`` is ``None`` when the calling command has no date in
    scope (e.g. `verify-universe`) -- there is no "today" fallback, because a
    silent fallback to current membership is precisely the survivorship bug this
    sigil exists to avoid.
    """
    if as_of is None:
        typer.echo(
            f"error: @{SP500_SIGIL} needs a start date to resolve point-in-time "
            "membership (ADR-0064/0072) and this command has none in scope -- use "
            "backtest/paper/sweep/gen-data, or pass a plain comma list here.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        return PointInTimeSP500.from_fixture().members_as_of(as_of)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _parse_symbols(symbols: str, as_of: datetime | None = None) -> list[str]:
    # `@name` expands a curated basket (universe.py) or the point-in-time S&P 500
    # (ADR-0072); a plain comma list is verbatim. `as_of` is the caller's own
    # backtest/session start date, threaded through by every command that has one.
    if symbols.startswith("@"):
        name = symbols[1:].strip()
        if name == SP500_SIGIL:
            return _parse_sp500_universe(as_of)
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


def _apply_liquidity_tiering(
    adapter: DataAdapter,
    tickers: list[str],
    start: datetime,
    *,
    tier_adv_floor: float,
    tier_slippage_bps: float,
    formation_days: int,
) -> dict[str, float]:
    """Classify ``tickers`` into the liquid cost tier by pre-run ADV (KAN-861).

    Reuses the exact formation-window ADV measurement ``--min-adv`` already uses
    (:func:`~trading.liquidity.classify_liquidity_tier`) — this doesn't drop a
    symbol, it only assigns a rate — and prints one line per symbol so a tiered
    run's cost basis is visible on stdout rather than a number baked silently into
    ``result.json``. Symbols below the floor, or with no formation-window data,
    keep the market's flat default rate untouched.
    """
    advs = classify_liquidity_tier(adapter, tickers, start, formation_days=formation_days)
    tiered = liquidity_tier_rates(
        advs, tier_adv_floor=tier_adv_floor, tier_slippage_bps=tier_slippage_bps
    )
    lines = [
        f"Liquidity cost tier: ADV >= ${tier_adv_floor:,.0f} -> {tier_slippage_bps:g} bps "
        "slippage (else the market's default rate), over the same pre-run formation "
        "window --min-adv uses (KAN-861, ADR-0063)"
    ]
    for symbol in tickers:
        adv = advs.get(symbol)
        adv_str = "no data" if adv is None else f"${adv:,.0f}"
        tier = "tiered" if symbol in tiered else "default"
        lines.append(f"  {symbol}: ADV {adv_str} -> {tier}")
    typer.echo("\n".join(lines) + "\n")
    return tiered


@dataclass(frozen=True, slots=True)
class _Market:
    """One market selection: a calendar, a risk posture, costs, and completeness.

    The things EPIC-87's phase-1 lanes built as independent library seams
    (ADR-0053/0054/0055), tied together by the one choice an operator makes
    (ADR-0057), plus the cost model ADR-0060 added as the fourth seam. ``name`` is
    the :class:`~trading.calendar.MarketCalendar`'s own registry name, so there is
    exactly one spelling of a market in the codebase and it is the one
    ``result.json`` records.
    """

    name: str
    calendar: MarketCalendar
    posture: RiskConfig
    costs: CostConfig


# The risk posture each market trades under (ADR-0055). Keyed by calendar name, so
# a calendar cannot be selectable without a posture: `_resolve_market` refuses a
# calendar missing from this table rather than falling back to the equity numbers,
# which is `get_calendar`'s own rule (ADR-0054) applied one layer up. A third
# market added to `CALENDARS` therefore fails loudly here until someone decides
# what risk limits it trades under, instead of silently inheriting equity's.
_MARKET_POSTURES: dict[str, Callable[[], RiskConfig]] = {
    US_EQUITY.name: RiskConfig.equity,
    CRYPTO_24_7.name: RiskConfig.crypto,
}

# The cost model each market trades under (ADR-0060), keyed by calendar name for
# exactly the reason `_MARKET_POSTURES` is: a market whose trading costs nobody has
# researched must be **unselectable**, not silently charged US-equity costs. That is
# the sharper half of this table, because the equity default is *commission-free* —
# a market missing here would not merely be mispriced, it would be modelled as free,
# which is the most flattering wrong answer available.
_MARKET_COSTS: dict[str, Callable[[], CostConfig]] = {
    US_EQUITY.name: CostConfig.equity,
    CRYPTO_24_7.name: CostConfig.crypto,
}

# Operator-friendly spellings of the canonical calendar names. Pure input
# normalization, exactly like the ``@basket`` sigil: the canonical name is what is
# printed, logged, and written to ``result.json``, so the alias never becomes a
# second vocabulary that can drift from the registry's.
_MARKET_ALIASES: dict[str, str] = {
    "equity": US_EQUITY.name,
    "us-equity": US_EQUITY.name,
    "crypto": CRYPTO_24_7.name,
    "crypto-24-7": CRYPTO_24_7.name,
}

# The market a run trades when the operator does not say: the only one this bench
# traded before EPIC-87, and the reason every equity invocation is byte-identical.
DEFAULT_MARKET = US_EQUITY.name

# Written once and shared by `backtest`, `paper` and `sweep`: three copies of a
# sentence about precedence is three chances for one of them to go stale.
MARKET_OPTION_HELP = (
    f"Market to trade: {' | '.join(sorted(CALENDARS))} (aliases: equity, crypto). "
    "Selects the annualization calendar, the paper feed's bar-completeness rule, and "
    f"the risk posture, all at once. Default {DEFAULT_MARKET}."
)
MAX_POSITION_HELP = (
    "Per-symbol position cap, fraction of equity. Unset takes the --market posture's "
    "value (us_equity: 0.25)."
)
MAX_GROSS_HELP = (
    "Max gross exposure, fraction of equity. Unset takes the --market posture's value "
    "(us_equity: 1.0)."
)
MAX_DRAWDOWN_HELP = (
    "Drawdown kill-switch threshold, fraction from peak. Unset takes the --market "
    "posture's value (us_equity: 0.20)."
)
SLIPPAGE_HELP = (
    "Adverse price move applied to every fill, in basis points. Unset takes the "
    "--market cost model's value (us_equity and crypto_24_7 both: 5.0)."
)
TAKER_FEE_HELP = (
    "Venue fee on the traded notional, in basis points. Unset takes the --market "
    f"cost model's value (us_equity: 0.0, commission-free; crypto_24_7: "
    f"{CRYPTO_TAKER_FEE_BPS:g}, Alpaca's published tier-1 taker rate — ADR-0060)."
)
LIQUIDITY_TIER_ADV_HELP = (
    "Charge a lower, more-liquid slippage rate (--liquidity-tier-slippage-bps) to "
    "symbols whose pre-run ADV clears this floor, measured over the same pre-"
    "backtest formation window --min-adv uses (KAN-861, ADR-0063). Symbols below "
    "the floor, or unmeasured, keep the market's flat default rate. Off by "
    "default (None) — a run without this flag prices every symbol flat, exactly "
    f"as before. A reasonable floor is {DEFAULT_TIER_ADV_FLOOR:,.0f} (dollars/day)."
)
LIQUIDITY_TIER_SLIPPAGE_HELP = (
    "Slippage bps charged to symbols at/above --liquidity-tier-adv (needs that "
    f"flag). Default {LIQUID_TIER_SLIPPAGE_BPS:g}."
)
COST_BUDGET_HELP = (
    "Warn (never abort) when this run's own predicted cost drag -- annual turnover "
    "times the effective one-way rate (slippage + taker fee) this run actually "
    "traded under -- exceeds this fraction of equity per year, e.g. 0.01 for 1% "
    "(ADR-0068, KAN-860). Off by default (None); a run without the flag pays "
    "nothing extra and prints nothing new."
)
HALT_COOLDOWN_HELP = (
    "Re-arm the halted kill switch after this many bars in force. With "
    "--halt-recovery-drawdown, whichever triggers first wins. Unset takes the --market "
    "posture's value: us_equity none (the halt latches), crypto_24_7 "
    f"{CRYPTO_HALT_COOLDOWN_BARS} (ADR-0055)."
)
# Shared by `backtest` and `sweep` for the same reason MARKET_OPTION_HELP is: one
# sentence, not two chances for the wording to drift (ADR-0062).
LEDGER_HELP = (
    "Append this run to a cross-invocation JSONL trial ledger at PATH, and widen the "
    "ADR-0039 deflation by its cumulative trial count from earlier logged invocations. "
    "Off by default — a path you do not give is a path this tool does not touch."
)
HYPOTHESIS_HELP = (
    "Pre-registered rationale for this run, recorded verbatim in the ledger (needs "
    "--ledger; harmless, but not yet used, without it). For KAN-862's playbook."
)
SLIPPAGE_SWEEP_HELP = (
    "Cost-sensitivity sweep (KAN-618, ADR-0069): comma-separated slippage-bps grid, "
    "e.g. 5,10,25,50. Re-runs the sweep's own winning combo (by --rank-by) at each "
    "level, holding its parameters fixed, and reports where Sharpe/total return "
    "crosses zero as cost rises. Off by default; the main sweep table and CSV are "
    "unchanged whether or not this is passed."
)

# Quote currencies that make a symbol a *pair* rather than a ticker. Deliberately
# narrow (ADR-0057): the rule is "the segment after a `/`, `-` or `_` is one of
# these", so `BRK-B` and `BF-B` (real share-class tickers) and a Bloomberg-style
# `BRK/B` are all untouched, while `BTC/USD`, `BTC-USD` and `ETH/BTC` are caught.
# Extending this set is how a missed pair shape (say a `BTC/JPY`-only venue) gets
# handled; an override flag is not, because a flag is a thing you forget you set.
_CRYPTO_QUOTE_CODES = frozenset(
    {"USD", "USDT", "USDC", "USDP", "BUSD", "DAI", "EUR", "GBP", "JPY", "BTC", "ETH"}
)

_PAIR_SEPARATORS = ("/", "-", "_")


def _resolve_market(name: str) -> _Market:
    """Resolve ``--market`` to a calendar + risk posture, or exit 2.

    Case- and whitespace-insensitive, and aliases (``crypto`` ->
    ``crypto_24_7``) resolve to the canonical calendar name. An unknown market is
    a hard error naming what we have, for the reason
    :func:`~trading.calendar.get_calendar` raises rather than defaulting: a run
    that quietly annualized a 24/7 market on the equity session, under equity risk
    limits, would print a confident number nobody could tell was wrong (ADR-0054).
    """
    key = name.strip().lower()
    key = _MARKET_ALIASES.get(key, key)
    try:
        calendar = get_calendar(key)
    except ValueError as exc:
        known = ", ".join(sorted(set(CALENDARS) | set(_MARKET_ALIASES)))
        typer.echo(f"error: unknown --market {name!r}; known markets: {known}", err=True)
        raise typer.Exit(2) from exc
    posture = _MARKET_POSTURES.get(calendar.name)
    if posture is None:
        typer.echo(
            f"error: market {calendar.name!r} has no risk posture, so it cannot be "
            "selected yet; add one to _MARKET_POSTURES (ADR-0055/0057). Refusing "
            "rather than falling back to the equity limits.",
            err=True,
        )
        raise typer.Exit(2)
    costs = _MARKET_COSTS.get(calendar.name)
    if costs is None:
        typer.echo(
            f"error: market {calendar.name!r} has no cost model, so it cannot be "
            "selected yet; add one to _MARKET_COSTS (ADR-0060). Refusing rather "
            "than falling back to the commission-free equity costs, which would "
            "model an unresearched venue as free.",
            err=True,
        )
        raise typer.Exit(2)
    return _Market(name=calendar.name, calendar=calendar, posture=posture(), costs=costs())


def _crypto_shaped(symbol: str) -> str | None:
    """Why ``symbol`` looks like a crypto pair, or ``None`` if it does not.

    A *shape* test, not a lookup: the segment after a pair separator has to be a
    known quote currency. That is narrow on purpose — see ``_CRYPTO_QUOTE_CODES``.
    """
    upper = symbol.strip().upper()
    for separator in _PAIR_SEPARATORS:
        base, found, quote = upper.rpartition(separator)
        if found and base and quote in _CRYPTO_QUOTE_CODES:
            return f"{base}{separator}{quote} is a pair quoted in {quote}"
    return None


def _check_symbol_shapes(market: _Market, tickers: list[str]) -> None:
    """Refuse crypto-shaped symbols on a market that closes (ADR-0057).

    The belt to ``--market``'s braces. An explicit flag is honest and forgettable,
    and *forgetting* it is the whole failure this epic was sequenced to prevent: the
    equity calendar understates a winner and flatters a loser, and pairs an honest
    drawdown with a Sharpe from another market's year (ADR-0054), so the wrong
    answer arrives looking exactly like a right one.

    One direction only. A crypto-shaped symbol under a session market is a silent
    wrong number, so it is refused. The reverse — an equity-looking ticker under
    ``--market crypto`` — is not checked, because the operator typed the market and
    the signal is weak the other way: a legitimate continuous symbol may be a bare
    ``BTC`` with no separator at all, so a "no crypto symbols here" warning would
    fire on correct usage. Chosen loudness follows ADR-0028's split: this is the
    broker-said-no case (a shape we recognise), not the could-not-ask case.
    """
    if market.calendar.is_continuous:
        return
    flagged = [(symbol, why) for symbol in tickers if (why := _crypto_shaped(symbol)) is not None]
    if not flagged:
        return
    detail = "; ".join(f"{symbol} ({why})" for symbol, why in flagged)
    typer.echo(
        f"error: {len(flagged)} symbol(s) look like crypto pairs but --market is "
        f"{market.name!r}: {detail}. On this calendar they would be annualized at "
        f"{market.calendar.days_per_year:g} x {market.calendar.minutes_per_day:g} "
        f"min/day and run under the {market.name} risk posture — a confident wrong "
        "number (ADR-0054/0057). Pass --market crypto, or rename the symbols if they "
        "really do trade on a session market.",
        err=True,
    )
    raise typer.Exit(2)


def _market_line(market: _Market, freq: Frequency) -> str:
    """One line naming a non-equity market's basis, or ``""`` for plain equity.

    Printed only when there *is* something to say, the same rule the absent-symbol
    and benchmark caveats follow — which is also what keeps every equity run's
    stdout byte-identical. When it appears it carries the two facts that make the
    figures below readable: the annualization basis and the halt posture.
    """
    if market.name == DEFAULT_MARKET:
        return ""
    cooldown = market.posture.halt_cooldown_bars
    recovery = f"halt re-arms after {cooldown} bar(s)" if cooldown is not None else "halt latches"
    return (
        f"Market:        {market.name} "
        f"({market.calendar.days_per_year:g} days x "
        f"{market.calendar.minutes_per_day:g} min/day) — "
        f"{freq.label} annualizes at {freq.periods_per_year:g} bars/year; "
        f"risk posture: {recovery}"
    )


def _completeness_policy(market: _Market, freq: Frequency) -> CompletenessPolicy:
    """Which bars the paper feed may act on, per market (ADR-0053).

    A continuous market has **no arms on this expression**:
    ``interval_is_complete(freq.delta)`` for every interval, daily included, because
    a 24/7 daily bar is a rolling 24-hour window closing at UTC midnight and
    ``ts + interval`` needs no calendar. ADR-0053 measured that there is nothing to
    add — the session rule and the interval rule agree at all 4,320 sampled instants
    on a midnight-stamped daily bar, and where a provider stamps it off midnight the
    session rule declares it complete *early*, handing the strategy a forming bar.
    Deliberately no ``continuous_is_complete`` to call: it would be a second name
    for one mechanism.

    A market that closes keeps the session rule for daily. That is not symmetry
    neglected: for a daily bar stamped at the session open, the interval rule would
    withhold it until 13.5 hours after the real close.
    """
    if market.calendar.is_continuous:
        return interval_is_complete(freq.delta)
    return interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete


def _parse_frequency(interval: str, calendar: MarketCalendar = US_EQUITY) -> Frequency:
    """Resolve ``--interval`` (e.g. ``1d``/``1h``/``30m``) or exit 2 on a bad label.

    ``calendar`` is the selected market's (ADR-0054): the label fixes the bar
    *length*, the calendar fixes how many of them a year holds. It defaults to US
    equity so a caller that has not chosen a market gets exactly what this function
    always returned.
    """
    try:
        return Frequency.parse(interval, calendar=calendar)
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
        # The venue comes from the frequency's calendar, which `--market` already
        # set (ADR-0057) — there is deliberately no separate asset-class flag, for
        # the reason ADR-0056 gave the synthetic generator: a second flag would
        # keep "crypto bars on a 252-day year" representable one keyword away.
        try:
            return AlpacaAdapter(
                interval=frequency.delta, feed=data_feed, calendar=frequency.calendar
            )
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
    posture: RiskConfig,
    max_position: float | None,
    max_gross: float | None,
    max_drawdown: float | None,
    target_vol: float | None,
    sector_map: str,
    max_sector_exposure: float | None,
    halt_recovery_drawdown: float | None = None,
    halt_cooldown_bars: int | None = None,
) -> RiskConfig:
    """Assemble the run's RiskConfig, or the permissive opt-out when disabled.

    **Precedence, in one sentence: an explicitly-passed flag always wins, and every
    limit the operator did not pass comes from the selected market's posture**
    (ADR-0055/0057). That is why the cap options default to ``None`` rather than to
    a number — ``None`` means "not chosen here", the idiom ``--target-vol`` and
    ``--halt-cooldown-bars`` already use, and it is the only way a preset and a
    per-flag override can compose without one silently shadowing the other. On the
    equity posture the resolved values are exactly the old literal defaults, so
    nothing about an existing invocation moves.

    ``--no-guardrails`` still wins over both: it is an explicit opt-out from
    enforcement, and therefore from the market's posture too.

    Raises ValueError (surfaced as a clean CLI error by the caller) on an invalid
    limit, a malformed --sector-map, or a halt-recovery threshold that is not below
    --max-drawdown (ADR-0031). Note one consequence worth stating: a crypto run
    cannot be talked into a permanently latching halt from the CLI, because the
    posture supplies the cooldown and there is no flag that spells ``None`` —
    ``RiskConfig.crypto(halt_cooldown_bars=None)`` is a ValueError for the same
    reason (ADR-0055).
    """
    if no_guardrails:
        return RiskConfig.unlimited()
    return RiskConfig(
        max_position_pct=posture.max_position_pct if max_position is None else max_position,
        max_gross_exposure=posture.max_gross_exposure if max_gross is None else max_gross,
        max_drawdown_pct=posture.max_drawdown_pct if max_drawdown is None else max_drawdown,
        target_volatility=target_vol,
        sector_map=_parse_sector_map(sector_map),
        max_sector_exposure=max_sector_exposure,
        halt_recovery_drawdown_pct=(
            posture.halt_recovery_drawdown_pct
            if halt_recovery_drawdown is None
            else halt_recovery_drawdown
        ),
        halt_cooldown_bars=(
            posture.halt_cooldown_bars if halt_cooldown_bars is None else halt_cooldown_bars
        ),
    )


def _build_costs(
    model: CostConfig,
    slippage_bps: float | None,
    taker_fee_bps: float | None,
) -> CostConfig:
    """Assemble the run's CostConfig from the market's model plus any overrides.

    **The same one-sentence precedence ``_build_risk`` uses** (ADR-0057, applied to
    costs by ADR-0060): an explicitly-passed flag always wins, and every term the
    operator did not pass comes from the selected market's cost model. Both options
    default to ``None`` — "not chosen here" — for the reason stated there: a number
    in the option *and* a number in the preset is two defaults that can drift apart.

    On ``us_equity`` the resolved values are exactly the old literals (5.0 bps of
    slippage, no commission, no fee), so no existing invocation moves a cent.

    Note the asymmetry with :meth:`~trading.config.CostConfig.crypto`, which refuses
    a zero fee. An explicit ``--taker-fee-bps 0`` is *not* refused here, and that is
    deliberate rather than an oversight: the preset's job is to stop a crypto run
    from being modelled as free by **default**, while a flag the operator typed is a
    typed choice that shows up in the shell history — exactly the line ADR-0057 drew
    when it let ``--max-drawdown 0.9`` override the posture's 0.20. The cure for a
    bad explicit number is the operator, not a second veto.
    """
    return CostConfig(
        commission_per_share=model.commission_per_share,
        slippage_bps=model.slippage_bps if slippage_bps is None else slippage_bps,
        taker_fee_bps=model.taker_fee_bps if taker_fee_bps is None else taker_fee_bps,
    )


def _make_paper_broker(
    name: str,
    live: bool,
    cash: float,
    calendar: MarketCalendar = US_EQUITY,
    costs: CostConfig | None = None,
) -> Broker:
    """Select the paper execution venue: the simulator, or the live Alpaca broker.

    Alpaca is real paper trading, so it requires --live and valid credentials; a
    missing key or the absent SDK surfaces as a clean CLI error, not a traceback.

    ``calendar`` reaches the Alpaca broker for the same reason it reaches the
    adapter (ADR-0058): the venue's crypto orders need a different time-in-force
    and its positions come back under a different symbol spelling. The simulator
    ignores it — it has no venue.

    ``costs`` is the mirror image: the **simulator** needs the market's cost model
    (ADR-0060) and the Alpaca broker does not, because a real venue charges what it
    charges and :class:`~trading.brokers.alpaca.AlpacaBroker` reconciles its
    portfolio from the account rather than from a modelled fill (ADR-0020). Our cost
    model is a *prediction* about that venue; handing it to the broker that talks to
    the venue would be modelling the thing we are measuring.
    """
    if name == "simulated":
        return SimulatedBroker(Portfolio(cash=cash), costs)
    if name == "alpaca":
        if not live:
            typer.echo("error: --broker alpaca requires --live (real paper trading).", err=True)
            raise typer.Exit(2)
        try:
            return AlpacaBroker(clock=WallClock(), calendar=calendar)
        except (ValueError, ImportError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
    typer.echo(f"error: --broker must be 'simulated' or 'alpaca', got {name!r}", err=True)
    raise typer.Exit(2)


def _run_benchmark(
    adapter: DataAdapter,
    strategy_name: str,
    symbols: list[str],
    *,
    cash: float,
    start: datetime,
    end: datetime,
    costs: CostConfig | None = None,
    label: str | None = None,
) -> BacktestResult | None:
    """Run an unconstrained comparison strategy, or warn and return ``None``.

    Generalized beyond the original single-symbol ``buy_and_hold`` benchmark
    (ADR-0037) to also drive the ADR-0071 diversified baseline —
    ``strategy_name``/``symbols`` let a caller run any registered strategy over
    any universe under the same unconstrained-guardrail/same-cost machinery,
    rather than duplicating this function's error handling a second time.
    ``label`` names the run for display/warnings; it defaults to the
    comma-joined symbols (exactly the old single-symbol behaviour when
    ``symbols`` has one element and ``label`` is omitted).

    ``costs`` is the selected market's cost model (ADR-0060) and the comparison
    run pays it too. It is *unconstrained* in the guardrail sense only —
    ADR-0037's point is that the comparison must not be clamped — but a run
    exempt from the venue's fees would be a different thing entirely: on a
    25 bps venue it would beat the strategy by the fees the strategy paid and it
    did not, and ADR-0039's paired bootstrap reads that curve.

    The comparison is bolted onto a backtest that has *already* finished, so one
    that cannot be run must cost one warning line, not the summary, the equity
    CSV, and the result.json the operator asked for. Previously the exception
    escaped after the main run had succeeded and the whole command died with a
    traceback.

    Exactly one exception type is tolerated, and the narrowness is deliberate.
    After ADR-0032 every way the comparison's *data* can fail arrives here as
    :class:`~trading.engine.EmptyUniverseError`: a mistyped ticker, a symbol that
    had not listed in the range, and a transport/credentials/unreadable-file
    failure all end up as an :class:`~trading.engine.AbsentSymbol` inside
    :func:`~trading.engine.load_series`, and a universe with nothing in it
    raises. The three failure shapes therefore share one handler *and* stay
    distinguishable, because the error message carries the per-symbol reason.
    Anything else — a broken guardrail, a sizing crash, a misbehaving broker —
    means the bench itself is faulty, which makes the strategy numbers suspect
    too, so it is left to propagate.
    """
    display = label if label is not None else ", ".join(symbols)
    bench_broker = SimulatedBroker(Portfolio(cash=cash), costs)
    engine = Engine(adapter, bench_broker, Guardrails(RiskConfig.unlimited()))
    try:
        bench = engine.run(get_strategy(strategy_name), symbols, start, end)
    except EmptyUniverseError as exc:
        typer.echo(
            f"warning: {display} could not be run, continuing without it — {exc}",
            err=True,
        )
        return None
    _warn_if_benchmark_never_invested(display, bench)
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


def _check_monte_carlo_options(*, monte_carlo: bool, resamples: int) -> None:
    """Reject a bad ``--monte-carlo-resamples`` **before** the backtest runs.

    Mirrors :func:`_check_bootstrap_options` exactly, and for the same reason: the
    shuffle happens after the engine has finished, so validating it there would let
    a typo throw away a completed multi-year run and write nothing.
    """
    if monte_carlo and resamples < 1:
        typer.echo(
            f"error: --monte-carlo-resamples must be >= 1, got {resamples}",
            err=True,
        )
        raise typer.Exit(2)


def _check_cost_budget_options(cost_budget_pct: float | None) -> None:
    """Reject a non-positive ``--cost-budget-pct`` **before** the backtest runs.

    Mirrors :func:`_check_bootstrap_options`/:func:`_check_monte_carlo_options`:
    :func:`~trading.metrics.assess_cost_budget` raises ``ValueError`` on a
    non-positive budget, and that check happens after the engine has finished, so
    validating here avoids throwing away a completed multi-year run and writing
    nothing.
    """
    if cost_budget_pct is not None and cost_budget_pct <= 0:
        typer.echo(
            f"error: --cost-budget-pct must be positive, got {cost_budget_pct}",
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
    prior_trials: int = 0,
) -> SignificanceReport:
    """Run the ADR-0039 bootstrap for a finished backtest, or exit 2 on a bad knob.

    The benchmark's curve is what turns on the *paired* win rate — the figure that
    says whether the strategy beat the alternative rather than merely beating zero —
    so it is passed through whenever ``--benchmark`` produced a run. When it did not
    (absent, or warned away by :func:`_run_benchmark`), ``assess_significance``
    records a note explaining the absence instead of quietly omitting a row.

    ``prior_trials`` (ADR-0062) is a :class:`~trading.ledger.TrialLedger`'s
    cumulative count from earlier logged invocations; ``0`` (the default) is
    exactly today's behaviour.

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
            prior_trials=prior_trials,
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
    market: str = typer.Option(
        DEFAULT_MARKET,
        "--market",
        help=MARKET_OPTION_HELP,
    ),
    seed: int = typer.Option(0, "--seed", help="RNG seed when --source synthetic."),
    cash: float = typer.Option(1_000.0, "--cash", help="Starting cash."),
    max_position: float | None = typer.Option(None, "--max-position", help=MAX_POSITION_HELP),
    max_gross: float | None = typer.Option(None, "--max-gross", help=MAX_GROSS_HELP),
    max_drawdown: float | None = typer.Option(None, "--max-drawdown", help=MAX_DRAWDOWN_HELP),
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
        help=HALT_COOLDOWN_HELP,
    ),
    slippage_bps: float | None = typer.Option(None, "--slippage-bps", help=SLIPPAGE_HELP),
    taker_fee_bps: float | None = typer.Option(None, "--taker-fee-bps", help=TAKER_FEE_HELP),
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
    diversified_baseline: bool = typer.Option(
        False,
        "--diversified-baseline/--no-diversified-baseline",
        help=(
            "Also compare against a naive equal_weight allocation across "
            "--baseline-basket (ADR-0071). Off by default."
        ),
    ),
    baseline_basket: str = typer.Option(
        "@core10",
        "--baseline-basket",
        help=(
            "Symbols for --diversified-baseline's equal_weight comparison; accepts "
            "@name (e.g. @core10) or a plain comma list. Ignored without "
            "--diversified-baseline."
        ),
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
    liquidity_tier_adv: float | None = typer.Option(
        None,
        "--liquidity-tier-adv",
        help=LIQUIDITY_TIER_ADV_HELP,
    ),
    liquidity_tier_slippage_bps: float = typer.Option(
        LIQUID_TIER_SLIPPAGE_BPS,
        "--liquidity-tier-slippage-bps",
        help=LIQUIDITY_TIER_SLIPPAGE_HELP,
    ),
    cost_budget_pct: float | None = typer.Option(
        None,
        "--cost-budget-pct",
        help=COST_BUDGET_HELP,
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
    regimes: bool = typer.Option(
        False,
        "--regimes/--no-regimes",
        help=(
            "Split the metrics by the run's own high/low-volatility and "
            "trending/mean-reverting bars (ADR-0066). Off by default; a run without "
            "the flag is byte-identical to before it existed."
        ),
    ),
    monte_carlo: bool = typer.Option(
        False,
        "--monte-carlo/--no-monte-carlo",
        help=(
            "Reshuffle the run's own per-bar returns into thousands of random "
            "reorderings and place the actual max drawdown against that distribution "
            "(ADR-0067). Off by default; a run without the flag is byte-identical to "
            "before it existed."
        ),
    ),
    monte_carlo_resamples: int = typer.Option(
        DEFAULT_BOOTSTRAP_RESAMPLES,
        "--monte-carlo-resamples",
        help="Random reorderings to draw (needs --monte-carlo). Cost is linear in this.",
    ),
    monte_carlo_seed: int = typer.Option(
        DEFAULT_BOOTSTRAP_SEED,
        "--monte-carlo-seed",
        help="Seed for the shuffle RNG (needs --monte-carlo). Printed with the report, "
        "so the figure is reproducible.",
    ),
    ledger: Path | None = typer.Option(
        None,
        "--ledger",
        help=LEDGER_HELP,
    ),
    hypothesis: str = typer.Option(
        "",
        "--hypothesis",
        help=HYPOTHESIS_HELP,
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
    tickers = _parse_symbols(symbols, as_of=start)
    chosen_market = _resolve_market(market)
    _check_symbol_shapes(chosen_market, tickers)
    freq = _parse_frequency(interval, chosen_market.calendar)
    _check_bootstrap_options(bootstrap=bootstrap, resamples=bootstrap_resamples)
    _check_monte_carlo_options(monte_carlo=monte_carlo, resamples=monte_carlo_resamples)
    _check_cost_budget_options(cost_budget_pct)

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        risk = _build_risk(
            no_guardrails=no_guardrails,
            posture=chosen_market.posture,
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

    try:
        costs = _build_costs(chosen_market.costs, slippage_bps, taker_fee_bps)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    adapter = _make_adapter(source, cache_dir, seed, freq)
    if min_adv is not None:
        tickers = _apply_liquidity_screen(adapter, tickers, start, min_adv, adv_window)
    if liquidity_tier_adv is not None:
        tiered_rates = _apply_liquidity_tiering(
            adapter,
            tickers,
            start,
            tier_adv_floor=liquidity_tier_adv,
            tier_slippage_bps=liquidity_tier_slippage_bps,
            formation_days=adv_window,
        )
        costs = CostConfig(
            commission_per_share=costs.commission_per_share,
            slippage_bps=costs.slippage_bps,
            taker_fee_bps=costs.taker_fee_bps,
            symbol_slippage_bps=tiered_rates,
        )
    broker = SimulatedBroker(Portfolio(cash=cash), costs)
    result = Engine(adapter, broker, Guardrails(risk)).run(strat, tickers, start, end)

    # Optional buy-and-hold benchmark on the same dates/source, run UNCONSTRAINED
    # (unlimited guardrails) so the benchmark itself is never clamped (Q24).
    bench_result: BacktestResult | None = None
    bench_symbol = benchmark.strip().upper()
    if bench_symbol:
        bench_result = _run_benchmark(
            adapter, "buy_and_hold", [bench_symbol], cash=cash, start=start, end=end, costs=costs
        )

    # Optional diversified baseline (ADR-0071, KAN-641): a second, independent
    # comparison — naive equal-weight across a multi-asset basket — reusing
    # exactly the same unconstrained-guardrail/same-cost machinery as
    # --benchmark above, generalized to a strategy + symbol list rather than
    # buy_and_hold + one symbol.
    diversified_baseline_report: DiversifiedBaselineReport | None = None
    if diversified_baseline:
        baseline_symbols = _parse_symbols(baseline_basket, as_of=start)
        _check_symbol_shapes(chosen_market, baseline_symbols)
        baseline_display = (
            baseline_basket[1:] if baseline_basket.startswith("@") else ", ".join(baseline_symbols)
        )
        baseline_label = f"equal_weight/{baseline_display}"
        baseline_result = _run_benchmark(
            adapter,
            "equal_weight",
            baseline_symbols,
            cash=cash,
            start=start,
            end=end,
            costs=costs,
            label=baseline_label,
        )
        if baseline_result is not None:
            diversified_baseline_report = assess_diversified_baseline(
                result, baseline_result, freq.periods_per_year, label=baseline_label
            )

    # Read BEFORE the significance is assembled, not after: the whole point of the
    # ledger (ADR-0062) is that this invocation's own trial count is widened by
    # every earlier one it cannot otherwise see. A ledger that does not exist yet
    # (the very first logged run) reads as 0, so it widens nothing.
    prior_trials = TrialLedger(ledger).cumulative_trials() if ledger is not None else 0

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
            prior_trials=prior_trials,
        )
        if bootstrap
        else None
    )

    # The strategy's tunable-argument count turns on the trades-per-parameter
    # sample-size check and its warning (ADR-0029).
    free_params = free_parameter_count(strat)
    # Empty on the equity default, so an equity run's stdout is untouched (ADR-0057).
    market_line = _market_line(chosen_market, freq)
    if market_line:
        typer.echo(market_line)

    # Computed ONCE here and handed to both the text summary and result.json
    # (ADR-0066, the same shape ADR-0039's bootstrap already uses): neither
    # `summarize` nor `write_result_json` ever derives it, so a run without
    # --regimes pays nothing and prints exactly the bytes it always did.
    regime_report = (
        compute_regime_report(result, freq.periods_per_year, free_parameters=free_params)
        if regimes
        else None
    )

    # Computed ONCE here and handed to both the text summary and result.json
    # (ADR-0067, the same shape --bootstrap/--regimes already use): neither
    # `summarize` nor `write_result_json` ever derives it, so a run without
    # --monte-carlo pays nothing and prints exactly the bytes it always did.
    monte_carlo_report = (
        monte_carlo_shuffle(
            result.equity_curve,
            freq.periods_per_year,
            resamples=monte_carlo_resamples,
            seed=monte_carlo_seed,
        )
        if monte_carlo
        else None
    )

    # Computed ONCE here and handed to both the text summary and result.json
    # (ADR-0068, the same shape --bootstrap/--regimes/--monte-carlo already use):
    # neither `summarize` nor `write_result_json` ever derives it, so a run without
    # --cost-budget-pct pays nothing and prints exactly the bytes it always did.
    # `costs` is the run's own CostConfig -- including any --liquidity-tier-adv
    # override -- so the effective rate reflects what this run actually traded at.
    cost_budget_report: CostBudgetReport | None = (
        assess_cost_budget(result, costs, cost_budget_pct, freq.periods_per_year)
        if cost_budget_pct is not None
        else None
    )

    typer.echo(
        summarize(
            result,
            bench_result,
            periods_per_year=freq.periods_per_year,
            free_parameters=free_params,
            significance=significance,
            regimes=regime_report,
            monte_carlo=monte_carlo_report,
            cost_budget=cost_budget_report,
            diversified_baseline=diversified_baseline_report,
        )
    )
    write_equity_csv(result, out, bench_result)
    typer.echo(f"\nWrote equity curve to {out}")

    # The canonical machine-readable artifact the dashboard consumes, alongside
    # the CSV. Metrics are computed once here at the run's frequency (default
    # 252/yr for daily keeps the numbers identical).
    metrics = compute_metrics(result, freq.periods_per_year, free_parameters=free_params)

    # Appended whether or not --bootstrap ran: the ledger's own bookkeeping is a
    # count of trials, not a significance figure, so a plain backtest still adds
    # its one trial for a LATER invocation's --ledger to see (ADR-0062). Only the
    # deflation math above is gated on --bootstrap; recording never is.
    if ledger is not None:
        TrialLedger(ledger).append(
            TrialRecord(
                timestamp=datetime.now(UTC).isoformat(),
                command="backtest",
                strategy=strategy,
                symbols=tuple(sorted(tickers)),
                date_from=from_,
                date_to=to,
                interval=interval,
                market=chosen_market.name,
                trial_count=1,
                observed_sharpe=metrics.sharpe,
                hypothesis=hypothesis,
            )
        )

    result_json = out.parent / "result.json"
    write_result_json(
        result,
        result_json,
        mode="backtest",
        frequency=freq.label,
        # An interval label alone cannot say whether "1d" meant 252 bars a year or
        # 365, and every risk-adjusted figure in the document depends on which
        # (ADR-0054's own recorded gap, ADR-0057).
        market=chosen_market.name,
        metrics=metrics,
        benchmark_curve=bench_result.equity_curve if bench_result is not None else None,
        significance=significance,
        regimes=regime_report,
        monte_carlo=monte_carlo_report,
        cost_budget=cost_budget_report,
        diversified_baseline=diversified_baseline_report,
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
    tickers = _parse_symbols(symbols, as_of=start)

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


def _format_bar(outcome: BarOutcome, frequency: Frequency) -> str:
    """One human-readable status line for a newly completed paper bar.

    The leading stamp is rendered at the session's ``frequency``: a bare date for
    daily (exactly as before intraday existed), date **and** ``%H:%M`` for anything
    sub-daily — the same rendering the ADR-0042 warmup line uses for its span.
    Without the time, a ``--interval 5m`` session prints ~78 identical stamps per
    symbol-day, so the operator watching stdout cannot tell which bar is which, when
    a fill happened, or whether the session is progressing or wedged; and this line
    is also what goes to ``paper_session.log``, the only per-bar artifact that
    survives mid-session, so the after-the-fact record had no time axis either.

    The frequency is *passed in*, not sniffed off ``outcome.ts``. A non-midnight
    timestamp would have implied intraday with no plumbing at all, but that is
    inference rather than fact — it would silently re-format a daily bar stamped at
    a session time — and ADR-0022's whole point is that the interval is carried
    deliberately rather than guessed. The caller already has it in scope.
    """
    day = _format_bar_stamp(outcome.ts, frequency)
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


def _format_bar_stamp(ts: datetime, frequency: Frequency) -> str:
    """The leading timestamp of a per-bar line, at ``frequency``'s resolution."""
    if frequency.is_intraday:
        return f"{ts:%Y-%m-%d %H:%M}"
    return ts.date().isoformat()


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
    market: str = typer.Option(
        DEFAULT_MARKET,
        "--market",
        help=MARKET_OPTION_HELP,
    ),
    seed: int = typer.Option(0, "--seed", help="RNG seed when --source synthetic."),
    cash: float = typer.Option(1_000.0, "--cash", help="Starting cash."),
    max_position: float | None = typer.Option(None, "--max-position", help=MAX_POSITION_HELP),
    max_gross: float | None = typer.Option(None, "--max-gross", help=MAX_GROSS_HELP),
    max_drawdown: float | None = typer.Option(None, "--max-drawdown", help=MAX_DRAWDOWN_HELP),
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
        help=HALT_COOLDOWN_HELP,
    ),
    slippage_bps: float | None = typer.Option(None, "--slippage-bps", help=SLIPPAGE_HELP),
    taker_fee_bps: float | None = typer.Option(None, "--taker-fee-bps", help=TAKER_FEE_HELP),
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
    tickers = _parse_symbols(symbols, as_of=start)
    chosen_market = _resolve_market(market)
    _check_symbol_shapes(chosen_market, tickers)
    freq = _parse_frequency(interval, chosen_market.calendar)

    try:
        strat = get_strategy(strategy)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    try:
        risk = _build_risk(
            no_guardrails=no_guardrails,
            posture=chosen_market.posture,
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
    #
    # **Equity only** (ADR-0058). ADR-0034 predates any second venue, and a tape
    # choice is a property of the equity market data API: `CryptoBarsRequest` has no
    # `feed` field, crypto market data needs no subscription at all, and the venue
    # was measured serving a 5m bar 3m54s old — so there is nothing here for the
    # free-plan restriction to work around. Left unguarded this made
    # `paper --market crypto --live` impossible: the client refuses feed+crypto, so
    # the run died at construction. That refusal is the guard working — loud, before
    # any network call — but the default is what had no business being set.
    if (
        data_feed is None
        and live
        and source == "alpaca"
        and not chosen_market.calendar.is_continuous
    ):
        data_feed = "iex"
    try:
        costs = _build_costs(chosen_market.costs, slippage_bps, taker_fee_bps)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    adapter = _make_adapter(source, cache_dir, seed, freq, data_feed)
    broker = _make_paper_broker(broker_name, live, cash, chosen_market.calendar, costs)

    # The clock and feed are the *only* difference between backtest and paper
    # (ADR-0002/0014). Live: wall clock over a recent-window feed, runs until
    # interrupted. Once: materialize the [from, to] bars into an in-memory adapter
    # and a fake clock parked just past the range so every bar reads as complete —
    # the loop drains them one _step at a time and stops, offline and deterministic.
    # Sub-daily bars need the interval-aware completeness policy (ADR-0022); daily
    # keeps the default policy so the daily path stays byte-identical to V5 — unless
    # the market never closes, which drops the daily special case entirely because a
    # session rule has nothing to ask about there (ADR-0053, selected by ADR-0057).
    is_complete = _completeness_policy(chosen_market, freq)
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
        # The counterfactual runs the market's own cost model (ADR-0060), so the
        # report measures the model this market would actually have used. Note what
        # it still cannot see: the crypto term is a fee on notional, and ADR-0038's
        # statistic is a ratio of fill price to reference price, so a 25 bps fee
        # moves no reported bps figure. The summary therefore *states* the modelled
        # fee alongside the slippage it can measure, rather than letting a clean
        # slippage verdict read as a validated cost model (KAN-710 owns the fix).
        shadow = ShadowBroker(
            broker,
            clock,
            costs=costs,
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
    # Empty on the equity default, so both paper modes' stdout is untouched there.
    market_line = _market_line(chosen_market, freq)
    if market_line:
        typer.echo(market_line + "\n")
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
                line = _format_bar(outcome, freq)
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
        write_result_json(
            result,
            result_json,
            mode="paper",
            frequency=freq.label,
            market=chosen_market.name,
            metrics=metrics,
        )

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


def _parse_bps_grid(raw: str) -> list[float]:
    """Parse ``--slippage-sweep 5,10,25,50`` into a list of non-negative floats.

    A malformed entry, an empty grid, or a negative level exits with code 2 —
    the same "fail before running anything" discipline ``_parse_grid`` and
    ``_parse_date`` already use.
    """
    values: list[float] = []
    for chunk in raw.split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            typer.echo(
                f"error: --slippage-sweep must be a comma-separated list of numbers, got {text!r}",
                err=True,
            )
            raise typer.Exit(2) from None
        if value < 0:
            typer.echo(f"error: --slippage-sweep level {value:g} must be non-negative", err=True)
            raise typer.Exit(2)
        values.append(value)
    if not values:
        typer.echo("error: --slippage-sweep has no values", err=True)
        raise typer.Exit(2)
    return values


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


def _stability_csv_path(out: Path) -> Path:
    """The neighbour-stability report's path, a sibling of the main sweep CSV.

    ``results/sweep.csv`` -> ``results/sweep_stability.csv``: no new ``--out``-style
    flag, matching how ``paper --divergence`` writes ``fill_divergence.csv`` beside
    the rest of a session's artifacts rather than taking its own path option.
    """
    suffix = out.suffix or ".csv"
    return out.with_name(f"{out.stem}_stability{suffix}")


def _write_stability_csv(
    rows: list[NeighborStability], path: Path, rank_by: str, param_keys: list[str]
) -> None:
    """Write each combo's score next to its grid-neighbour mean (ADR-0065, KAN-620).

    One row per unique combo — window repeats are already collapsed to their mean by
    :meth:`~trading.sweep.SweepSummary.stability` — ranked the same way as the main
    sweep CSV (best ``by``-score first) so the two files line up rank for rank.
    ``neighbor_mean``/``gap`` are blank, never ``0``, when a combo has no in-grid
    neighbour with a recorded score.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ranked = sorted(rows, key=lambda row: row.score, reverse=True)
    header = ["rank", *param_keys, rank_by, "neighbor_mean", "neighbor_count", "gap"]
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for rank, row in enumerate(ranked, start=1):
            writer.writerow(
                [
                    rank,
                    *(row.params.get(key, "") for key in param_keys),
                    round(row.score, 6),
                    "" if row.neighbor_mean is None else round(row.neighbor_mean, 6),
                    row.neighbor_count,
                    "" if row.gap is None else round(row.gap, 6),
                ]
            )


def _format_stability_heatmap(
    rows: list[NeighborStability], param_keys: list[str], grid: dict[str, list[object]]
) -> str:
    """A 2-axis score matrix: rows are ``param_keys[0]``, columns ``param_keys[1]``.

    A literal ASCII heatmap, only meaningful for a two-parameter grid (with more
    axes there is no single 2D picture to draw, so callers gate this on
    ``len(param_keys) == 2``). A blank cell (``.``) is a combo the strategy
    constructor rejected or that never ran — never a fabricated score.
    """
    row_key, col_key = param_keys
    row_values = grid[row_key]
    col_values = grid[col_key]
    scores = {combo_key(row.params): row.score for row in rows}

    header = [f"{row_key}\\{col_key}"] + [_format_param(v) for v in col_values]
    body: list[list[str]] = []
    for rv in row_values:
        cells = [_format_param(rv)]
        for cv in col_values:
            score = scores.get(combo_key({row_key: rv, col_key: cv}))
            cells.append("." if score is None else f"{score:+.2f}")
        body.append(cells)

    widths = [len(h) for h in header]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header))]
    lines.append("  ".join("-" * widths[i] for i in range(len(header))))
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in body)
    return "\n".join(lines)


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


def _sweep_significance_block(
    summary: SweepSummary,
    rank_by: str,
    periods_per_year: float,
    *,
    prior_trials: int = 0,
) -> str:
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

    ``prior_trials`` (ADR-0062) is a :class:`~trading.ledger.TrialLedger`'s
    cumulative count from earlier logged invocations; ``0`` (the default)
    reproduces the pre-ledger behaviour exactly.

    ``""`` when the summary has no runs, or when the winner's moments were not
    recorded — an honest absence rather than a fabricated figure.
    """
    deflated = summary.deflated_winner(rank_by, periods_per_year, prior_trials=prior_trials)
    if deflated is None:
        return ""
    return summarize_significance(
        SignificanceReport(
            deflated=deflated,
            notes=[trial_count_note(deflated.trials, prior_trials=prior_trials)],
        )
    )


def _format_param(value: object) -> str:
    """Compact rendering of one parameter value for the table (empty for None)."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _cost_sensitivity_csv_path(out: Path) -> Path:
    """The cost-sensitivity report's path, a sibling of the main sweep CSV.

    ``results/sweep.csv`` -> ``results/sweep_cost_sensitivity.csv``, the same
    no-new-``--out``-flag idiom :func:`_stability_csv_path` (ADR-0065) and
    ``paper --divergence``'s ``fill_divergence.csv`` already use.
    """
    suffix = out.suffix or ".csv"
    return out.with_name(f"{out.stem}_cost_sensitivity{suffix}")


def _write_cost_sensitivity_csv(summary: CostSensitivitySummary, path: Path) -> None:
    """Write one row per slippage level, ascending, with the same metric columns
    :func:`_write_sweep_csv` uses so the two files read the same way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(summary.runs, key=lambda run: run.slippage_bps)
    header = ["slippage_bps", "taker_fee_bps"] + [name for _attr, name in _SWEEP_METRIC_COLUMNS]
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for run in ordered:
            row: list[object] = [run.slippage_bps, run.taker_fee_bps]
            row.extend(
                round(getattr(run.metrics, attr), 6) for attr, _name in _SWEEP_METRIC_COLUMNS
            )
            writer.writerow(row)


def _format_cost_sensitivity_table(summary: CostSensitivitySummary) -> str:
    """Render one row per slippage level, ascending, as a plain-text table."""
    ordered = sorted(summary.runs, key=lambda run: run.slippage_bps)
    headers = ["slippage_bps", "sharpe", "total_return", "max_drawdown"]
    rows: list[list[str]] = []
    for run in ordered:
        rows.append(
            [
                f"{run.slippage_bps:g}",
                f"{run.metrics.sharpe:.3f}",
                f"{run.metrics.total_return * 100:.2f}%",
                f"{run.metrics.max_drawdown * 100:.2f}%",
            ]
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    lines.extend("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return "\n".join(lines)


def _format_edge_death(death: EdgeDeath) -> str:
    """One human-readable line naming exactly where the edge died (KAN-618).

    The concrete number the ticket asks for, not a table the reader has to
    eyeball: an interpolated bps level, or one of the two honest non-answers
    (:attr:`EdgeDeath.already_dead` / :attr:`EdgeDeath.survives_grid`).
    """
    label = "Sharpe" if death.metric == "sharpe" else "total return"
    threshold = f"{death.threshold:g}"
    if death.already_dead:
        return (
            f"Edge already dead: {label} is at/below {threshold} at the cheapest "
            f"level tested ({death.crossing_bps:g} bps)."
        )
    if death.survives_grid:
        return (
            f"Edge survives this grid: {label} never crosses {threshold} within the levels tested."
        )
    assert death.crossing_bps is not None  # narrowed by the two branches above
    return (
        f"Edge dies (~{label} crosses {threshold}) at ~{death.crossing_bps:.2f} bps (interpolated)."
    )


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
    market: str = typer.Option(
        DEFAULT_MARKET,
        "--market",
        help=MARKET_OPTION_HELP,
    ),
    seed: int = typer.Option(0, "--seed", help="RNG seed when --source synthetic."),
    cash: float = typer.Option(1_000.0, "--cash", help="Starting cash per run."),
    max_position: float | None = typer.Option(None, "--max-position", help=MAX_POSITION_HELP),
    max_gross: float | None = typer.Option(None, "--max-gross", help=MAX_GROSS_HELP),
    max_drawdown: float | None = typer.Option(None, "--max-drawdown", help=MAX_DRAWDOWN_HELP),
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
        help=HALT_COOLDOWN_HELP,
    ),
    slippage_bps: float | None = typer.Option(None, "--slippage-bps", help=SLIPPAGE_HELP),
    taker_fee_bps: float | None = typer.Option(None, "--taker-fee-bps", help=TAKER_FEE_HELP),
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
    ledger: Path | None = typer.Option(
        None,
        "--ledger",
        help=LEDGER_HELP,
    ),
    hypothesis: str = typer.Option(
        "",
        "--hypothesis",
        help=HYPOTHESIS_HELP,
    ),
    cache_dir: Path = typer.Option(Path(".cache/data"), "--cache-dir"),
    out: Path = typer.Option(Path("results/sweep.csv"), "--out", help="Results CSV path."),
    stability: bool = typer.Option(
        False,
        "--stability",
        help=(
            "Report each combo's score next to the mean of its immediate grid "
            "neighbours, to surface a 'cliff' — a combo that scored far above "
            "neighbours a real search would not reliably land on (ADR-0065). Writes "
            "a sibling *_stability.csv next to --out; off by default. Not yet wired "
            "into --folds walk-forward."
        ),
    ),
    slippage_sweep: str = typer.Option(
        "",
        "--slippage-sweep",
        help=SLIPPAGE_SWEEP_HELP,
    ),
) -> None:
    """Grid-sweep a strategy's parameters over a date range, ranked by a metric.

    Runs the backtest engine once per parameter combination (an OUTER loop, not an
    engine feature — ADR-0016), computes the same metrics as ``backtest``, prints a
    ranked table, and writes a results CSV. Deterministic and offline-capable with
    ``--source synthetic``. ``--windows N`` adds a simple per-window walk-forward.
    """
    start = _parse_date("--from", from_)
    end = _parse_date("--to", to)
    tickers = _parse_symbols(symbols, as_of=start)
    chosen_market = _resolve_market(market)
    _check_symbol_shapes(chosen_market, tickers)
    freq = _parse_frequency(interval, chosen_market.calendar)
    grid = _parse_grid(param)
    slippage_grid = _parse_bps_grid(slippage_sweep) if slippage_sweep else None
    if rank_by not in {"sharpe", "total_return"}:
        typer.echo(
            f"error: --rank-by must be 'sharpe' or 'total_return', got {rank_by!r}", err=True
        )
        raise typer.Exit(2)

    if slippage_grid is not None and slippage_bps is not None:
        typer.echo(
            "error: --slippage-bps and --slippage-sweep are mutually exclusive; use "
            "--slippage-sweep to test a grid of rates instead of overriding a single one",
            err=True,
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
            posture=chosen_market.posture,
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

    try:
        costs = _build_costs(chosen_market.costs, slippage_bps, taker_fee_bps)
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

    market_line = _market_line(chosen_market, freq)
    if market_line:
        typer.echo(market_line + "\n")

    adapter = _make_adapter(source, cache_dir, seed, freq)

    if folds > 0:
        if ledger is not None:
            # KAN-677: walk-forward prints no deflation of its own yet, so there is
            # nothing here for --ledger to widen or contribute to. Named rather
            # than silently ignored (ADR-0062).
            typer.echo(
                "note: --ledger is not yet wired into --folds walk-forward (KAN-677); "
                "nothing was appended",
                err=True,
            )
        if stability:
            # ADR-0065: neighbour stability reads a plain grid sweep's SweepSummary;
            # a walk-forward fold does not carry one yet (a later slice, not this
            # one), so say so rather than silently ignoring the flag.
            typer.echo(
                "note: --stability is not yet wired into --folds walk-forward "
                "(ADR-0065); nothing was written",
                err=True,
            )
        if slippage_grid is not None:
            # KAN-618: cost-sensitivity re-runs a plain sweep's single winning
            # combo; a walk-forward fold has no single winner across the whole
            # range to re-run at each cost level, same shape as the two gaps above.
            typer.echo(
                "note: --slippage-sweep is not yet wired into --folds walk-forward "
                "(ADR-0069); nothing was written",
                err=True,
            )
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
            costs=costs,
            out=out,
            periods_per_year=freq.periods_per_year,
        )
        return

    # Read BEFORE the deflation is scored, same as backtest's --ledger (ADR-0062).
    prior_trials = TrialLedger(ledger).cumulative_trials() if ledger is not None else 0

    # The run's own basis, from the --interval x --market Frequency: the sweep's
    # metrics used to take metrics.compute's 252.0 whatever the bars were (KAN-840).
    summary = run_sweep(
        strategy,
        grid,
        adapter,
        tickers,
        start,
        end,
        cash=cash,
        risk=risk,
        costs=costs,
        windows=windows,
        periods_per_year=freq.periods_per_year,
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
        deflation = _sweep_significance_block(
            summary, rank_by, freq.periods_per_year, prior_trials=prior_trials
        )
        if deflation:
            typer.echo("\n" + deflation)
        _write_sweep_csv(summary, out, rank_by, param_keys)
        typer.echo(f"\nWrote sweep results to {out}")

        if stability:
            # Read-only reporting on top of the summary already computed above — no
            # ranking, no metric, and no existing CSV column changes (ADR-0065).
            stability_rows = summary.stability(by=rank_by)
            if stability_rows:
                stability_path = _stability_csv_path(out)
                _write_stability_csv(stability_rows, stability_path, rank_by, param_keys)
                typer.echo(f"Wrote parameter-stability report to {stability_path}")
                if len(param_keys) == 2:
                    typer.echo("\n" + _format_stability_heatmap(stability_rows, param_keys, grid))

        if slippage_grid is not None:
            # KAN-618: re-run the sweep's own winner at every cost level, holding
            # its parameters fixed — read-only reporting on top of the summary
            # already computed above, same shape as --stability.
            winner_params = summary.ranked(rank_by)[0].params
            cost_summary = run_cost_sensitivity_sweep(
                strategy,
                winner_params,
                adapter,
                tickers,
                start,
                end,
                slippage_bps=slippage_grid,
                cash=cash,
                risk=risk,
                base_costs=costs,
                periods_per_year=freq.periods_per_year,
            )
            winner_pretty = ", ".join(f"{k}={_format_param(v)}" for k, v in winner_params.items())
            typer.echo(
                f"\nCost sensitivity: strategy={strategy} params={{{winner_pretty}}} "
                f"levels={len(cost_summary.runs)}\n"
            )
            if cost_summary.runs:
                typer.echo(_format_cost_sensitivity_table(cost_summary))
                death = cost_summary.edge_death(metric=rank_by)
                if death is not None:
                    typer.echo("\n" + _format_edge_death(death))
                cost_path = _cost_sensitivity_csv_path(out)
                _write_cost_sensitivity_csv(cost_summary, cost_path)
                typer.echo(f"Wrote cost-sensitivity report to {cost_path}")
            for level, reason in cost_summary.unusable_levels:
                typer.echo(f"unusable slippage level {level:g} bps: {reason}")

        # Appended after the deflation above so a failure building the report never
        # costs the log entry, and before the caller sees "Wrote sweep results" so
        # the ledger and the CSV land in the same run (ADR-0062).
        if ledger is not None:
            winner = summary.ranked(rank_by)[0]
            TrialLedger(ledger).append(
                TrialRecord(
                    timestamp=datetime.now(UTC).isoformat(),
                    command="sweep",
                    strategy=strategy,
                    symbols=tuple(sorted(tickers)),
                    date_from=from_,
                    date_to=to,
                    interval=interval,
                    market=chosen_market.name,
                    trial_count=len(summary.runs),
                    observed_sharpe=winner.metrics.sharpe,
                    hypothesis=hypothesis,
                )
            )

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
    costs: CostConfig,
    out: Path,
    periods_per_year: float,
) -> None:
    """Run and print a true in-sample -> out-of-sample walk-forward (ADR-0026).

    Prints one line per fold (which parameters in-sample picked, and how they then
    did out-of-sample) followed by the aggregate the whole exercise exists to
    produce: mean OOS performance and the IS->OOS degradation. The out-of-sample
    figures are the honest ones; the in-sample figures are shown only so the gap
    between them is visible.

    ``periods_per_year`` is the run's ``Frequency`` basis, threaded through for the
    same reason the plain sweep threads it (KAN-840). The walk-forward path is the
    quieter half of that defect: every Sharpe on these fold lines was annualized at
    252 whatever ``--interval`` said, and unlike the sweep there is no deflation
    block underneath printing a contradictory figure to notice it by.
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
        costs=costs,
        periods_per_year=periods_per_year,
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
