"""Bring-your-own-data CSV :class:`~trading.interfaces.DataAdapter`.

Reads local CSV files so a real-data backtest needs no network and no provider
account: drop a file per symbol into a directory and point the adapter at it.
The schema is exactly the one :mod:`trading.data.yfinance_adapter`'s cache and the
``trading gen-data`` command emit, so a ``gen-data`` dump is directly replayable.

File layout
-----------
One file per symbol, named ``<SYMBOL>.csv`` with the symbol **uppercased**
(e.g. ``AAPL.csv``), inside the directory passed to the constructor. Each file has
the header row::

    ts,open,high,low,close,volume

where ``ts`` is a calendar date ``YYYY-MM-DD`` (interpreted at midnight UTC, to
match the rest of the bench), ``open``/``high``/``low``/``close`` are floats, and
``volume`` is a non-negative integer.

Adjusted prices
---------------
Prices are assumed to be **already split/dividend adjusted** (ADR-0008); this
adapter does no adjustment of its own. Feeding raw (unadjusted) prices will
produce dishonest, phantom-jump returns, so bring adjusted data.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from trading.types import Bar

_HEADER = ("ts", "open", "high", "low", "close", "volume")


class CsvAdapter:
    """Serves adjusted daily bars from ``<data_dir>/<SYMBOL>.csv`` files."""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        """Return ``symbol``'s bars within ``[start, end]`` inclusive, ascending.

        Reads ``<data_dir>/<SYMBOL>.csv`` (uppercased symbol). Raises
        :class:`FileNotFoundError` if the file is absent and :class:`ValueError`
        if the header or any row is malformed.
        """
        if not adjusted:
            raise ValueError(
                "CsvAdapter serves already-adjusted prices only (ADR-0008); it is a "
                "backtest source and cannot supply raw quotes. For RAW live paper "
                "quotes use --source alpaca; for an offline demo use --source "
                "synthetic (raw == adjusted there)."
            )

        path = self._data_dir / f"{symbol.upper()}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"No CSV for symbol {symbol!r}: expected a file at {path}. "
                f"Create <SYMBOL>.csv with header {','.join(_HEADER)}."
            )

        bars = self._read_bars(symbol, path)
        bars.sort(key=lambda b: b.ts)
        return [b for b in bars if start <= b.ts <= end]

    def _read_bars(self, symbol: str, path: Path) -> list[Bar]:
        with path.open(newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError(f"{path} is empty; expected header {','.join(_HEADER)}") from None

            if tuple(h.strip().lower() for h in header) != _HEADER:
                raise ValueError(
                    f"{path} has unexpected header {header!r}; expected {list(_HEADER)}."
                )

            bars: list[Bar] = []
            # Row 1 is the header, so data rows start at line 2.
            for line_no, row in enumerate(reader, start=2):
                if not row:
                    continue  # tolerate blank trailing lines
                bars.append(self._parse_row(symbol, path, line_no, row))
            return bars

    def _parse_row(self, symbol: str, path: Path, line_no: int, row: list[str]) -> Bar:
        if len(row) != len(_HEADER):
            raise ValueError(
                f"{path}:{line_no}: expected {len(_HEADER)} columns "
                f"({','.join(_HEADER)}), got {len(row)}: {row!r}"
            )
        ts_raw, open_raw, high_raw, low_raw, close_raw, volume_raw = row
        try:
            ts = datetime.strptime(ts_raw.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
            open_ = float(open_raw)
            high = float(high_raw)
            low = float(low_raw)
            close = float(close_raw)
            volume = int(volume_raw)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_no}: malformed row {row!r} ({exc})") from exc

        try:
            return Bar(
                symbol=symbol,
                ts=ts,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        except ValueError as exc:
            # Surface Bar's own invariant failures (e.g. high < low) with location.
            raise ValueError(f"{path}:{line_no}: invalid bar {row!r} ({exc})") from exc
