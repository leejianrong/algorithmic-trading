"""The quantity- and cash-side recovery of a fee the price report cannot see.

Every number in :class:`TestReproducesTheObservedRows` is a venue observation
transcribed from ADR-0060 §2 — four fills on two pairs across two dates and two
volume tiers. Pinning the *arithmetic* against them rather than against invented
figures is what makes this module a reading of the venue instead of a formula.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from trading.config import CRYPTO_TAKER_FEE_BPS
from trading.fees import (
    ALPACA_CRYPTO_FEE_TIERS,
    CashFee,
    QuantityFee,
    TradeLeg,
    cash_fee,
    quantity_fees,
    tier_for_volume,
    traded_notional,
)
from trading.types import Side


def buy(symbol: str, qty: float, price: float = 1.0) -> TradeLeg:
    return TradeLeg(symbol=symbol, side=Side.BUY, qty=qty, price=price)


def sell(symbol: str, qty: float, price: float = 1.0) -> TradeLeg:
    return TradeLeg(symbol=symbol, side=Side.SELL, qty=qty, price=price)


class TestThePublishedSchedule:
    """The transcription, and the one boundary an operator actually lands on."""

    def test_tier_one_taker_is_the_constant_the_cost_model_ships(self) -> None:
        """The schedule and ``CostConfig.crypto()`` must not drift apart.

        ADR-0060 sourced ``CRYPTO_TAKER_FEE_BPS`` from tier 1's taker column. If
        someone re-tunes the constant without touching the schedule this goes red,
        which is the whole point of keeping the published table in the repo.
        """
        assert ALPACA_CRYPTO_FEE_TIERS[0].tier == 1
        assert ALPACA_CRYPTO_FEE_TIERS[0].taker_bps == CRYPTO_TAKER_FEE_BPS

    def test_the_hundred_thousand_boundary_is_where_this_account_sits(self) -> None:
        """$100,000 is tier 2, and the bench's own account is just past it.

        Reconstructed trailing 30-day crypto notional on 2026-08-16 was
        $100,636.53 (ADR-0060 §2, re-measured for KAN-710), and the venue charged
        22 bps — the tier-2 taker row. So the band is half-open upward.
        """
        assert tier_for_volume(99_999.99).tier == 1
        assert tier_for_volume(100_000.0).tier == 2
        assert tier_for_volume(100_636.53).tier == 2
        assert tier_for_volume(100_636.53).taker_bps == 22.0

    def test_every_band_is_contiguous_and_the_last_is_open_ended(self) -> None:
        for lower, upper in pairwise(ALPACA_CRYPTO_FEE_TIERS):
            assert lower.max_volume_usd == upper.min_volume_usd
        assert ALPACA_CRYPTO_FEE_TIERS[-1].max_volume_usd is None
        assert tier_for_volume(5e12).tier == 8

    def test_a_negative_volume_raises_rather_than_answering_tier_one(self) -> None:
        """A negative trailing volume is a broken reconstruction, not a small one."""
        with pytest.raises(ValueError, match="cannot be negative"):
            tier_for_volume(-1.0)


class TestReproducesTheObservedRows:
    """ADR-0060 §2's four measured fills, recovered by this module's arithmetic."""

    # Row (a) is pinned at 25.0085 rather than the 25.0006 ADR-0060 printed beside
    # it: that row's own quantities give a ratio of 0.99749915, and its stated
    # ratio (0.99749936) implies 25.0064, so the ADR's two figures already disagree
    # with each other by more than either disagrees with the schedule. It is a
    # transcription slip of eight ten-thousandths of a basis point on a row that
    # aggregates four separate fills quantized at nine decimals -- worth pinning to
    # the quantities, which are the observation, and not worth a correction to a
    # conclusion that was "25 bps, to four decimals" either way.
    @pytest.mark.parametrize(
        ("label", "gross", "credited", "expected_bps"),
        [
            ("a: BTC/USD 08-14, four buys, tier 1", 0.000617391, 0.000615847, 25.0085),
            ("b: BTC/USD 08-14, one buy, tier 1", 0.00016, 0.0001596, 25.0000),
            ("c: ETH/USD 08-16, tier 2", 0.00638666, 0.006372609, 22.0005),
            ("d: BTC/USD 08-16, tier 2", 0.000190444, 0.000190025, 22.0012),
        ],
    )
    def test_gross_versus_credited_recovers_the_published_rate(
        self, label: str, gross: float, credited: float, expected_bps: float
    ) -> None:
        fee = QuantityFee(
            symbol="X/USD",
            gross_bought=gross,
            gross_sold=0.0,
            opening_qty=0.0,
            closing_qty=credited,
        )
        assert fee.implied_fee_bps == pytest.approx(expected_bps, abs=0.001)

    def test_the_two_dates_land_on_two_different_published_tiers(self) -> None:
        """Not drift: exactly one published row, then exactly the next one."""
        assert tier_for_volume(50_000.0).taker_bps == pytest.approx(25.0006, abs=0.01)
        assert tier_for_volume(100_636.53).taker_bps == pytest.approx(22.0005, abs=0.01)


class TestTheBuySideIsVisibleInQuantity:
    def test_a_completed_round_trip_still_shows_the_buy_fee(self) -> None:
        """Selling everything credited leaves ``bought - sold`` equal to the fee.

        This is the case a real session produces — it ends flat in a symbol — and it
        is the one where "the position is zero, so nothing is measurable" is the
        intuitive and wrong answer.
        """
        fee_rate = 0.0022
        bought = 10.0
        credited = bought * (1 - fee_rate)
        fee = QuantityFee("ETH/USD", bought, credited, 0.0, 0.0)
        assert fee.implied_fee_bps == pytest.approx(22.0, abs=1e-9)

    def test_a_still_open_position_shows_the_same_rate(self) -> None:
        fee_rate = 0.0022
        fee = QuantityFee("ETH/USD", 10.0, 4.0, 0.0, 10.0 * (1 - fee_rate) - 4.0)
        assert fee.implied_fee_bps == pytest.approx(22.0, abs=1e-9)

    def test_an_opening_position_is_carried_not_assumed_away(self) -> None:
        """A session that did not start flat must say so or it measures the balance."""
        assert QuantityFee("ETH/USD", 10.0, 0.0, 3.0, 12.978).implied_fee_bps == pytest.approx(
            22.0, abs=1e-6
        )
        # Pretending the 3.0 was not there reads the whole opening balance as a
        # negative fee -- loudly wrong rather than subtly wrong, which is the point.
        assumed_flat = QuantityFee("ETH/USD", 10.0, 0.0, 0.0, 12.978).implied_fee_bps
        assert assumed_flat is not None
        assert assumed_flat < -2000

    def test_using_a_net_sold_quantity_double_counts_the_sell_fee(self) -> None:
        """The named error: a SELL's fee is taken in fiat, so coin leaves in full.

        Deducting a *net* sold quantity charges the sell-side fee to the coin ledger
        as well as to the cash one, so the residual absorbs both and the buy-side
        rate comes out inflated -- here 33 bps for a 22 bps venue. It errs
        pessimistically, which is why it would survive a sanity check.
        """
        gross_correct = QuantityFee("ETH/USD", 10.0, 5.0, 0.0, 9.978 - 5.0)
        net_wrong = QuantityFee("ETH/USD", 10.0, 5.0 * 0.9978, 0.0, 9.978 - 5.0)
        assert gross_correct.implied_fee_bps == pytest.approx(22.0, abs=1e-6)
        assert net_wrong.implied_fee_bps == pytest.approx(33.0, abs=1e-6)

    def test_nothing_bought_means_no_rate_rather_than_zero(self) -> None:
        assert QuantityFee("ETH/USD", 0.0, 5.0, 5.0, 0.0).implied_fee_bps is None


class TestTheSellSideIsVisibleInCash:
    def test_cash_recovers_the_rate_independently_of_price_movement(self) -> None:
        """The notionals are realized, so a big move between the legs cancels."""
        fee_rate = 0.0022
        bought_qty, buy_price = 2.0, 100.0
        sold_qty, sell_price = 2.0, 175.0  # +75% between the legs
        buy_notional = bought_qty * buy_price
        sell_notional = sold_qty * sell_price
        closing = 1_000.0 - buy_notional + sell_notional * (1 - fee_rate)
        fee = CashFee(buy_notional, sell_notional, opening_cash=1_000.0, closing_cash=closing)
        assert fee.implied_fee_bps == pytest.approx(22.0, abs=1e-9)

    def test_a_buy_only_session_has_no_sell_side_rate(self) -> None:
        """A BUY pays its notional in full; the fee went into the coin, not the cash."""
        fee = CashFee(500.0, 0.0, opening_cash=1_000.0, closing_cash=500.0)
        assert fee.implied_fee_bps is None
        assert fee.missing_cash == pytest.approx(0.0)


class TestTheTwoLedgersAgreeOnOneSession:
    """The whole reason both exist: two assets, two readings, one rate."""

    def test_a_synthetic_session_yields_the_same_rate_from_coin_and_from_cash(self) -> None:
        fee_rate = 0.0022
        legs = [
            buy("BTC/USD", 0.5, 60_000.0),
            sell("BTC/USD", 0.2, 61_000.0),
            buy("ETH/USD", 3.0, 2_000.0),
        ]
        # What the venue would credit: coin net of the fee on each buy.
        closing = {
            "BTC/USD": 0.5 * (1 - fee_rate) - 0.2,
            "ETH/USD": 3.0 * (1 - fee_rate),
        }
        opening_cash = 100_000.0
        buys = 0.5 * 60_000.0 + 3.0 * 2_000.0
        sells = 0.2 * 61_000.0
        closing_cash = opening_cash - buys + sells * (1 - fee_rate)

        by_symbol = {f.symbol: f.implied_fee_bps for f in quantity_fees(legs, closing)}
        assert by_symbol["BTC/USD"] == pytest.approx(22.0, abs=1e-6)
        assert by_symbol["ETH/USD"] == pytest.approx(22.0, abs=1e-6)
        assert cash_fee(legs, opening_cash, closing_cash).implied_fee_bps == pytest.approx(
            22.0, abs=1e-9
        )

    def test_a_fully_exited_symbol_is_absent_from_positions_and_reads_as_flat(self) -> None:
        legs = [buy("SOL/USD", 100.0, 150.0), sell("SOL/USD", 99.78, 151.0)]
        (fee,) = quantity_fees(legs, closing={})  # the venue reports no position at all
        assert fee.closing_qty == 0.0
        assert fee.implied_fee_bps == pytest.approx(22.0, abs=1e-9)

    def test_traded_notional_counts_both_sides(self) -> None:
        """A volume tier is measured on gross turnover, buys and sells alike."""
        legs = [buy("BTC/USD", 1.0, 60_000.0), sell("BTC/USD", 1.0, 61_000.0)]
        assert traded_notional(legs) == pytest.approx(121_000.0)
