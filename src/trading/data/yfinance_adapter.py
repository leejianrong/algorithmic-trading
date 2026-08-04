"""yfinance-backed :class:`~trading.interfaces.DataAdapter` with a CSV cache.

Two design choices from the ADRs and the dev-playbook:

- **Adjusted prices** (ADR-0008): the default fetcher asks yfinance for
  split/dividend-adjusted OHLC, so returns are total return.
- **Read-through cache + injectable fetcher** (ADR-0003, dev-playbook seam): the
  network call is a constructor-injected function, so cache behaviour is unit
  tested with a stub and never hits yfinance. On a miss we fetch, write the CSV,
  then *always* rebuild bars from that CSV — so a re-run parses the same bytes and
  is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

from trading.types import Bar

Fetcher = Callable[[str, datetime, datetime], pd.DataFrame]
_COLUMNS = ["open", "high", "low", "close", "volume"]


def cache_filename(symbol: str, start: datetime, end: datetime) -> str:
    """The cache file name for a (symbol, range). Shared so other tools (e.g. the
    synthetic ``gen-data`` command) can write files this adapter will read back."""
    return f"{symbol}_{start:%Y%m%d}_{end:%Y%m%d}_adj.csv"


class DataUnavailableError(Exception):
    """The provider returned no data for a symbol/range."""


def _default_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetch adjusted daily bars from yfinance (the network path)."""
    import yfinance as yf

    raw = yf.download(
        symbol,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=True,
        actions=False,
        progress=False,
    )
    if raw is None or raw.empty:
        raise DataUnavailableError(f"yfinance returned no data for {symbol!r}")

    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    selected: pd.DataFrame = df[_COLUMNS]
    selected.index.name = "ts"
    return selected


class YFinanceAdapter:
    """Serves adjusted daily bars, caching each (symbol, range) to CSV."""

    def __init__(self, cache_dir: Path, fetcher: Fetcher | None = None) -> None:
        self._cache_dir = cache_dir
        self._fetch = fetcher or _default_fetch

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        if not adjusted:
            raise ValueError(
                "YFinanceAdapter serves split/dividend-adjusted prices only "
                "(ADR-0008); it is a backtest source and cannot supply raw quotes. "
                "For RAW live paper quotes use --source alpaca; for an offline demo "
                "use --source synthetic (raw == adjusted there)."
            )

        path = self._cache_path(symbol, start, end)
        if not path.exists():
            df = self._fetch(symbol, start, end)
            self._write_cache(df, path)

        return self._to_bars(symbol, self._read_cache(path))

    def _cache_path(self, symbol: str, start: datetime, end: datetime) -> Path:
        return self._cache_dir / cache_filename(symbol, start, end)

    def _write_cache(self, df: pd.DataFrame, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path)

    def _read_cache(self, path: Path) -> pd.DataFrame:
        return pd.read_csv(path, index_col="ts", parse_dates=["ts"])

    def _to_bars(self, symbol: str, df: pd.DataFrame) -> list[Bar]:
        bars: list[Bar] = []
        for idx, row in df.iterrows():
            ts = pd.Timestamp(idx)  # type: ignore[arg-type]
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            bars.append(
                Bar(
                    symbol=symbol,
                    ts=ts.to_pydatetime(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
            )
        bars.sort(key=lambda b: b.ts)
        return bars
