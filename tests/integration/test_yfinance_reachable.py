"""Integration layer (dev-playbook principle 1): needs the network / yfinance.

Marked ``integration`` so it is excluded from the fast pre-push gate and runs
only in CI's integration job. It guards the one thing unit tests cannot: that
the real data provider still returns the columns the future YFinanceAdapter
depends on. A failure here is a provider/contract change to investigate, not a
flake to paper over (principle 8).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_yfinance_returns_expected_ohlcv_columns() -> None:
    yf = pytest.importorskip("yfinance")

    data = yf.download("SPY", period="5d", interval="1d", progress=False, auto_adjust=True)
    assert not data.empty, "yfinance returned no rows for SPY"

    columns = {str(c).lower() for c in data.columns.get_level_values(0)}
    assert {"open", "high", "low", "close", "volume"} <= columns
