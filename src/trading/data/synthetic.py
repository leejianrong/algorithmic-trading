"""Deterministic synthetic price data — an offline :class:`DataAdapter` (ADR-0012).

Generates geometric-Brownian-motion bars so the engine, strategies, and CLI can be
exercised end to end without a network or a real provider. Given the same seed,
symbol, and date range it produces byte-identical bars, so synthetic backtests are
reproducible (a domain requirement). There are no corporate actions to model, so
raw == adjusted (ADR-0021): the per-call ``adjusted`` flag does not change the
numbers, and the same series drives both the adjusted backtest feed (ADR-0008) and
the raw paper feed.

The bar cadence is a construction-time :class:`~trading.frequency.Frequency`
(default :data:`~trading.frequency.DAILY`, so existing behaviour and numbers are
unchanged, ADR-0022). Daily bars are stamped at midnight UTC, one per weekday.
Intraday bars are stamped at their START time (ADR-0022 convention) and spaced by
the interval across a nominal regular session — 13:30-20:00 UTC (9:30-16:00 ET) —
for each trading weekday in the range. GBM drift and vol are scaled to the bar via
the frequency's ``periods_per_year``, so the annualized shape is frequency-stable.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from trading.frequency import DAILY, Frequency
from trading.types import Bar

# Nominal regular US-equity cash session in UTC: 13:30-20:00 = 9:30-16:00 ET.
# Intraday bars start at SESSION_OPEN and step by the interval while strictly
# before SESSION_CLOSE (a bar whose start reaches the close is not emitted).
_SESSION_OPEN = time(13, 30, tzinfo=UTC)
_SESSION_CLOSE = time(20, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SyntheticParams:
    """Shape of the generated series (annualized drift/vol, base price/volume)."""

    base_price: float = 100.0
    annual_drift: float = 0.08  # ~8%/yr expected return
    annual_vol: float = 0.20  # 20%/yr volatility
    base_volume: int = 1_000_000

    def __post_init__(self) -> None:
        if self.base_price <= 0:
            raise ValueError("base_price must be positive")
        if self.annual_vol < 0:
            raise ValueError("annual_vol must be non-negative")
        if self.base_volume <= 0:
            raise ValueError("base_volume must be positive")


def _symbol_seed(symbol: str, seed: int) -> int:
    """A stable per-symbol seed, independent of PYTHONHASHSEED."""
    digest = hashlib.sha256(f"{symbol}:{seed}".encode()).hexdigest()
    return int(digest[:16], 16)


def _trading_days(start: datetime, end: datetime) -> Iterator[datetime]:
    """Weekday timestamps (Mon-Fri) at midnight UTC in ``[start, end]``."""
    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    last = datetime(end.year, end.month, end.day, tzinfo=UTC)
    while day <= last:
        if day.weekday() < 5:  # skip Sat/Sun; holidays aren't modeled
            yield day
        day += timedelta(days=1)


def _intraday_starts(start: datetime, end: datetime, interval: timedelta) -> Iterator[datetime]:
    """Bar START times spaced by ``interval`` within each trading day's session.

    For every weekday in ``[start, end]`` (via :func:`_trading_days`), step from
    the session open by ``interval`` while the start is strictly before the
    session close, so a bar's whole ``[ts, ts + interval)`` window is not required
    to fit — only its start must land inside the session (ADR-0022).
    """
    for day in _trading_days(start, end):
        session_open = datetime.combine(day.date(), _SESSION_OPEN)
        session_close = datetime.combine(day.date(), _SESSION_CLOSE)
        ts = session_open
        while ts < session_close:
            yield ts
            ts += interval


class SyntheticAdapter:
    """A :class:`~trading.interfaces.DataAdapter` that fabricates GBM bars.

    The bar cadence is fixed at construction by ``frequency`` (default
    :data:`~trading.frequency.DAILY`); daily construction is byte-identical to the
    original generator (ADR-0022).
    """

    def __init__(
        self,
        seed: int = 0,
        params: SyntheticParams | None = None,
        *,
        frequency: Frequency = DAILY,
    ) -> None:
        self._seed = seed
        self._params = params or SyntheticParams()
        self._frequency = frequency

    def _bar_starts(self, start: datetime, end: datetime) -> Iterator[datetime]:
        """Bar START timestamps for this adapter's frequency over ``[start, end]``."""
        if self._frequency.is_intraday:
            return _intraday_starts(start, end, self._frequency.delta)
        return _trading_days(start, end)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        """Return the deterministic GBM series for ``symbol`` in ``[start, end]``.

        Synthetic GBM has no corporate actions, so raw == adjusted: ``adjusted`` is
        accepted for :class:`DataAdapter` parity but does not change the numbers,
        which lets the offline paper feed (raw, ADR-0021) and the backtest feed
        (adjusted, ADR-0008) drive the identical series.
        """
        p = self._params
        rng = random.Random(_symbol_seed(symbol, self._seed))
        # Scale drift/vol to one bar via the frequency's annualization factor; for
        # DAILY this is 252.0, reproducing the original per-day step exactly.
        periods = self._frequency.periods_per_year
        mu_bar = p.annual_drift / periods
        sigma_bar = p.annual_vol / math.sqrt(periods)

        # A per-symbol starting price so a multi-symbol universe isn't identical.
        prev_close = p.base_price * (0.5 + rng.random())
        bars: list[Bar] = []
        for ts in self._bar_starts(start, end):
            close = prev_close * math.exp(rng.normalvariate(mu_bar, sigma_bar))
            open_ = prev_close * math.exp(rng.normalvariate(0.0, sigma_bar * 0.5))
            high = max(open_, close) * (1.0 + abs(rng.normalvariate(0.0, sigma_bar * 0.5)))
            low = min(open_, close) * (1.0 - abs(rng.normalvariate(0.0, sigma_bar * 0.5)))
            volume = int(p.base_volume * (0.5 + rng.random()))
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=ts,
                    open=round(open_, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close, 4),
                    volume=volume,
                )
            )
            prev_close = close
        return bars
