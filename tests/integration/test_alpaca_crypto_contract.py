"""Nightly provider-contract checks for Alpaca's **crypto** venue (ADR-0058).

Every premise ADR-0053 through ADR-0057 rests on was arithmetic and generated
data: EPIC-87's own closing note said so — *"every claim about a live crypto venue
in ADR-0053 through ADR-0057 is arithmetic and generated data, never observed"*.
This file is where those premises get watched against the real thing, so that the
day Alpaca changes one of them we hear about it instead of discovering it in a
live session.

Four of them are load-bearing enough to fail a nightly over:

1. **A crypto daily bar is stamped at UTC midnight** — ADR-0053's completeness
   rule and ADR-0056's generation anchor both assume it. If Alpaca started
   stamping at, say, 13:30, ``interval_is_complete`` would still be right (it
   needs no calendar) but the synthetic series and the real one would no longer
   line up on the same grid.
2. **There is no weekend gap** — the thing that makes it a continuous market at
   all (ADR-0053/0056).
3. **An absurdly early start returns *one* bar, not zero and not an error** — a
   third behaviour, quieter than the equity case ADR-0047 fixed, and the reason a
   bounded window is load-bearing here rather than merely tidy.
4. **``CryptoBarsRequest`` carries no ``adjustment`` and no ``feed``** — the two
   fields the equity path depends on (ADR-0021/0045 and ADR-0034). If either
   appeared, our "one price notion, nothing to adjust" claim would need revisiting
   the same day.

Layering, per ADR-0040/0046: marked ``integration`` **and** ``network``, so the
REQUIRED ``integration`` job never runs it and a provider outage can never block a
merge. **Nothing here places an order or touches the account balance** — order
behaviour lives in ``test_alpaca_crypto_live.py``, which is gated separately.

One genuinely new fact for this repo, and it is worth stating loudly: **crypto
market data needs no credentials at all**. The bar tests below therefore run on a
keyless client and are gated only on the SDK, which means CI checks them even
before ADR-0046's secrets are added.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from trading.calendar import CRYPTO_24_7
from trading.data.alpaca_adapter import AlpacaAdapter
from trading.data.alpaca_client import (
    ASSET_CLASS_CRYPTO,
    RealAlpacaClient,
    canonical_crypto_symbol,
)
from trading.frequency import Frequency
from trading.tape_density import screen_by_tape_density

# The venue's own stablecoin/pegged-asset pairs (matching universe.py's crypto10
# exclusion reasoning): a pegged asset has no meaningful tape-density question
# because it is not something a trend/relative-strength strategy would hold, so
# it is excluded from the tape-density candidate set the same way it is excluded
# from crypto10.
_STABLE_OR_PEGGED = frozenset({"USDC/USD", "USDT/USD", "USDG/USD", "PAXG/USD"})

_HAVE_CREDS = bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))
_HAVE_SDK = importlib.util.find_spec("alpaca") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.network,
    pytest.mark.skipif(not _HAVE_SDK, reason="needs the alpaca-py SDK (uv sync --extra alpaca)"),
]

_NEEDS_CREDS = pytest.mark.skipif(
    not _HAVE_CREDS, reason="needs ALPACA_API_KEY / ALPACA_SECRET_KEY (paper only); see ADR-0046"
)

# The pair every claim below is measured on. Listed since Alpaca's crypto history
# began (2021-01-01) and the most liquid thing on the venue.
SYMBOL = "BTC/USD"

# What the venue served on 2026-08-14. Not asserted as an equality — a data
# inception date could legitimately move if Alpaca backfills — but a *floor*, so a
# tape that suddenly starts in 2025 is caught.
KNOWN_INCEPTION = datetime(2021, 1, 1, tzinfo=UTC)


def _crypto_data_client() -> Any:
    """A keyless crypto bars client — measured to return identical bars to a keyed one."""
    from alpaca.data.historical import CryptoHistoricalDataClient

    return CryptoHistoricalDataClient()


def _bars(timeframe: Any, start: datetime, end: datetime) -> list[Any]:
    from alpaca.data.requests import CryptoBarsRequest

    client = _crypto_data_client()
    response = client.get_crypto_bars(
        CryptoBarsRequest(symbol_or_symbols=SYMBOL, timeframe=timeframe, start=start, end=end)
    )
    return list(response.data.get(SYMBOL, []))


class TestDailyBarShape:
    """ADR-0053/0056's two generation premises, converted from reasoned to observed."""

    def test_daily_bars_are_stamped_at_utc_midnight(self) -> None:
        """ADR-0056 adopted UTC midnight deliberately; the venue agrees.

        Not a cosmetic detail: the daily bar and its intraday grid must share an
        anchor, and ADR-0053's completeness rule closes a 24/7 daily bar exactly
        ``ts + 24h`` after this stamp. A provider stamping at 13:30 would put the
        real close 13.5 hours from where we believe it is.
        """
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(UTC)
        rows = _bars(TimeFrame.Day, now - timedelta(days=20), now)
        assert rows, "the venue returned no daily bars at all"
        offsets = {(r.timestamp.hour, r.timestamp.minute, r.timestamp.second) for r in rows}
        assert offsets == {(0, 0, 0)}, f"daily bars are not at UTC midnight: {sorted(offsets)}"

    def test_there_is_no_weekend_gap(self) -> None:
        """A market that never closes, observed rather than assumed (ADR-0053)."""
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(UTC)
        rows = _bars(TimeFrame.Day, now - timedelta(days=30), now)
        assert len(rows) >= 25, f"expected ~30 daily bars on a continuous market, got {len(rows)}"
        weekend = [r for r in rows if r.timestamp.weekday() >= 5]
        assert weekend, "a continuous market must produce weekend bars"
        gaps = [
            (a.timestamp, b.timestamp)
            for a, b in pairwise(rows)
            if b.timestamp - a.timestamp != timedelta(days=1)
        ]
        assert not gaps, f"unexpected gaps in a 24/7 daily series: {gaps}"

    def test_the_days_forming_bar_is_served(self) -> None:
        """Which is precisely what ADR-0053's ``ts + interval`` rule exists to withhold.

        The venue hands out the current day's partial bar. Under the equity
        session rule ("the UTC date has turned over") that bar reads complete the
        moment the date changes — i.e. immediately — so a 24/7 strategy would act
        on a bar with hours left to run. This asserts the venue's behaviour, not
        ours; ``interval_is_complete`` is what makes it safe.
        """
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(UTC)
        rows = _bars(TimeFrame.Day, now - timedelta(days=3), now)
        assert rows
        newest = rows[-1].timestamp
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        assert newest == today, (
            "expected the venue to serve today's still-forming daily bar; "
            f"newest={newest.isoformat()} today={today.isoformat()}"
        )
        assert newest + timedelta(days=1) > now, "today's bar cannot have elapsed yet"


class TestAbsurdStartIsAThirdBehaviour:
    """ADR-0047 measured 0 bars on equities. Crypto answers 1, which is worse."""

    def test_an_absurdly_early_start_returns_one_bar_not_zero_and_not_an_error(self) -> None:
        """The quiet failure a bounded window prevents (ADR-0047 extended by ADR-0058).

        Zero bars at least trips ADR-0035's per-symbol absence and ADR-0047's
        universe-wide ERROR. **One** bar trips neither: the poll looks successful,
        the symbol looks present, and a live session primes a single bar and
        reports itself healthy while the strategy starves.

        Asserted as an inequality against a real request rather than as the
        literal ``1``, so a venue that starts answering ``0`` (the equity
        behaviour) or a full history also turns this red — either way the premise
        moved and someone should read this ADR again.
        """
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(UTC)
        absurd = _bars(TimeFrame.Day, datetime.min.replace(tzinfo=UTC), now)
        bounded = _bars(TimeFrame.Day, now - timedelta(days=30), now)

        assert len(absurd) < len(bounded), (
            "an unbounded start no longer under-answers; ADR-0047's premise has moved "
            f"(absurd={len(absurd)} bounded={len(bounded)})"
        )
        assert absurd, (
            "an unbounded start now returns nothing, i.e. crypto has become the equity "
            "case; ADR-0058's 'quieter than zero' reasoning needs revisiting"
        )

    def test_a_merely_early_start_returns_the_whole_history(self) -> None:
        """1900 and 1990 both work — so it is not a plan limit, it is the request."""
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(UTC)
        rows = _bars(TimeFrame.Day, datetime(1900, 1, 1, tzinfo=UTC), now)
        assert len(rows) > 1000, f"expected the full history, got {len(rows)}"
        first = rows[0].timestamp
        assert first >= KNOWN_INCEPTION, (
            f"the tape now starts before {KNOWN_INCEPTION.date()} ({first.isoformat()}); "
            "crypto10's inception comments are stale"
        )


class TestRequestSurfaceHasNoEquityKnobs:
    """The two fields whose absence our design decisions rest on."""

    def test_crypto_bars_request_has_no_adjustment_field(self) -> None:
        """No ``adjustment`` means one price notion, so ADR-0021 and ADR-0008 coincide.

        If this ever gains an ``adjustment``, the "raw is the total-return series"
        claim stops being structural and ADR-0045's split guard becomes relevant
        to crypto — both worth knowing the day it happens.
        """
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest

        assert "adjustment" not in CryptoBarsRequest.model_fields
        assert "adjustment" in StockBarsRequest.model_fields, "the equity contrast is the point"

    def test_crypto_bars_request_has_no_feed_field(self) -> None:
        """No feed means ADR-0034's free-plan SIP restriction has no crypto analogue."""
        from alpaca.data.requests import CryptoBarsRequest

        assert "feed" not in CryptoBarsRequest.model_fields
        assert set(CryptoBarsRequest.model_fields) == {
            "currency",
            "end",
            "limit",
            "sort",
            "start",
            "symbol_or_symbols",
            "timeframe",
        }

    def test_the_stock_client_refuses_a_pair_symbol(self) -> None:
        """Why the asset class is a construction property and not a runtime guess."""
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        if not _HAVE_CREDS:
            pytest.skip("the stock data client needs credentials")
        client = StockHistoricalDataClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
        )
        now = datetime.now(UTC)
        with pytest.raises(Exception, match="invalid symbol"):
            client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=SYMBOL,
                    timeframe=TimeFrame.Day,
                    start=now - timedelta(days=5),
                    end=now,
                )
            )

    def test_the_data_api_refuses_the_concatenated_alias(self) -> None:
        """`BTCUSD` is a *trading*-API alias only; the data API has one canonical form.

        Which is why ``crypto10`` is written in slash form and why the ADR-0057
        shape guard's rule (a known quote currency after a separator) matches what
        this venue actually serves.
        """
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(UTC)
        client = _crypto_data_client()
        with pytest.raises(Exception, match="invalid symbol"):
            client.get_crypto_bars(
                CryptoBarsRequest(
                    symbol_or_symbols="BTCUSD",
                    timeframe=TimeFrame.Day,
                    start=now - timedelta(days=5),
                    end=now,
                )
            )


@_NEEDS_CREDS
class TestAssetMetadataContract:
    """ADR-0028's flags, and the symbol map ``list_positions`` depends on."""

    def test_the_venue_symbol_map_is_collision_free(self) -> None:
        """The map is only usable because concatenation is injective on this venue."""
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        symbol_map = client._crypto_symbol_map()
        assert len(symbol_map) >= 50, f"suspiciously few crypto assets: {len(symbol_map)}"
        assert len(set(symbol_map.values())) == len(symbol_map), "concatenation collided"
        assert all("/" in slash for slash in symbol_map.values())

    def test_a_suffix_rule_would_agree_with_the_venue_map_today(self) -> None:
        """Pinned as a *contract*, deliberately not shipped as a second mechanism.

        Production canonicalization reads the venue's own asset listing, so there
        is no rule of ours to keep in sync (ADR-0035's reuse rule). This asserts
        that the obvious shortcut would currently give the same answer — so the
        day it stops, we learn that from a nightly rather than from a position
        reconciled under the wrong key.
        """
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        symbol_map = client._crypto_symbol_map()
        quotes = sorted({s.split("/")[1] for s in symbol_map.values()}, key=len, reverse=True)

        disagreements = []
        for flat, slash in symbol_map.items():
            guess = next(
                (
                    f"{flat[: -len(q)]}/{q}"
                    for q in quotes
                    if flat.endswith(q) and len(flat) > len(q)
                ),
                None,
            )
            if guess != slash:
                disagreements.append((flat, slash, guess))
        assert not disagreements, (
            f"the suffix shortcut no longer matches the venue: {disagreements}"
        )

    def test_every_crypto10_symbol_is_tradable_and_fractionable(self) -> None:
        """ADR-0024/0028's snapshot, re-run. A basket is a claim until this passes."""
        from trading.universe import get_universe, validate_universe

        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        result = validate_universe(get_universe("crypto10"), client)
        assert result.is_clean, "\n".join(result.report_lines())

    def test_get_asset_normalizes_the_concatenated_alias(self) -> None:
        """The trading API accepts both spellings and answers in the canonical one."""
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        assert client.get_asset("BTCUSD").symbol == SYMBOL
        assert client.get_asset(SYMBOL).symbol == SYMBOL

    def test_min_order_size_is_published_and_varies_enormously(self) -> None:
        """Recorded metadata (ADR-0058). It is real, and it is not one number."""
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        sizes = [
            size
            for symbol in ("BTC/USD", "ETH/USD", "DOGE/USD")
            if (size := client.get_asset(symbol).min_order_size) is not None
        ]
        assert len(sizes) == 3, "the venue stopped publishing min_order_size"
        assert max(sizes) / min(sizes) > 1000, f"expected wide spread, got {sizes}"

    def test_us_equities_publish_no_min_order_size(self) -> None:
        """Which is why the field is ``None``-by-default rather than ``0.0``."""
        client = RealAlpacaClient()
        assert client.get_asset("AAPL").min_order_size is None


@_NEEDS_CREDS
class TestAdapterThroughTheSeam:
    """The whole path a backtest uses, end to end, on the real tape."""

    def test_the_crypto_adapter_returns_bars_and_never_asks_for_splits(self) -> None:
        """A crypto pair has no corporate actions, so the guard must not fire at all.

        Measured separately: the corporate-actions endpoint answers a crypto
        symbol with empty data rather than an error, so the guard would have
        *passed* — it is skipped to avoid paying a request and emitting a
        could-not-verify warning for a check that can never apply.
        """
        adapter = AlpacaAdapter(interval=timedelta(days=1), calendar=CRYPTO_24_7)
        now = datetime.now(UTC)
        bars = adapter.get_bars(SYMBOL, now - timedelta(days=30), now)
        assert len(bars) >= 25
        assert all(bar.symbol == SYMBOL for bar in bars)
        assert bars == sorted(bars, key=lambda b: b.ts)

    def test_adjusted_and_raw_return_the_same_bars(self) -> None:
        """One price notion, so ADR-0008 and ADR-0021 ask for the same thing here.

        Not the flag being ignored: a crypto pair has no splits and no dividends,
        so the raw series *is* the total-return series. Asserted so that if Alpaca
        ever adds an ``adjustment`` and the two diverge, this goes red rather than
        a backtest quietly marking a raw account on adjusted prices.
        """
        adapter = AlpacaAdapter(interval=timedelta(days=1), calendar=CRYPTO_24_7)
        now = datetime.now(UTC)
        window = (now - timedelta(days=10), now)
        assert adapter.get_bars(SYMBOL, *window, adjusted=True) == adapter.get_bars(
            SYMBOL, *window, adjusted=False
        )

    def test_intraday_crypto_bars_have_no_session_shape(self) -> None:
        """5-minute bars run through the night, unlike the equity tape."""
        adapter = AlpacaAdapter(interval=timedelta(minutes=5), calendar=CRYPTO_24_7)
        now = datetime.now(UTC)
        bars = adapter.get_bars(SYMBOL, now - timedelta(hours=6), now)
        assert len(bars) >= 50, f"expected ~72 five-minute bars in six hours, got {len(bars)}"
        hours = {bar.ts.hour for bar in bars}
        assert len(hours) >= 5, "six hours of bars should span at least five distinct hours"

    def test_a_feed_and_the_crypto_venue_cannot_be_combined(self) -> None:
        with pytest.raises(ValueError, match="no feed field at all"):
            RealAlpacaClient(feed="iex", asset_class=ASSET_CLASS_CRYPTO)

    def test_canonicalization_agrees_with_the_live_map(self) -> None:
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        symbol_map = client._crypto_symbol_map()
        assert canonical_crypto_symbol("BTCUSD", "crypto", symbol_map) == SYMBOL


@_NEEDS_CREDS
class TestTapeDensityLive:
    """KAN-863 / ADR-0073: the tape-density screen, driven against the real venue.

    Reproduced the ticket's own cited numbers exactly on 2026-08-15 bars (see the
    ADR) before this was written; these tests are the ongoing nightly watch, not
    a re-assertion of that one day's figures -- coverage moves day to day (BTC/USD
    measured 98.6% on 2026-08-15 and 100.0% eight days later), so nothing here
    pins a specific coverage number.
    """

    def test_list_assets_agrees_with_the_symbol_map(self) -> None:
        """`list_assets` (KAN-863's seam widening) and `_crypto_symbol_map` must agree.

        The latter now derives from the former (ADR-0035's reuse rule), so this is
        really a change-detector on that refactor rather than two independent
        mechanisms happening to agree.
        """
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        listed = {asset.symbol for asset in client.list_assets()}
        mapped = set(client._crypto_symbol_map().values())
        assert listed == mapped
        assert len(listed) >= 50, f"suspiciously few crypto assets: {len(listed)}"

    def test_the_non_stablecoin_usd_candidate_set_is_a_few_dozen(self) -> None:
        """Ballpark, not a pin: KAN-863 measured 32 on 2026-08-15; the venue's listing grows."""
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        assets = client.list_assets()
        candidates = [
            asset.symbol
            for asset in assets
            if asset.symbol.endswith("/USD") and asset.symbol not in _STABLE_OR_PEGGED
        ]
        assert 20 <= len(candidates) <= 60, candidates

    def test_screen_by_tape_density_runs_end_to_end_on_the_real_listing(self) -> None:
        """The screen actually discriminates: some real pairs clear the default floor, some don't.

        This is the finding the whole ticket is about, checked structurally
        (not a specific number) so it survives day-to-day coverage drift: a
        universe screened by tape density is not simply "everything" or
        "nothing".
        """
        client = RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)
        assets = client.list_assets()
        candidates = sorted(
            asset.symbol
            for asset in assets
            if asset.symbol.endswith("/USD") and asset.symbol not in _STABLE_OR_PEGGED
        )
        adapter = AlpacaAdapter(client=client, interval=timedelta(minutes=5), calendar=CRYPTO_24_7)
        freq = Frequency.parse("5m", calendar=CRYPTO_24_7)
        # Formation window ends "yesterday" relative to right now, so it always
        # has a full day of real, settled trading behind it.
        backtest_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        screen = screen_by_tape_density(adapter, candidates, backtest_start, freq)

        assert screen.verdicts
        assert all(v.coverage is None or 0.0 <= v.coverage <= 1.05 for v in screen.verdicts), (
            "a coverage ratio outside [0, ~1] means the expected-bar-count arithmetic broke"
        )
        assert screen.kept, "nothing cleared the default floor -- the whole venue looks dead"

        btc = next((v for v in screen.verdicts if v.symbol == SYMBOL), None)
        assert btc is not None and not btc.unverified, (
            "BTC/USD -- the venue's deepest market -- had no formation-window bars at all"
        )
