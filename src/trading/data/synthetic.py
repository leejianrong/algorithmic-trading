"""Deterministic synthetic price data — an offline :class:`DataAdapter` (ADR-0012).

Generates geometric-Brownian-motion daily bars so the engine, strategies, and CLI
can be exercised end to end without a network or a real provider. Given the same
seed, symbol, and date range it produces byte-identical bars, so synthetic
backtests are reproducible (a domain requirement). There are no corporate actions
to model, so raw == adjusted (ADR-0021): the per-call ``adjusted`` flag does not
change the numbers, and the same series drives both the adjusted backtest feed
(ADR-0008) and the raw paper feed.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading.types import Bar

_TRADING_DAYS_PER_YEAR = 252


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


class SyntheticAdapter:
    """A :class:`~trading.interfaces.DataAdapter` that fabricates GBM bars."""

    def __init__(self, seed: int = 0, params: SyntheticParams | None = None) -> None:
        self._seed = seed
        self._params = params or SyntheticParams()

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
        mu_daily = p.annual_drift / _TRADING_DAYS_PER_YEAR
        sigma_daily = p.annual_vol / math.sqrt(_TRADING_DAYS_PER_YEAR)

        # A per-symbol starting price so a multi-symbol universe isn't identical.
        prev_close = p.base_price * (0.5 + rng.random())
        bars: list[Bar] = []
        for ts in _trading_days(start, end):
            close = prev_close * math.exp(rng.normalvariate(mu_daily, sigma_daily))
            open_ = prev_close * math.exp(rng.normalvariate(0.0, sigma_daily * 0.5))
            high = max(open_, close) * (1.0 + abs(rng.normalvariate(0.0, sigma_daily * 0.5)))
            low = min(open_, close) * (1.0 - abs(rng.normalvariate(0.0, sigma_daily * 0.5)))
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
