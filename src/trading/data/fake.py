"""In-memory :class:`~trading.interfaces.DataAdapter` for the fast test layer.

This is the fake half of the DataAdapter seam (dev-playbook principle 2): tests
construct a deterministic bar series in code and inject it, so the fast layer
never touches the network or yfinance. The real, network-backed adapter arrives
with slice V1.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from trading.types import Bar


class FakeAdapter:
    """Serves pre-supplied bars, filtered to a requested date range.

    Bars are grouped by symbol and kept sorted ascending by timestamp so
    ``get_bars`` returns them in the same order the engine will consume them.
    """

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._by_symbol: dict[str, list[Bar]] = {}
        for bar in bars:
            self._by_symbol.setdefault(bar.symbol, []).append(bar)
        for series in self._by_symbol.values():
            series.sort(key=lambda b: b.ts)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        """Return ``symbol``'s bars within ``[start, end]`` inclusive."""
        return [b for b in self._by_symbol.get(symbol, []) if start <= b.ts <= end]
