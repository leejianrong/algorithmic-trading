"""yfinance-backed :class:`~trading.interfaces.DataAdapter` with a CSV cache.

Two design choices from the ADRs and the dev-playbook:

- **Adjusted prices** (ADR-0008): the default fetcher asks yfinance for
  split/dividend-adjusted OHLC, so returns are total return.
- **Read-through cache + injectable fetcher** (ADR-0003, dev-playbook seam): the
  network call is a constructor-injected function, so cache behaviour is unit
  tested with a stub and never hits yfinance. On a miss we fetch, write the CSV,
  then *always* rebuild bars from that CSV — so a re-run parses the same bytes and
  is deterministic.
- **Absence is cached too** (ADR-0032): a symbol with no rows in the window caches
  an empty CSV and returns ``[]``. Previously this raised *before* the cache write,
  so every walk-forward fold re-hit the network to fail again — with each fold
  keying its own ``(symbol, start, end)`` cache file, a 6-fold sweep over a
  universe with four late-listing names paid two dozen doomed network round trips.
- **A refusal is not an absence** (ADR-0040): ``yf.download`` catches *every*
  per-ticker exception internally and hands back an empty frame (``multi.py``'s
  ``_download_one``), so a rate limit arrived here looking exactly like "this
  symbol has no history" and came out of the engine as
  ``REASON_NO_BARS``/``EmptyUniverseError: ... not listed in this window``. That is
  the wrong direction of error for both readings: a 429 masqueraded as a data
  regression, and a real regression could be shrugged off as "just rate limited,
  re-run". :func:`_default_fetch` now probes an empty response and raises
  :class:`ProviderRefusedError` when the provider is refusing us, so the engine
  classifies it as ``REASON_FETCH_FAILED``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

from trading.types import Bar

Fetcher = Callable[[str, datetime, datetime], pd.DataFrame]
_COLUMNS = ["open", "high", "low", "close", "volume"]


class ProviderRefusedError(RuntimeError):
    """The provider declined to answer — a rate limit, not missing history.

    Raised by :func:`probe_refusal` so :func:`trading.engine.load_series` records
    ``REASON_FETCH_FAILED`` rather than ``REASON_NO_BARS`` (ADR-0032's two codes,
    ADR-0040's reason for keeping them honest). "Yahoo said no" and "AAPL had not
    listed yet" must never render as the same sentence.
    """


def cache_filename(symbol: str, start: datetime, end: datetime) -> str:
    """The cache file name for a (symbol, range). Shared so other tools (e.g. the
    synthetic ``gen-data`` command) can write files this adapter will read back."""
    return f"{symbol}_{start:%Y%m%d}_{end:%Y%m%d}_adj.csv"


def _empty_frame() -> pd.DataFrame:
    """An empty, correctly-shaped OHLCV frame — "the provider had no rows"."""
    empty = pd.DataFrame(columns=_COLUMNS)
    empty.index.name = "ts"
    return empty


def probe_refusal(symbol: str, start: datetime, end: datetime) -> None:
    """Re-ask for one symbol so a *refusal* raises instead of reading as absence.

    Public because the nightly provider-contract test needs the same distinction:
    ``yf.download`` returning nothing means either "we were refused" or "there is no
    data", and a test that cannot tell them apart reports a rate limit as a provider
    regression. Raises :class:`ProviderRefusedError` for a refusal; returns for
    anything else.

    Only called when ``yf.download`` came back empty, which is both the rate-limited
    case and the genuinely-no-history case (ADR-0040). ``yf.download`` cannot tell us
    which: it catches every per-ticker exception and substitutes an empty frame.
    ``Ticker.history`` does not swallow ``YFRateLimitError`` — it re-raises it
    unconditionally — while a delisted/not-yet-listed symbol comes back as an empty
    frame with no exception. So the *exception type* answers the question; no log
    scraping and no message matching, which would break on a provider rewording.

    Deliberately conservative: anything other than a refusal leaves the caller's
    "empty means absent" reading intact, because misclassifying a late listing as a
    failure would re-break the multi-decade walk-forward ADR-0032 fixed. One extra
    request, only on the already-empty path, and only on a cache miss — absence is
    cached, so a sweep pays it once per ``(symbol, range)``.
    """
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    try:
        yf.Ticker(symbol).history(
            start=start,
            end=end,
            interval="1d",
            auto_adjust=True,
            actions=False,
        )
    except YFRateLimitError as exc:
        raise ProviderRefusedError(
            f"yfinance refused the request for {symbol} "
            f"({start:%Y-%m-%d}..{end:%Y-%m-%d}): {exc}. This is a provider refusal, "
            "NOT missing history — retry later or use a cached/offline source."
        ) from exc
    except Exception:
        return  # not a refusal we can name; keep the empty-means-absent reading


def _default_fetch(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetch adjusted daily bars from yfinance (the network path).

    An empty response is returned as an **empty frame, not an exception**
    (ADR-0032), *once we have established it is a genuine absence*: the provider has
    no rows for this symbol in this window — a stock that had not listed yet, or a
    fold whose span predates the listing. That is data, and the engine reports it;
    raising here aborted whole multi-decade sweeps over one late-listing symbol.

    ADR-0032 justified that on ``yf.download`` signalling genuine failure by raising.
    It does not — it catches every per-ticker exception and returns an empty frame —
    so an empty response is first probed (:func:`probe_refusal`) and a rate limit
    becomes a :class:`ProviderRefusedError`, i.e. ``REASON_FETCH_FAILED`` (ADR-0040).
    """
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
        probe_refusal(symbol, start, end)
        return _empty_frame()

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
