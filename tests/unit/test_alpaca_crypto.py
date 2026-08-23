"""Fast, offline guards on the Alpaca **crypto** venue seam (ADR-0058).

No SDK, no key, no network. Everything that actually crosses the wire lives in
``tests/integration/test_alpaca_crypto_contract.py``, double-gated on credentials
and the SDK so a required CI check never leaves the machine (ADR-0040).

What can be pinned here is the part that decided *what* to send: the asset-class
vocabulary, the order duration each venue accepts, the position-symbol
canonicalization, and the adapter/broker wiring that picks a venue from the
market's calendar. Each of these is a defect that was **measured** against the
live paper account on 2026-08-14, not a shape someone imagined:

* ``TimeInForce.DAY`` on a crypto order is refused ``422``/``42210000``.
* A ``BTC/USD`` fill creates a position the venue calls ``BTCUSD``.
* ``CryptoBarsRequest`` has no ``adjustment`` and no ``feed`` field.

ADR-0040's lesson, fifth sighting, is loud in this file: ``SyntheticAdapter``
clips an absurd start to its epoch and ``FakeAdapter`` filters any range, so
**neither can test bounded-window behaviour against this venue** — Alpaca answers
``datetime.min`` on crypto with *one* bar, which is quieter than the equity zero
ADR-0047 fixed. ``TestStandInsCannotTestTheVenue`` pins that both stand-ins are
more forgiving than the provider, so the file cannot be quietly rewritten onto
them.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading.brokers.alpaca import AlpacaBroker
from trading.calendar import CRYPTO_24_7, US_EQUITY, MarketCalendar
from trading.data.alpaca_adapter import AlpacaAdapter
from trading.data.alpaca_client import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_US_EQUITY,
    ASSET_CLASSES,
    AlpacaClient,
    AlpacaOrder,
    AssetInfo,
    FakeAlpacaClient,
    RealAlpacaClient,
    SplitEvent,
    canonical_crypto_symbol,
    is_crypto_asset_class,
    require_asset_class,
    time_in_force_for,
)
from trading.data.fake import FakeAdapter
from trading.data.synthetic import SyntheticAdapter
from trading.frequency import Frequency
from trading.types import Bar, Order, Side

# The venue's own map, as read off the paper account on 2026-08-14. Four quote
# currencies across 73 pairs; these are the ones the tests below exercise.
VENUE_SYMBOL_MAP = {
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "BTCUSDT": "BTC/USDT",
    "DOTUSDC": "DOT/USDC",
    "BCHBTC": "BCH/BTC",
    "USDCUSD": "USDC/USD",
    "USDTUSD": "USDT/USD",
}


def _bar(symbol: str, day: int, close: float = 100.0) -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime(2026, 8, day, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
    )


class TestAssetClassVocabulary:
    """One spelling per venue, and an unknown one raises rather than defaulting."""

    def test_the_two_known_asset_classes(self) -> None:
        assert {ASSET_CLASS_US_EQUITY, ASSET_CLASS_CRYPTO} == ASSET_CLASSES

    @pytest.mark.parametrize("raw", ["crypto", "CRYPTO", "  Crypto  "])
    def test_normalizes_case_and_whitespace(self, raw: str) -> None:
        assert require_asset_class(raw) == ASSET_CLASS_CRYPTO

    def test_unknown_asset_class_raises_and_names_the_known_ones(self) -> None:
        """`get_calendar`'s rule (ADR-0054), one layer down.

        A venue that silently became the equity one would send every crypto order
        to the stock tape and collect `invalid symbol` on every bar.
        """
        with pytest.raises(ValueError, match="unknown asset class 'futures'"):
            require_asset_class("futures")
        with pytest.raises(ValueError, match="crypto, us_equity"):
            require_asset_class("futures")


class TestTimeInForce:
    """The measured reason a crypto order needs GTC (ADR-0058)."""

    def test_equity_stays_day(self) -> None:
        """The equity duration must not move: it is what every live order used."""
        assert time_in_force_for(ASSET_CLASS_US_EQUITY) == "day"

    def test_crypto_is_gtc_not_day(self) -> None:
        """Measured: DAY is refused 422/42210000 'invalid crypto time_in_force'."""
        assert time_in_force_for(ASSET_CLASS_CRYPTO) == "gtc"

    def test_the_two_venues_disagree(self) -> None:
        """Stated as an inequality so a 'simplification' to one constant goes red."""
        assert time_in_force_for(ASSET_CLASS_CRYPTO) != time_in_force_for(ASSET_CLASS_US_EQUITY)

    def test_unknown_asset_class_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown asset class"):
            time_in_force_for("futures")


class TestCanonicalCryptoSymbol:
    """Alpaca disagrees with itself: `BTC/USD` on the order, `BTCUSD` on the position."""

    def test_restores_the_slash_form(self) -> None:
        assert canonical_crypto_symbol("BTCUSD", "crypto", VENUE_SYMBOL_MAP) == "BTC/USD"

    def test_an_already_canonical_symbol_passes_through(self) -> None:
        assert canonical_crypto_symbol("BTC/USD", "crypto", VENUE_SYMBOL_MAP) == "BTC/USD"

    @pytest.mark.parametrize(
        ("flat", "expected"),
        [
            ("BTCUSDT", "BTC/USDT"),
            ("USDCUSD", "USDC/USD"),
            ("USDTUSD", "USDT/USD"),
            ("BCHBTC", "BCH/BTC"),
            ("DOTUSDC", "DOT/USDC"),
        ],
    )
    def test_ambiguous_looking_pairs_resolve_from_the_venue_map(
        self, flat: str, expected: str
    ) -> None:
        """`USDCUSD` and `BTCUSDT` are exactly why this is a map, not a suffix rule.

        Both split two ways by eye. The venue's own asset listing has one answer,
        so there is no rule of ours to get wrong or to keep in sync.
        """
        assert canonical_crypto_symbol(flat, "crypto", VENUE_SYMBOL_MAP) == expected

    def test_a_non_crypto_position_is_left_alone(self) -> None:
        """A stock sitting on the same account is not ours to rewrite."""
        assert canonical_crypto_symbol("AAPL", "us_equity", VENUE_SYMBOL_MAP) == "AAPL"

    def test_an_unmappable_crypto_position_raises_rather_than_reconciling(self) -> None:
        """The silent alternative is the whole defect.

        A position keyed `SOLUSD` while the bars say `SOL/USD` is invisible to
        sizing and the guardrails: gross exposure reads zero, the target-weight
        sizer sees a permanently unmet target, and the run buys the same coin every
        bar. Stopping beats narrating (ADR-0028).
        """
        with pytest.raises(ValueError, match="not in its own crypto asset listing"):
            canonical_crypto_symbol("SOLUSD", "crypto", VENUE_SYMBOL_MAP)

    @pytest.mark.parametrize("raw", ["crypto", "CRYPTO", "AssetClass.CRYPTO"])
    def test_asset_class_is_recognised_however_the_sdk_renders_it(self, raw: str) -> None:
        assert is_crypto_asset_class(raw) is True

    @pytest.mark.parametrize("raw", ["us_equity", "AssetClass.US_EQUITY", ""])
    def test_non_crypto_asset_class_strings(self, raw: str) -> None:
        assert is_crypto_asset_class(raw) is False


class TestRealClientConstructionGuards:
    """What the client refuses before it ever reaches the network."""

    def test_feed_and_crypto_cannot_be_combined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`CryptoBarsRequest` has no `feed` field, so a feed here is a lie.

        Refused rather than ignored: an operator who passed `--data-feed iex`
        would otherwise believe they had chosen a tape.
        """
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        with pytest.raises(ValueError, match="no feed field at all"):
            RealAlpacaClient(feed="iex", asset_class=ASSET_CLASS_CRYPTO)

    def test_unknown_asset_class_is_refused_before_the_sdk_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering matters: this must fail even with no SDK installed."""
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        with pytest.raises(ValueError, match="unknown asset class"):
            RealAlpacaClient(asset_class="futures")

    def test_missing_credentials_still_win_over_the_asset_class_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crypto *market data* needs no key, but this client also trades."""
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        with pytest.raises(ValueError, match="Alpaca credentials required"):
            RealAlpacaClient(asset_class=ASSET_CLASS_CRYPTO)

    def test_the_equity_default_is_unchanged(self) -> None:
        """No existing caller passes an asset class, so the default is the contract."""
        default = inspect.signature(RealAlpacaClient.__init__).parameters["asset_class"].default
        assert default == ASSET_CLASS_US_EQUITY


def _uninitialized_client(asset_class: str, symbol_map: dict[str, str] | None = None) -> object:
    """A `RealAlpacaClient` with its state set by hand and no SDK, key, or network.

    `__init__` builds two SDK clients, so the venue-specific *decisions* it makes
    afterwards were reachable only from a live test — and a live test cannot run in
    the fast gate. Constructing the object without running `__init__` and seeding
    exactly the attributes under test keeps those decisions honestly covered here,
    rather than covered nowhere.
    """
    client = object.__new__(RealAlpacaClient)
    client._asset_class = asset_class
    client._crypto_symbols = symbol_map
    return client


class TestPositionSymbolWiring:
    """The *call site*, not just the pure helper it delegates to.

    `canonical_crypto_symbol` is tested above, but nothing checked that
    `list_positions` actually calls it — reverting the call left every test green,
    which is the coverage hole this class closes.
    """

    def test_a_crypto_client_canonicalizes(self) -> None:
        client = _uninitialized_client(ASSET_CLASS_CRYPTO, dict(VENUE_SYMBOL_MAP))
        position = SimpleNamespace(symbol="BTCUSD", asset_class="AssetClass.CRYPTO")
        assert client._position_symbol(position) == "BTC/USD"  # type: ignore[attr-defined]

    def test_an_equity_client_leaves_every_symbol_alone(self) -> None:
        """The equity path must not pay for, or be changed by, the crypto fix."""
        client = _uninitialized_client(ASSET_CLASS_US_EQUITY, None)
        position = SimpleNamespace(symbol="BTCUSD", asset_class="AssetClass.CRYPTO")
        assert client._position_symbol(position) == "BTCUSD"  # type: ignore[attr-defined]

    def test_a_stock_position_on_a_crypto_account_is_returned_unchanged(self) -> None:
        """Not ours to rewrite — and it must not raise the unmappable-crypto error."""
        client = _uninitialized_client(ASSET_CLASS_CRYPTO, dict(VENUE_SYMBOL_MAP))
        position = SimpleNamespace(symbol="AAPL", asset_class="AssetClass.US_EQUITY")
        assert client._position_symbol(position) == "AAPL"  # type: ignore[attr-defined]

    def test_an_unmappable_crypto_position_raises_through_the_call_site(self) -> None:
        """Reaching the raise from `list_positions` matters, not just from the helper."""
        client = _uninitialized_client(ASSET_CLASS_CRYPTO, dict(VENUE_SYMBOL_MAP))
        position = SimpleNamespace(symbol="SOLUSD", asset_class="AssetClass.CRYPTO")
        with pytest.raises(ValueError, match="not in its own crypto asset listing"):
            client._position_symbol(position)  # type: ignore[attr-defined]


class TestCancelIsIdempotentWiring:
    """`cancel_order` absorbing an already-settled order, without a venue."""

    class _Trading:
        def __init__(self, error: Exception) -> None:
            self.error = error
            self.cancels = 0

        def cancel_order_by_id(self, order_id: str) -> None:
            self.cancels += 1
            raise self.error

    @staticmethod
    def _client(error: Exception, status: str) -> object:
        client = _uninitialized_client(ASSET_CLASS_CRYPTO)
        client._trading = TestCancelIsIdempotentWiring._Trading(error)  # type: ignore[attr-defined]
        client.get_order = lambda order_id: AlpacaOrder(  # type: ignore[attr-defined]
            id=order_id, symbol="BTC/USD", qty=1.0, side=Side.BUY, status=status
        )
        return client

    def test_a_cancel_of_a_filled_order_is_absorbed(self) -> None:
        """Measured live: `422`/`42210000` *order is already in "filled" state*."""
        error = type("APIErrorLike", (Exception,), {"status_code": 422})("already in filled state")
        client = self._client(error, "filled")
        client.cancel_order("abc")  # type: ignore[attr-defined]

    def test_a_cancel_of_a_still_working_order_propagates_the_failure(self) -> None:
        """Absorption is keyed on the order's *state*, never on the message.

        Alpaca answers both this and "invalid crypto time_in_force" with the same
        `42210000`, so the error taxonomy ADR-0041 relies on cannot separate them;
        re-reading the order can.
        """
        error = type("APIErrorLike", (Exception,), {"status_code": 422})("not cancelable")
        client = self._client(error, "new")
        with pytest.raises(Exception, match="not cancelable"):
            client.cancel_order("abc")  # type: ignore[attr-defined]

    def test_an_unknown_order_still_raises_lookup_error(self) -> None:
        """ "We never heard of it" must survive the absorption (ADR-0028)."""
        error = type("APIErrorLike", (Exception,), {"status_code": 404})("no such order")
        client = self._client(error, "filled")
        with pytest.raises(LookupError):
            client.cancel_order("abc")  # type: ignore[attr-defined]

    def test_a_failed_re_read_leaves_the_original_failure_standing(self) -> None:
        """If we cannot tell, the honest answer is that the cancel failed."""
        error = type("APIErrorLike", (Exception,), {"status_code": 422})("already in filled state")
        client = _uninitialized_client(ASSET_CLASS_CRYPTO)
        client._trading = self._Trading(error)  # type: ignore[attr-defined]

        def _boom(order_id: str) -> AlpacaOrder:
            raise RuntimeError("the re-read failed too")

        client.get_order = _boom  # type: ignore[attr-defined]
        with pytest.raises(Exception, match="already in filled state"):
            client.cancel_order("abc")  # type: ignore[attr-defined]


class TestFractionalVolumeIsTruncated:
    """A recorded lossy conversion, pinned so it cannot be forgotten (ADR-0058)."""

    def test_a_fractional_crypto_volume_truncates_toward_zero(self) -> None:
        """`Bar.volume` is an int; crypto volume is a coin count and is fractional.

        The measured BTC/USD daily volumes on 2026-08-13/14 were 0.797691877 and
        0.147082239 — the second lands here as a **zero-volume bar**, which is a
        lie about a day that did trade. Recorded, not fixed: widening
        `Bar.volume` touches `types.py` and every adapter.
        """
        rows = [
            SimpleNamespace(
                timestamp=datetime(2026, 8, 13, tzinfo=UTC),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.205143732,
            ),
            SimpleNamespace(
                timestamp=datetime(2026, 8, 14, tzinfo=UTC),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=0.147082239,
            ),
        ]
        bars = RealAlpacaClient._rows_to_bars("BTC/USD", rows)
        assert [b.volume for b in bars] == [1, 0]

    def test_an_integer_equity_volume_is_unaffected(self) -> None:
        rows = [
            SimpleNamespace(
                timestamp=datetime(2026, 8, 13, tzinfo=UTC),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=52_345_678,
            )
        ]
        assert RealAlpacaClient._rows_to_bars("AAPL", rows)[0].volume == 52_345_678


class _SplitSpy(FakeAlpacaClient):
    """A fake that screams if the corporate-actions endpoint is touched."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.split_calls = 0

    def get_splits(self, symbol: str, start: datetime, end: datetime) -> list[SplitEvent]:
        self.split_calls += 1
        raise AssertionError(f"get_splits must not be called for {symbol!r} on a crypto venue")


class TestAdapterSelectsTheVenueFromTheCalendar:
    """ADR-0056's reasoning reused: the calendar picks the venue, not a new flag."""

    def test_no_asset_class_argument_on_get_bars(self) -> None:
        """The venue is a construction property, never a per-call argument.

        Same shape ADR-0022 pinned for the interval and ADR-0056 for the day
        shape: a per-call argument would make a mixed-venue run representable.
        """
        params = set(inspect.signature(AlpacaAdapter.get_bars).parameters)
        assert params == {"self", "symbol", "start", "end", "adjusted"}

    def test_the_calendar_default_is_equity(self) -> None:
        default = inspect.signature(AlpacaAdapter.__init__).parameters["calendar"].default
        assert default is US_EQUITY

    def test_a_continuous_calendar_never_asks_for_splits(self) -> None:
        """Skipped, not merely inert.

        A crypto pair has no corporate actions, and ADR-0045's guard fails *loud*
        (a warning per window) when it cannot ask. A warning about a cross-check
        that could never apply is noise that trains an operator to ignore
        warnings.
        """
        client = _SplitSpy({"BTC/USD": [_bar("BTC/USD", 10), _bar("BTC/USD", 11, 200.0)]})
        adapter = AlpacaAdapter(client, calendar=CRYPTO_24_7)
        bars = adapter.get_bars(
            "BTC/USD", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC)
        )
        assert len(bars) == 2
        assert client.split_calls == 0

    def test_an_equity_calendar_still_verifies(self) -> None:
        """The guard ADR-0045 built must not have been switched off for everyone.

        Asserted on the call count rather than on the spy's exception, because
        ``_splits_in`` catches every ``Exception`` and warns — a failed lookup is
        never evidence the data is wrong (ADR-0045). That swallowing is exactly
        why the crypto side has to *skip* the call rather than let it fail.
        """
        client = _SplitSpy({"AAPL": [_bar("AAPL", 10), _bar("AAPL", 11, 200.0)]})
        adapter = AlpacaAdapter(client, calendar=US_EQUITY)
        adapter.get_bars(
            "AAPL", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC)
        )
        assert client.split_calls == 1

    def test_a_crypto_adapter_serves_bars_under_the_pair_symbol(self) -> None:
        client = FakeAlpacaClient({"ETH/USD": [_bar("ETH/USD", 10, 3000.0)]})
        adapter = AlpacaAdapter(client, calendar=CRYPTO_24_7)
        bars = adapter.get_bars(
            "ETH/USD", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC)
        )
        assert [b.symbol for b in bars] == ["ETH/USD"]

    @pytest.mark.parametrize("calendar", [US_EQUITY, CRYPTO_24_7])
    def test_an_injected_client_still_refuses_a_feed(self, calendar: MarketCalendar) -> None:
        with pytest.raises(ValueError, match="feed applies only when"):
            AlpacaAdapter(FakeAlpacaClient(), feed="iex", calendar=calendar)


class TestBrokerIsAssetClassAgnostic:
    """The finding: the loop needed nothing. Everything venue-specific is lower down."""

    def test_the_calendar_default_is_equity(self) -> None:
        default = inspect.signature(AlpacaBroker.__init__).parameters["calendar"].default
        assert default is US_EQUITY

    @pytest.mark.parametrize("calendar", [US_EQUITY, CRYPTO_24_7])
    def test_a_pair_symbol_fills_and_reconciles_like_any_other(
        self, calendar: MarketCalendar
    ) -> None:
        """`BTC/USD` is just a symbol to the broker, once the client canonicalizes."""
        client = FakeAlpacaClient(cash=100_000.0)
        client.set_price("BTC/USD", 62_000.0)
        broker = AlpacaBroker(client, calendar=calendar)
        broker.submit(Order(symbol="BTC/USD", qty=0.001, side=Side.BUY))
        fills = broker.on_bar({"BTC/USD": _bar("BTC/USD", 14, 62_000.0)})
        assert [f.symbol for f in fills] == ["BTC/USD"]
        assert broker.portfolio.positions["BTC/USD"].qty == pytest.approx(0.001)

    def test_the_duplicate_guard_keys_on_the_pair_symbol(self) -> None:
        """ADR-0036's guard was written with no market in mind; it holds here."""
        client = FakeAlpacaClient(cash=100_000.0, auto_fill=False)
        client.set_price("BTC/USD", 62_000.0)
        broker = AlpacaBroker(client, calendar=CRYPTO_24_7)
        broker.submit(Order(symbol="BTC/USD", qty=0.001, side=Side.BUY))
        broker.submit(Order(symbol="BTC/USD", qty=0.001, side=Side.BUY))
        assert len(broker.pending_order_ids) == 1
        assert len(broker.rejections) == 1
        assert "still working at the venue" in broker.rejections[0][1]

    def test_an_exit_is_never_blocked_by_a_working_entry(self) -> None:
        """Long-or-flat means a SELL is the only way out (ADR-0011/0036)."""
        client = FakeAlpacaClient(cash=100_000.0, auto_fill=False)
        client.set_price("BTC/USD", 62_000.0)
        broker = AlpacaBroker(client, calendar=CRYPTO_24_7)
        broker.submit(Order(symbol="BTC/USD", qty=0.001, side=Side.BUY))
        broker.submit(Order(symbol="BTC/USD", qty=0.001, side=Side.SELL))
        assert len(broker.pending_order_ids) == 2
        assert broker.rejections == []


class TestAnExitIsNeverBlockedByRounding:
    """A live crypto session could not sell what it held (ADR-0058).

    Observed on 2026-08-14, the last bar of a real `--live` session::

        REJECT SELL ETH/USD (Alpaca refused sell 13.339 ETH/USD (HTTP 403,
        code 40310000): insufficient balance for ETH
        (requested: 13.338989, available: 13.33898895))

    `sizing.SHARE_PRECISION = 6` rounds the delta to six decimals, and
    `round(-13.33898895, 6)` is `-13.338989` — **more than is held**. Alpaca's
    crypto `min_trade_increment` is `1e-9`, so a reconciled crypto quantity
    routinely carries nine decimals and rounding to six rounds *up* about half
    the time. US equity fractional shares are already quantized at six decimals
    or fewer, so `round` is exact there and the equity path never sees it.

    This is a **domain-invariant break, not a cosmetic refusal**: this bench is
    long-or-flat (ADR-0011), a SELL is the only exit there is, and ADR-0013/0031
    and ADR-0036 all go out of their way to keep exits unblocked. A position that
    cannot be sold is the worst failure this file could have found.
    """

    def test_a_full_exit_sized_at_share_precision_would_oversell(self) -> None:
        """The arithmetic, stated on its own so the cause is not in doubt."""
        from trading.sizing import SHARE_PRECISION

        held = 13.33898895
        assert round(0.0 - held, SHARE_PRECISION) == -13.338989
        assert held < 13.338989, "the sized exit asks for more than the account holds"

    def test_the_broker_trims_a_dust_oversell_to_what_is_held(self) -> None:
        client = FakeAlpacaClient(cash=100_000.0)
        client.set_price("ETH/USD", 1870.0)
        broker = AlpacaBroker(client, calendar=CRYPTO_24_7)
        broker.submit(Order(symbol="ETH/USD", qty=13.33898895, side=Side.BUY))
        broker.on_bar({"ETH/USD": _bar("ETH/USD", 14, 1870.0)})

        held = broker.portfolio.positions["ETH/USD"].qty
        broker.submit(Order(symbol="ETH/USD", qty=round(held, 6), side=Side.SELL))
        fills = broker.on_bar({"ETH/USD": _bar("ETH/USD", 14, 1870.0)})

        assert broker.rejections == [], broker.rejections
        assert len(fills) == 1
        assert fills[0].qty <= held, "the exit still asks for more than is held"
        assert "ETH/USD" not in broker.portfolio.positions, "the position did not fully exit"

    def test_a_buy_is_never_trimmed(self) -> None:
        """Only a SELL can oversell; a BUY has no holding to exceed."""
        client = FakeAlpacaClient(cash=100_000.0)
        client.set_price("ETH/USD", 1870.0)
        broker = AlpacaBroker(client, calendar=CRYPTO_24_7)
        broker.submit(Order(symbol="ETH/USD", qty=1.234567891, side=Side.BUY))
        fills = broker.on_bar({"ETH/USD": _bar("ETH/USD", 14, 1870.0)})
        assert fills[0].qty == pytest.approx(1.234567891)

    def test_a_genuinely_oversized_sell_is_left_for_the_venue_to_refuse(self) -> None:
        """The trim is for *rounding dust*, never a licence to rewrite an order.

        An exit for twice what is held is a bug somewhere upstream, and silently
        halving it would hide that. It goes to the venue, which refuses it, and
        the refusal is recorded (ADR-0041).
        """
        client = FakeAlpacaClient(cash=100_000.0)
        client.set_price("ETH/USD", 1870.0)
        broker = AlpacaBroker(client, calendar=CRYPTO_24_7)
        broker.submit(Order(symbol="ETH/USD", qty=1.0, side=Side.BUY))
        broker.on_bar({"ETH/USD": _bar("ETH/USD", 14, 1870.0)})

        with pytest.raises(ValueError, match="cannot sell"):
            broker.submit(Order(symbol="ETH/USD", qty=2.0, side=Side.SELL))
            broker.on_bar({"ETH/USD": _bar("ETH/USD", 14, 1870.0)})

    def test_selling_a_symbol_that_is_not_held_is_untouched(self) -> None:
        """No position means nothing to trim against; the venue decides."""
        client = FakeAlpacaClient(cash=100_000.0)
        client.set_price("ETH/USD", 1870.0)
        broker = AlpacaBroker(client, calendar=CRYPTO_24_7)
        with pytest.raises(ValueError, match="cannot sell"):
            broker.submit(Order(symbol="ETH/USD", qty=0.5, side=Side.SELL))


class TestAssetInfoCarriesMinOrderSize:
    """Recorded metadata, deliberately not a client-side gate (ADR-0058)."""

    def test_it_defaults_to_none_not_zero(self) -> None:
        """`None` means 'the venue did not say'; `0.0` would mean 'no minimum'."""
        assert AssetInfo(symbol="AAPL", tradable=True, fractionable=True).min_order_size is None

    def test_it_round_trips(self) -> None:
        info = AssetInfo(
            symbol="BTC/USD", tradable=True, fractionable=True, min_order_size=1.5739e-05
        )
        assert info.min_order_size == pytest.approx(1.5739e-05)
        assert replace(info, min_order_size=None).min_order_size is None

    def test_the_fake_can_script_it(self) -> None:
        client = FakeAlpacaClient()
        client.set_asset("SHIB/USDT", fractionable=False, min_order_size=86_880.973066898)
        asset = client.get_asset("SHIB/USDT")
        assert asset.fractionable is False
        assert asset.min_order_size == pytest.approx(86_880.973066898)

    def test_the_published_minimum_is_below_the_binding_notional_floor(self) -> None:
        """Why no client-side gate exists, as arithmetic rather than opinion.

        Measured 2026-08-14 on BTC/USD at ~$62,800: `min_order_size` is
        1.5739e-05 (~$0.99), the venue accepted 0.00016 (~$10.05) and refused
        0.000155 (~$9.73) with `cost basis must be >= minimal amount of order 10`.
        A gate on the published number would pass an order a full order of
        magnitude below the real floor — a false negative dressed as a check.
        """
        published_minimum, price = 1.5739e-05, 62_800.0
        assert published_minimum * price < 1.0
        assert 0.000155 * price < 10.0 < 0.00016 * price


class TestStandInsCannotTestTheVenue:
    """ADR-0040's lesson, fifth sighting — pinned so it cannot be re-learned.

    Alpaca's crypto endpoint answers `datetime.min` with **one** bar (today's
    forming one), which is *quieter* than the equity zero ADR-0047 fixed: a
    non-empty answer never trips per-symbol absence (ADR-0035) or the
    universe-wide empty ERROR, so a live session would prime one bar and look
    healthy. Neither offline stand-in can express that.
    """

    def test_synthetic_clips_an_absurd_start_to_its_epoch(self) -> None:
        adapter = SyntheticAdapter(seed=7, frequency=Frequency.parse("1d", calendar=CRYPTO_24_7))
        bars = adapter.get_bars(
            "BTC/USD", datetime.min.replace(tzinfo=UTC), datetime(2021, 1, 10, tzinfo=UTC)
        )
        assert len(bars) > 1, "SyntheticAdapter clips rather than refusing — it cannot test this"
        assert bars[0].ts.year >= 1990

    def test_the_fake_adapter_merely_filters_the_range(self) -> None:
        series = [_bar("BTC/USD", day) for day in range(10, 15)]
        adapter = FakeAdapter(series)
        bars = adapter.get_bars(
            "BTC/USD", datetime.min.replace(tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC)
        )
        assert len(bars) == len(series), "FakeAdapter filters — it cannot test this either"

    def test_the_real_venues_answer_differs_from_both_and_is_only_asserted_live(self) -> None:
        """The statement of record, kept next to the two stand-ins it indicts.

        Measured 2026-08-14, BTC/USD daily: `datetime.min` -> **1** bar,
        `1900-01-01` -> 2,052 bars from 2021-01-01, `now-5d` -> 5. The equity tape
        answered the same absurd start with **0**. This assertion is arithmetic
        about the recorded numbers; the live check is in the nightly contract
        test, which is the only place it can honestly run.
        """
        crypto_absurd_start_bars, equity_absurd_start_bars = 1, 0
        assert crypto_absurd_start_bars > equity_absurd_start_bars


class TestSeamStaysTyped:
    """No SDK type escapes (ADR-0017), and the fake still satisfies the protocol."""

    def test_the_fake_is_still_a_structural_alpaca_client(self) -> None:
        assert isinstance(FakeAlpacaClient(), AlpacaClient)

    def test_the_protocol_gained_no_crypto_specific_call(self) -> None:
        """The venue split cost the seam **no** new method (ADR-0017/0058).

        `cancel_order` (ADR-0036) and `get_splits` (ADR-0045) were each a widening
        the seam paid for. Crypto itself is not: it rides the seven calls that
        already existed at the time, which was the load-bearing claim that the seam
        was right. `list_assets` (KAN-863, ADR-0073) is a later, unrelated eighth
        call -- it exists so a tape-density screen can enumerate a venue's whole
        listing, not because crypto needed a new call of its own.
        """
        calls = {name for name in vars(AlpacaClient) if not name.startswith("_")}
        assert calls == {
            "get_daily_bars",
            "get_bars",
            "get_splits",
            "submit_order",
            "get_order",
            "cancel_order",
            "get_account",
            "list_positions",
            "get_asset",
            "list_assets",
        }


class TestCryptoAdapterPollingWindow:
    """The bounded window (ADR-0047) is what already prevents the quiet failure."""

    def test_a_bounded_window_is_what_the_feed_asks_for(self) -> None:
        """`fetch_span` is still equity-shaped, and that is the safe direction.

        Recorded rather than fixed (ADR-0053 assessed it, ADR-0058 confirms it
        against a real continuous venue): it over-asks a 24/7 source, so the
        ADR-0042 warmup cannot be truncated. What matters here is only that the
        span is finite — an unbounded start is the request the crypto endpoint
        answers with one bar.
        """
        from trading.data.recent_window import fetch_span

        span = fetch_span(512, timedelta(days=1))
        assert timedelta(0) < span < timedelta(days=365 * 50)
