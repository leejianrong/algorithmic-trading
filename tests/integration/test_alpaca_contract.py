"""Nightly provider-contract checks for Alpaca (ADR-0046, extending ADR-0040).

ADR-0040 built exactly this mechanism and pointed it at one provider. Its own
"still open" note said so: *"other adapters are not covered by a contract test at
all. `AlpacaAdapter` has creds-gated live tests (ADR-0018) that skip in CI, so
nothing nightly notices an Alpaca response-shape change either."*

KAN-694 proved that gap is not theoretical. Between 2026-08-04 and 2026-08-08
Alpaca stopped applying AAPL's 2020-08-31 4:1 split to its *adjusted* bars, and
**nothing in this repo noticed** — it surfaced only because an agent happened to
be executing live paths for an unrelated ticket. Every Alpaca test lived behind a
credentials gate that CI could never satisfy, so "skipped" and "passing" looked
identical from the outside.

Layering, per ADR-0040: marked ``integration`` **and** ``network``, so the
REQUIRED ``integration`` job (``-m "integration and not network"``) never runs it
and a provider outage can never block a merge. It runs in the nightly,
non-required ``integration-network`` job. **Never add that job to branch
protection** — it does not run on pull requests, so requiring it deadlocks every
merge.

These tests need credentials in Actions secrets, which is new for this repo (see
ADR-0046 for the exact names and setup). PAPER KEYS ONLY, and nothing here places
an order, cancels one, or touches the account balance — it is a pure read of the
market-data and asset-metadata surfaces.

Skipping is deliberately *visible*: the workflow annotates the run when the
secrets are absent, because a green job that ran nothing is exactly the failure
mode ADR-0040 warned about ("a skipped provider check is a green tick that means
nothing").
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

_HAVE_CREDS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_HAVE_SDK = importlib.util.find_spec("alpaca") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.network,
    pytest.mark.skipif(
        not (_HAVE_CREDS and _HAVE_SDK),
        reason=(
            "needs ALPACA_API_KEY / ALPACA_SECRET_KEY and the alpaca-py SDK "
            "(uv sync --extra alpaca). In CI these come from repository secrets; "
            "see ADR-0046."
        ),
    ),
]

# A split the provider handles CORRECTLY, used to state the general contract.
# NVDA's 10-for-1 on 2024-06-10 is recent, unambiguous, and (measured 2026-08-09)
# properly backed out of the adjusted series.
_WORKING_SYMBOL = "NVDA"
_WORKING_EX_DATE = datetime(2024, 6, 10, tzinfo=UTC)
_WORKING_RATIO = 10.0

# The split the provider currently gets WRONG (KAN-694 / ADR-0045).
_BROKEN_SYMBOL = "AAPL"
_BROKEN_EX_DATE = datetime(2020, 8, 31, tzinfo=UTC)
_BROKEN_RATIO = 4.0

_RATIO_TOLERANCE = 0.02


def _straddle_factor(symbol: str, ex_date: datetime, *, pad_days: int = 6) -> float:
    """Measure how much split adjustment the provider applied across ``ex_date``.

    ``factor = raw_close / adjusted_close`` is the cumulative adjustment the
    provider claims at a bar. The *ratio* of that factor either side of a split's
    ex-date equals the split ratio when the split was applied and 1.0 when it was
    not — and because raw and adjusted both carry the stock's own move that day,
    the move cancels exactly. This is the same arithmetic
    :mod:`trading.data.alpaca_adapter` runs (ADR-0045), restated here in the
    test's own terms so a bug in the adapter cannot make this test agree with it.
    """
    from trading.data.alpaca_client import RealAlpacaClient

    client = RealAlpacaClient()
    start = ex_date - timedelta(days=pad_days)
    end = ex_date + timedelta(days=pad_days)
    adjusted = {b.ts: b for b in client.get_daily_bars(symbol, start, end, adjusted=True)}
    raw = {b.ts: b for b in client.get_daily_bars(symbol, start, end, adjusted=False)}
    assert adjusted and raw, f"no bars for {symbol} around {ex_date.date()}"

    shared = sorted(set(adjusted) & set(raw))
    before = [ts for ts in shared if ts.date() < ex_date.date()]
    on_or_after = [ts for ts in shared if ts.date() >= ex_date.date()]
    assert before and on_or_after, (
        f"window around {ex_date.date()} does not straddle the ex-date for {symbol}; "
        "the check would be vacuous"
    )
    pre, post = before[-1], on_or_after[0]
    return (raw[pre].close / adjusted[pre].close) / (raw[post].close / adjusted[post].close)


class TestAdjustedReallyMeansAdjusted:
    """The contract KAN-694 broke: ``adjustment=all`` must back splits out."""

    def test_a_known_split_is_applied_to_the_adjusted_series(self) -> None:
        # The general statement. If this goes red, Alpaca's adjustment pipeline is
        # broken *broadly* rather than for one symbol, and ADR-0045's per-symbol
        # guard is no longer the right shape — read the ADR before touching it.
        measured = _straddle_factor(_WORKING_SYMBOL, _WORKING_EX_DATE)
        assert abs(measured / _WORKING_RATIO - 1) < _RATIO_TOLERANCE, (
            f"{_WORKING_SYMBOL}'s {_WORKING_RATIO:g}:1 split of "
            f"{_WORKING_EX_DATE.date()} is no longer backed out of the adjusted "
            f"series (measured adjustment factor {measured:.4f}, expected "
            f"{_WORKING_RATIO:g}). That is ADR-0008's phantom-split hazard: a "
            "backtest over this window now sees a ~-90% day that never happened."
        )

    def test_the_adjusted_series_carries_no_phantom_cliff(self) -> None:
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient()
        bars = client.get_daily_bars(
            _WORKING_SYMBOL,
            _WORKING_EX_DATE - timedelta(days=6),
            _WORKING_EX_DATE + timedelta(days=6),
            adjusted=True,
        )
        worst = min(b.close / a.close - 1 for a, b in pairwise(bars))
        assert worst > -0.35, (
            f"the adjusted {_WORKING_SYMBOL} series contains a {worst:.1%} single-day "
            "move across a split date — the split was not applied (ADR-0008)"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "KNOWN PROVIDER DEFECT (KAN-694 / ADR-0045): Alpaca does not apply "
            "AAPL's 2020-08-31 4:1 split to its adjusted bars, though its own "
            "corporate-actions endpoint reports the split. Reproduced live "
            "2026-08-09. This is strict on purpose: when Alpaca fixes it this "
            "test XPASSes and the nightly goes RED, which is the signal to delete "
            "this xfail and re-evaluate whether ADR-0045's guard is still needed. "
            "A silently-passing test would let the fix go unnoticed."
        ),
    )
    def test_aapl_split_adjustment_is_still_broken(self) -> None:
        measured = _straddle_factor(_BROKEN_SYMBOL, _BROKEN_EX_DATE)
        assert abs(measured / _BROKEN_RATIO - 1) < _RATIO_TOLERANCE, (
            f"{_BROKEN_SYMBOL} adjustment factor across {_BROKEN_EX_DATE.date()} "
            f"measured {measured:.4f}, expected {_BROKEN_RATIO:g}"
        )

    def test_the_guard_refuses_the_broken_window_end_to_end(self) -> None:
        """ADR-0045's guard, through the real adapter against the real provider.

        Not an xfail: this asserts *our* behaviour, which is correct either way.
        While the provider is broken the guard must fire; the day it is fixed the
        guard must fall silent — and both are stated here.
        """
        from trading.data.alpaca_adapter import AlpacaAdapter, UnadjustedSplitError

        adapter = AlpacaAdapter()
        start = _BROKEN_EX_DATE - timedelta(days=6)
        end = _BROKEN_EX_DATE + timedelta(days=6)
        provider_is_broken = abs(_straddle_factor(_BROKEN_SYMBOL, _BROKEN_EX_DATE) - 1.0) < (
            _RATIO_TOLERANCE
        )
        if provider_is_broken:
            with pytest.raises(UnadjustedSplitError) as excinfo:
                adapter.get_bars(_BROKEN_SYMBOL, start, end, adjusted=True)
            assert str(_BROKEN_EX_DATE.date()) in str(excinfo.value)
        else:
            assert adapter.get_bars(_BROKEN_SYMBOL, start, end, adjusted=True)


class TestTheGuardsOwnDependency:
    """ADR-0045's guard rests on the corporate-actions endpoint — watch it too.

    If this endpoint stops answering, the guard degrades to a warning and lets
    unverified bars through by design ("we could not ask" is not "the data is
    bad", ADR-0028). That degradation is silent in a batch run, so the nightly is
    the place it must be loud.
    """

    def test_the_corporate_actions_endpoint_still_reports_the_aapl_split(self) -> None:
        from trading.data.alpaca_client import RealAlpacaClient

        splits = RealAlpacaClient().get_splits(
            _BROKEN_SYMBOL,
            _BROKEN_EX_DATE - timedelta(days=30),
            _BROKEN_EX_DATE + timedelta(days=30),
        )
        matching = [s for s in splits if s.ex_date == _BROKEN_EX_DATE.date()]
        assert matching, (
            f"Alpaca's corporate-actions endpoint no longer reports "
            f"{_BROKEN_SYMBOL}'s {_BROKEN_EX_DATE.date()} split. ADR-0045's guard "
            "is now blind and will pass unverified adjusted bars with only a "
            "warning."
        )
        assert abs(matching[0].ratio - _BROKEN_RATIO) < 1e-9

    def test_a_window_with_no_corporate_action_comes_back_empty(self) -> None:
        # The other half: "no splits" must be an empty list, not an error and not
        # a stale echo of another window.
        from trading.data.alpaca_client import RealAlpacaClient

        splits = RealAlpacaClient().get_splits(
            _BROKEN_SYMBOL, datetime(2023, 1, 1, tzinfo=UTC), datetime(2023, 3, 1, tzinfo=UTC)
        )
        assert splits == []


class TestBarShapeIsUnchanged:
    """The Alpaca half of ADR-0040's yfinance column-shape check.

    ``_rows_to_bars`` reads ``timestamp``/``open``/``high``/``low``/``close``/
    ``volume`` off the SDK's row objects. A renamed or dropped field would break
    every Alpaca-sourced run, and nothing else in the suite would see it.
    """

    def test_daily_bars_parse_into_our_bar_type(self) -> None:
        from trading.data.alpaca_client import RealAlpacaClient
        from trading.types import Bar

        client = RealAlpacaClient()
        bars = client.get_daily_bars(
            _WORKING_SYMBOL,
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 2, 1, tzinfo=UTC),
            adjusted=True,
        )
        assert bars, "expected daily bars for a liquid mega-cap over a normal month"
        assert all(isinstance(b, Bar) for b in bars)
        assert bars == sorted(bars, key=lambda b: b.ts)
        for bar in bars:
            assert bar.symbol == _WORKING_SYMBOL
            assert bar.ts.tzinfo is not None, "a naive timestamp would desync every adapter"
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high
            assert bar.volume > 0, "volume is read by the ADV screen (ADR-0029)"

    def test_intraday_bars_still_carry_start_timestamps(self) -> None:
        # ADR-0022: sub-daily bars are START-stamped, which is what
        # `interval_is_complete` gates on. A switch to END stamps would make the
        # paper loop act on bars a full interval early.
        from trading.data.alpaca_client import RealAlpacaClient

        client = RealAlpacaClient(feed="iex")
        end = datetime.now(UTC)
        bars = client.get_bars(
            _WORKING_SYMBOL,
            end - timedelta(days=5),
            end,
            adjusted=False,
            interval=timedelta(minutes=5),
        )
        assert bars, "expected recent 5m IEX bars within a 5-day window"
        assert bars == sorted(bars, key=lambda b: b.ts)
        assert all(b.ts.minute % 5 == 0 and b.ts.second == 0 for b in bars), (
            "5m bars are no longer stamped on the interval boundary (ADR-0022)"
        )


class TestAssetMetadataIsUnchanged:
    """ADR-0028's authority: the broker still says what it will trade."""

    def test_get_asset_reports_tradable_and_fractionable(self) -> None:
        from trading.data.alpaca_client import RealAlpacaClient

        asset = RealAlpacaClient().get_asset(_WORKING_SYMBOL)
        assert asset.symbol == _WORKING_SYMBOL
        assert asset.tradable is True
        assert asset.fractionable is True, (
            "fractional sizing (ADR-0011) assumes this; a False here silently "
            "breaks every target-weight order in the universe"
        )
        assert asset.exchange and "." not in asset.exchange, (
            "the AssetExchange enum prefix is no longer being stripped"
        )

    def test_an_unknown_ticker_is_a_lookup_error_not_a_crash(self) -> None:
        from trading.data.alpaca_client import RealAlpacaClient

        with pytest.raises(LookupError):
            RealAlpacaClient().get_asset("ZZZZNOTREAL")
