"""Integration: real Alpaca intraday bars through the adapter (ADR-0022, step 6).

Marked ``integration`` and additionally SKIPPED unless both Alpaca credentials
(``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``) and the optional ``alpaca-py`` SDK
are present. It never runs in the fast gate, and it never runs in the offline
sandbox — it only proves the intraday path lights up against the real API when the
environment can reach it. The offline behaviour is covered by the fast layer
(``tests/unit/test_alpaca_adapter.py``) with ``FakeAlpacaClient``.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta

import pytest

_HAVE_CREDS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_HAVE_SDK = importlib.util.find_spec("alpaca") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_HAVE_CREDS and _HAVE_SDK),
        reason="needs ALPACA_API_KEY / ALPACA_SECRET_KEY and the alpaca-py SDK",
    ),
]


def test_real_alpaca_serves_hourly_bars() -> None:
    from trading.data.alpaca_adapter import AlpacaAdapter

    adapter = AlpacaAdapter(interval=timedelta(hours=1))
    end = datetime.now(UTC) - timedelta(days=2)
    start = end - timedelta(days=5)

    bars = adapter.get_bars("AAPL", start, end)

    assert bars, "expected at least one hourly bar in a 5-day window"
    assert all(start <= b.ts <= end for b in bars)
    assert bars == sorted(bars, key=lambda b: b.ts)
    # Intraday: more than one distinct time-of-day across the window.
    assert len({b.ts.time() for b in bars}) > 1
