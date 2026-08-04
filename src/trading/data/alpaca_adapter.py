"""Alpaca-backed :class:`~trading.interfaces.DataAdapter` over the client seam.

A thin adapter that turns the :class:`~trading.data.alpaca_client.AlpacaClient`
seam (ADR-0017) into a :class:`~trading.interfaces.DataAdapter`: ``get_bars``
simply delegates to :meth:`AlpacaClient.get_daily_bars`. The ``adjusted`` policy
is fixed at construction and defaults to adjusted prices (ADR-0008).

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
        adjusted: bool = True,
    ) -> list[Bar]:
        """Return ``symbol``'s daily bars in ``[start, end]``, ascending by time.

        Delegates to the seam with the adapter's fixed ``adjusted`` policy; the
        per-call ``adjusted`` keyword exists only for :class:`DataAdapter`
        signature parity and is ignored. The seam already returns ascending,
        range-filtered bars; we re-filter and sort here defensively (both cheap).
        """
        bars = self._client.get_daily_bars(symbol, start, end, adjusted=self._adjusted)
        filtered = [b for b in bars if start <= b.ts <= end]
        filtered.sort(key=lambda b: b.ts)
        return filtered
