"""Integration: a real buy-and-hold backtest across a known stock split — OFFLINE.

Guards ADR-0008: on adjusted prices, AAPL's 4-for-1 split effective 2020-08-31 must
NOT appear as a ~75% one-day crash in the equity curve.

**Real data, no network** (ADR-0040). The bars come from a committed
``YFinanceAdapter`` cache CSV (``tests/fixtures/yfinance_cache/``), fetched once from
yfinance and checked in. The window is five years in the past and immutable — the
split happened, those adjusted bars will never change — so fetching them on every CI
run bought nothing and cost merge availability: this test lives in the **required**
``integration`` job, and on 2026-08-08 an upstream ``YFRateLimitError`` in that job
blocked a merge that had nothing to do with the data. Whether *yfinance today* still
returns adjusted OHLCV is a separate question, answered by the nightly, non-required
provider-contract test (``test_yfinance_reachable.py``).

The fetcher seam is injected with a stub that **raises if called**, so a missing or
misnamed fixture fails loudly instead of quietly reaching for the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.yfinance_adapter import YFinanceAdapter
from trading.engine import (
    REASON_FETCH_FAILED,
    BacktestResult,
    EmptyUniverseError,
    Engine,
    load_series,
)
from trading.interfaces import DataAdapter
from trading.risk import Guardrails
from trading.strategies.buy_and_hold import BuyAndHold
from trading.types import Bar, Portfolio

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "yfinance_cache"

_START = datetime(2020, 6, 1, tzinfo=UTC)
_END = datetime(2020, 12, 1, tzinfo=UTC)
# AAPL's 4-for-1 split was effective at the open on 2020-08-31.
_SPLIT_TS = datetime(2020, 8, 31, tzinfo=UTC)
_SPLIT_RATIO = 4
_PHANTOM_CRASH_FLOOR = -0.35


class _ForbiddenFetcher:
    """A fetcher that must never be reached: this test is offline by construction."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, symbol: str, start: datetime, end: datetime) -> NoReturn:
        self.calls.append(symbol)
        raise AssertionError(
            f"the network was reached for {symbol} {start:%Y-%m-%d}..{end:%Y-%m-%d}; "
            f"expected the committed fixture in {FIXTURE_DIR}"
        )


def _daily_returns(result: BacktestResult) -> list[float]:
    equities = [p.equity for p in result.equity_curve]
    return [
        equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0
    ]


def _run(adapter: DataAdapter) -> BacktestResult:
    """Buy-and-hold AAPL over the split window, fully invested.

    Guardrails are opted out (``RiskConfig.unlimited()``) because the default 25%
    position cap *dilutes the artifact under test*: measured on this very fixture, an
    unadjusted series clamped to a quarter of equity shows a worst day of only
    -25.3%, which slips under the -35% floor — so with caps on, this test passed on
    raw prices too and was not a guard at all (ADR-0040). Fully invested, adjusted
    bars bottom out at -8.0% and unadjusted at -73.9%: a real gap, which
    :func:`test_the_phantom_crash_floor_actually_catches_an_unadjusted_series`
    pins from the other side.
    """
    broker = SimulatedBroker(Portfolio(cash=1_000.0))
    engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
    return engine.run(BuyAndHold(), ["AAPL"], _START, _END)


def test_buy_and_hold_across_apple_split_has_no_phantom_crash() -> None:
    fetcher = _ForbiddenFetcher()
    result = _run(YFinanceAdapter(FIXTURE_DIR, fetcher))

    # Proof this test needs no network: the injected fetcher was never called.
    assert fetcher.calls == []

    # And proof it read real bars rather than degrading to an empty universe: the
    # reason codes are the only thing that separates "the fixture is fine" from
    # "something went wrong fetching" (ADR-0032) — a distinction a rate limit used
    # to blur, which is how a provider outage could read as a data regression.
    assert result.absent == []
    assert result.traded_symbols == ["AAPL"]

    assert len(result.equity_curve) > 100  # roughly six months of trading days
    assert result.final_equity > 0

    # A raw (unadjusted) series would show ~ -0.74 on the split day; adjusted must not.
    assert min(_daily_returns(result)) > _PHANTOM_CRASH_FLOOR, (
        "phantom split crash — prices are not adjusted"
    )

    # Buy-and-hold ends holding a single position and essentially no idle cash.
    assert set(result.final_portfolio.positions) == {"AAPL"}


def test_the_fixture_still_spans_the_split_date() -> None:
    """A fixture trimmed past 2020-08-31 would make the guard above vacuous."""
    bars = YFinanceAdapter(FIXTURE_DIR, _ForbiddenFetcher()).get_bars("AAPL", _START, _END)

    timestamps = [b.ts for b in bars]
    assert _SPLIT_TS in timestamps
    assert timestamps[0] < _SPLIT_TS < timestamps[-1]
    # Adjusted, so the pre-split close is in post-split dollars (~$121, not ~$484).
    pre_split = [b for b in bars if b.ts < _SPLIT_TS][-1]
    assert 100.0 < pre_split.close < 200.0


def test_the_phantom_crash_floor_actually_catches_an_unadjusted_series() -> None:
    """Watch the guard fail: de-adjust the same fixture and it must trip.

    This is what a committed fixture cannot get from the provider — the *negative*
    case. The split is arithmetic we know exactly (4-for-1 on 2020-08-31), so
    multiplying the pre-split bars back up reconstructs what an unadjusted tape
    printed, and the run over it is the failure mode ADR-0008 exists to prevent.
    Without this, the assertion above is only ever exercised in the green direction.
    """
    adjusted = YFinanceAdapter(FIXTURE_DIR, _ForbiddenFetcher()).get_bars("AAPL", _START, _END)
    unadjusted = [
        b
        if b.ts >= _SPLIT_TS
        else Bar(
            symbol=b.symbol,
            ts=b.ts,
            open=b.open * _SPLIT_RATIO,
            high=b.high * _SPLIT_RATIO,
            low=b.low * _SPLIT_RATIO,
            close=b.close * _SPLIT_RATIO,
            volume=b.volume // _SPLIT_RATIO,
        )
        for b in adjusted
    ]

    worst = min(_daily_returns(_run(FakeAdapter(unadjusted))))

    assert worst < _PHANTOM_CRASH_FLOOR, (
        f"the phantom-crash floor {_PHANTOM_CRASH_FLOOR} does not discriminate: an "
        f"unadjusted series only lost {worst:.1%} in a day"
    )
    assert worst < -0.6  # the split itself, ~ -74%


def test_a_missing_fixture_is_reported_as_a_fetch_failure_not_missing_history(
    tmp_path: Path,
) -> None:
    """The other half of the reason-code split, from this test's own failure mode.

    If the fixture disappears, this test must say "the lookup failed", never "AAPL
    has no history in 2020" — the sentence a rate limit used to produce, which is
    exactly how a real data break gets dismissed as a flake and re-run (ADR-0040).
    """
    adapter = YFinanceAdapter(tmp_path, _ForbiddenFetcher())

    _, absent = load_series(adapter, ["AAPL"], _START, _END)
    assert [a.reason for a in absent] == [REASON_FETCH_FAILED]
    assert "not listed in this window" not in absent[0].detail

    with pytest.raises(EmptyUniverseError) as excinfo:
        _run(adapter)
    assert "data lookup failed" in str(excinfo.value)
