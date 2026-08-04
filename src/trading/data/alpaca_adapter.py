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

from datetime import datetime, timedelta

from trading.data.alpaca_client import AlpacaClient
from trading.types import Bar


class AlpacaAdapter:
    """Serves adjusted bars from Alpaca through the client seam.

    The bar cadence is fixed at construction by ``interval`` (default one day, the
    original daily behaviour). A daily interval routes to
    :meth:`AlpacaClient.get_daily_bars` so the daily path is byte-identical to
    before; a sub-daily interval routes to the interval-aware
    :meth:`AlpacaClient.get_bars` (ADR-0022). Either way the returned bars satisfy
    the daily-shaped :class:`~trading.interfaces.DataAdapter` protocol — the
    interval is an adapter property, never a ``get_bars`` argument.
    """

    def __init__(
        self,
        client: AlpacaClient | None = None,
        *,
        adjusted: bool = True,
        interval: timedelta = timedelta(days=1),
    ) -> None:
        if client is None:
            from trading.data.alpaca_client import RealAlpacaClient

            client = RealAlpacaClient()
        self._client = client
        self._adjusted = adjusted
        self._interval = interval

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool | None = None,
    ) -> list[Bar]:
        """Return ``symbol``'s bars in ``[start, end]``, ascending by time.

        The per-call ``adjusted`` keyword controls the fetch (ADR-0021): pass
        ``True`` for split/dividend-adjusted total-return prices (the backtest
        feed) or ``False`` for RAW actual quotes (the paper/live feed); when it is
        omitted the constructor default applies. The construction-time ``interval``
        selects the cadence (ADR-0022): a daily interval uses ``get_daily_bars``
        (byte-identical to before); a sub-daily one uses the interval-aware
        ``get_bars``. Either seam already returns ascending, range-filtered bars;
        we re-filter and sort here defensively (both cheap).
        """
        effective = self._adjusted if adjusted is None else adjusted
        if self._interval >= timedelta(days=1):
            bars = self._client.get_daily_bars(symbol, start, end, adjusted=effective)
        else:
            bars = self._client.get_bars(
                symbol, start, end, adjusted=effective, interval=self._interval
            )
        filtered = [b for b in bars if start <= b.ts <= end]
        filtered.sort(key=lambda b: b.ts)
        return filtered
