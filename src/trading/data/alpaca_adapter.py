"""Alpaca-backed :class:`~trading.interfaces.DataAdapter` over the client seam.

A thin adapter that turns the :class:`~trading.data.alpaca_client.AlpacaClient`
seam (ADR-0017) into a :class:`~trading.interfaces.DataAdapter`: ``get_bars``
delegates to :meth:`AlpacaClient.get_daily_bars`. The ``adjusted`` notion is a
**per-mode policy carried by the feed**, not the adapter (ADR-0021): the backtest
feed asks for adjusted total-return prices (ADR-0008), while the paper/live feed
asks for RAW actual quotes so the strategy decides and marks in the same dollars
the :class:`~trading.brokers.alpaca.AlpacaBroker` reconciles from the real
account. So the per-call ``adjusted`` keyword controls each fetch; the constructor
``adjusted`` param only supplies the default used when a caller omits the keyword.
The seam already serves raw via ``Adjustment.RAW``.

Following the injectable-dependency pattern (dev-playbook seam), the client is
constructor-injected -- the fast test layer passes a
:class:`~trading.data.alpaca_client.FakeAlpacaClient`, so no test needs the
network, a key, or the SDK. When no client is supplied, a
:class:`~trading.data.alpaca_client.RealAlpacaClient` is built lazily and its
import is deferred, so importing this module never requires credentials or
``alpaca-py`` (ADR-0018).
"""

from __future__ import annotations

from datetime import datetime

from trading.data.alpaca_client import AlpacaClient
from trading.types import Bar


class AlpacaAdapter:
    """Serves adjusted daily bars from Alpaca through the client seam."""

    def __init__(self, client: AlpacaClient | None = None, *, adjusted: bool = True) -> None:
        if client is None:
            from trading.data.alpaca_client import RealAlpacaClient

            client = RealAlpacaClient()
        self._client = client
        self._adjusted = adjusted

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool | None = None,
    ) -> list[Bar]:
        """Return ``symbol``'s daily bars in ``[start, end]``, ascending by time.

        The per-call ``adjusted`` keyword controls the fetch (ADR-0021): pass
        ``True`` for split/dividend-adjusted total-return prices (the backtest
        feed) or ``False`` for RAW actual quotes (the paper/live feed). When it is
        omitted the constructor default applies. The seam already returns
        ascending, range-filtered bars; we re-filter and sort here defensively
        (both cheap).
        """
        effective = self._adjusted if adjusted is None else adjusted
        bars = self._client.get_daily_bars(symbol, start, end, adjusted=effective)
        filtered = [b for b in bars if start <= b.ts <= end]
        filtered.sort(key=lambda b: b.ts)
        return filtered
