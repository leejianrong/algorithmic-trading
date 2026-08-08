"""The provider-contract test: does yfinance still answer with OHLCV? LIVE.

This is the one test here that is live **by definition** and cannot be faked — it
asks the real provider whether the response shape ``YFinanceAdapter`` depends on is
still what we assume. ``columns.get_level_values(0)`` below is a MultiIndex
accommodation, i.e. standing evidence that this shape has already changed once.

Marked ``integration`` **and** ``network`` (ADR-0040). The ``network`` marker keeps
it out of the fast gate *and* out of CI's required ``integration`` job, which must
never be able to fail on a third-party rate limit — it does not gate a merge; it
runs nightly (and on demand) in ``integration-network``. Everything else that used
to live in the integration job is offline now.

A failure here is a provider/contract change to investigate, not a flake to paper
over (dev-playbook principle 8) — *unless* the provider simply refused us, which the
assertions below name explicitly rather than leaving to a re-run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.data.yfinance_adapter import probe_refusal

pytestmark = [pytest.mark.integration, pytest.mark.network]


def test_yfinance_returns_expected_ohlcv_columns() -> None:
    yf = pytest.importorskip("yfinance")

    end = datetime.now(UTC)
    start = end - timedelta(days=10)
    data = yf.download("SPY", start=start, end=end, interval="1d", progress=False, auto_adjust=True)

    if data is None or data.empty:
        # ``yf.download`` swallows every per-ticker exception into an empty frame, so
        # "no rows" is ambiguous: a 429 and a genuine outage look identical here.
        # Probe, so the failure says which one it was instead of inviting a re-run
        # (this raises ProviderRefusedError when we were merely rate limited).
        probe_refusal("SPY", start, end)
        pytest.fail(
            "yfinance returned no rows for SPY over the last 10 days and did not "
            "report a refusal — investigate the provider, do not just re-run"
        )

    columns = {str(c).lower() for c in data.columns.get_level_values(0)}
    assert {"open", "high", "low", "close", "volume"} <= columns


def test_the_adapter_still_gets_adjusted_prices_from_the_live_provider() -> None:
    """The half a committed fixture cannot cover (ADR-0040).

    The offline split guard proves the *adapter* handles adjusted prices; only a live
    call proves yfinance still *serves* them. AAPL's 4-for-1 split on 2020-08-31 is
    the probe: adjusted, the pre-split close is ~$121 rather than the ~$484 the tape
    printed, so a provider that silently stopped adjusting shows up as a 4x price.
    """
    pytest.importorskip("yfinance")
    from trading.data.yfinance_adapter import _default_fetch

    frame = _default_fetch(
        "AAPL", datetime(2020, 8, 20, tzinfo=UTC), datetime(2020, 9, 5, tzinfo=UTC)
    )

    assert not frame.empty, "yfinance returned no rows for AAPL across its 2020 split"
    pre_split = frame.loc[frame.index < "2020-08-31", "close"]
    assert not pre_split.empty
    # ~121 adjusted vs ~484 raw. A generous band: this asks "adjusted or not", not
    # "what exactly was the close".
    assert 100.0 < float(pre_split.iloc[-1]) < 200.0
