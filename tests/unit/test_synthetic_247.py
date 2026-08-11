"""The synthetic generator's 24/7 (continuous-market) mode — ADR-0056, KAN-830.

EPIC-87 was written assuming ``SyntheticAdapter`` could already produce 24/7 bars,
so all three phase-1 cards would be testable with no network and no crypto data.
Measured against ``main`` @ ``cfb4d85``, that was false: a daily request returned
weekday-only bars and an intraday one filled the nominal 13:30-20:00 UTC equity
session. :class:`TestTheDefectAsMeasured` pins the card's two numbers as the shape
the continuous mode must now have.

Two things this file is careful about.

**ADR-0030 is the invariant most at risk.** The equity mode indexes a bar by its
*weekday* count from :data:`EPOCH`; a continuous series indexes *calendar days* —
a different counting function on the same anchor. Range independence has to be
re-proved on the new counting, at daily and at a sub-daily interval, including
across a weekend where the two indexings disagree most visibly.

**ADR-0040's lesson applies to this file's own subject.** The point of an offline
24/7 fixture is to be a *faithful* stand-in, not a forgiving one, so the ways this
generator is knowingly *not* faithful are pinned in :class:`TestStandInLimits`
rather than left to be discovered by a test that passes for the wrong reason.
"""

from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from statistics import fmean, stdev

import pytest

from trading.calendar import CRYPTO_24_7, US_EQUITY, MarketCalendar
from trading.data.synthetic import EPOCH, SyntheticAdapter, SyntheticParams
from trading.frequency import DAILY, TRADING_DAYS_PER_YEAR, Frequency
from trading.types import Bar

_ONE_DAY = timedelta(days=1)
_MIDNIGHT = time(0, tzinfo=UTC)


def _continuous(label: str) -> Frequency:
    """The ``label`` cadence on a market that never closes (ADR-0054)."""
    return Frequency.parse(label, calendar=CRYPTO_24_7)


def _adapter(
    label: str = "1d", *, seed: int = 1, params: SyntheticParams | None = None
) -> SyntheticAdapter:
    return SyntheticAdapter(seed=seed, params=params, frequency=_continuous(label))


def _at_or_after(bars: list[Bar], start: datetime) -> list[Bar]:
    """The tail of ``bars`` from ``start`` on — what a sub-range must reproduce."""
    return [b for b in bars if b.ts >= start]


def _values(bars: list[Bar]) -> list[tuple[float, float, float, float, int]]:
    """Bar values without their timestamps, to compare two *different* spans."""
    return [(b.open, b.high, b.low, b.close, b.volume) for b in bars]


# --- the defect, as the card measured it --------------------------------------


class TestTheDefectAsMeasured:
    """KAN-830's two measurements, now the contract of the continuous mode."""

    def test_daily_emits_every_calendar_day_including_weekends(self) -> None:
        # Measured on main @ cfb4d85: get_bars("BTC", 2021-01-01..2021-01-15) at 1d
        # returned 11 bars, weekdays only. 2021-01-01 .. 2021-01-15 inclusive is 15
        # calendar days, and a market that never closes trades all of them.
        bars = _adapter().get_bars(
            "BTC", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 1, 15, tzinfo=UTC)
        )
        assert len(bars) == 15
        assert {b.ts.weekday() for b in bars} == set(range(7))
        stamps = [b.ts for b in bars]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)

    def test_hourly_steps_the_full_1440_minute_day(self) -> None:
        # Measured on main: one day at 1h yielded 7 bars stamped 13:30..19:30 UTC,
        # confined to the equity session. A continuous day is 24 hourly bars from
        # midnight, START-stamped (ADR-0022).
        day = datetime(2021, 1, 4, tzinfo=UTC)
        bars = _adapter("1h").get_bars("BTC", day, day)
        assert [b.ts for b in bars] == [day + i * timedelta(hours=1) for i in range(24)]

    def test_weekend_days_are_full_trading_days_intraday_too(self) -> None:
        # Sat 2021-01-02 and Sun 2021-01-03: the equity mode emits nothing at all.
        bars = _adapter("1h").get_bars(
            "BTC", datetime(2021, 1, 2, tzinfo=UTC), datetime(2021, 1, 3, tzinfo=UTC)
        )
        assert len(bars) == 48
        assert {b.ts.weekday() for b in bars} == {5, 6}


class TestContinuousDayGrid:
    """What a continuous day looks like: which stamps, how many, and no gaps."""

    @pytest.mark.parametrize(("label", "per_day"), [("1h", 24), ("30m", 48), ("5m", 288)])
    def test_slots_per_day(self, label: str, per_day: int) -> None:
        day = datetime(2022, 6, 15, tzinfo=UTC)
        assert len(_adapter(label).get_bars("BTC", day, day)) == per_day

    def test_one_minute_day_is_1440_bars(self) -> None:
        day = datetime(2022, 6, 15, tzinfo=UTC)
        bars = _adapter("1m").get_bars("BTC", day, day)
        assert len(bars) == 1440
        assert bars[0].ts.timetz() == _MIDNIGHT
        assert bars[-1].ts.timetz() == time(23, 59, tzinfo=UTC)

    def test_daily_bars_are_stamped_at_utc_midnight(self) -> None:
        # ADR-0053's convention: a 24/7 daily bar is a rolling 24-hour window
        # closing at UTC midnight, so it is START-stamped at UTC midnight.
        bars = _adapter().get_bars(
            "BTC", datetime(2021, 3, 1, tzinfo=UTC), datetime(2021, 3, 10, tzinfo=UTC)
        )
        assert all(b.ts.timetz() == _MIDNIGHT for b in bars)

    def test_there_is_no_gap_between_consecutive_days(self) -> None:
        # The whole difference from the session shape: the last bar of a day is
        # adjacent to the first bar of the next, so the intraday grid is one
        # unbroken sequence of `interval`-spaced stamps with no overnight hole.
        bars = _adapter("1h").get_bars(
            "BTC", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 1, 4, tzinfo=UTC)
        )
        assert len(bars) == 96
        gaps = {later.ts - earlier.ts for earlier, later in pairwise(bars)}
        assert gaps == {timedelta(hours=1)}

    def test_bars_are_valid_ohlcv(self) -> None:
        bars = _adapter("30m", seed=5).get_bars(
            "BTC", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 1, 5, tzinfo=UTC)
        )
        assert bars
        for bar in bars:
            assert bar.ts.tzinfo is not None
            assert bar.open > 0 and bar.close > 0
            assert bar.high >= max(bar.open, bar.close)
            assert bar.low <= min(bar.open, bar.close)
            assert bar.volume > 0

    def test_raw_equals_adjusted(self) -> None:
        # ADR-0021: no corporate actions in GBM, in either mode.
        adapter = _adapter(seed=3)
        start, end = datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 2, 1, tzinfo=UTC)
        assert adapter.get_bars("BTC", start, end, adjusted=True) == adapter.get_bars(
            "BTC", start, end, adjusted=False
        )

    def test_naive_bounds_are_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _adapter().get_bars("BTC", datetime(2021, 1, 1), datetime(2021, 1, 5))


# --- how the mode is selected -------------------------------------------------


class TestModeSelection:
    """A construction property carried by the frequency's calendar (ADR-0022/0054)."""

    def test_equity_is_the_default(self) -> None:
        # The safe one: an adapter built the way every existing caller builds it
        # emits weekday-only session bars.
        bars = SyntheticAdapter(seed=1).get_bars(
            "AAA", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 1, 15, tzinfo=UTC)
        )
        assert len(bars) == 11
        assert {b.ts.weekday() for b in bars} <= {0, 1, 2, 3, 4}
        assert DAILY.calendar == US_EQUITY

    def test_get_bars_never_learns_about_markets(self) -> None:
        # ADR-0022: the interval — and now the market it belongs to — is an adapter
        # construction property, never an argument to get_bars.
        params = inspect.signature(SyntheticAdapter.get_bars).parameters
        assert list(params) == ["self", "symbol", "start", "end", "adjusted"]

    def test_the_mode_follows_the_frequencys_calendar(self) -> None:
        # There is no second switch to disagree with the annualization basis: the
        # calendar that sets periods_per_year (ADR-0054) also sets the day shape,
        # so "24/7 bars annualized on 252 days" is unrepresentable.
        assert _continuous("1d").periods_per_year == 365.0
        assert _continuous("5m").periods_per_year == 105_120.0
        continuous = _adapter().get_bars(
            "BTC", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 1, 15, tzinfo=UTC)
        )
        assert len(continuous) == 15

    def test_the_two_markets_are_two_canonical_series(self) -> None:
        # ADR-0030 keys the canonical series on (symbol, seed, params, frequency),
        # and ADR-0054 made the calendar part of a Frequency's identity — so an
        # equity "1d" and a 24/7 "1d" cannot be conflated by a dict key, and their
        # bars are genuinely different series.
        assert _continuous("1d") != DAILY
        day = datetime(2021, 1, 4, tzinfo=UTC)  # a Monday: both modes emit a bar
        equity = SyntheticAdapter(seed=1).get_bars("BTC", day, day)
        continuous = _adapter().get_bars("BTC", day, day)
        assert len(equity) == len(continuous) == 1
        assert equity[0].close != continuous[0].close

    def test_an_unmodellable_calendar_is_refused_loudly(self) -> None:
        # This generator models exactly two day shapes: the nominal US session and a
        # full continuous day. A calendar that is neither — say a 24-hour weekday-only
        # market — must not silently receive 6.5-hour days while annualizing on 1440
        # minutes. get_calendar raises rather than defaulting to equity (ADR-0054);
        # so does this.
        weekday_24h = MarketCalendar("fx_like", 252.0, 1440.0)
        with pytest.raises(ValueError, match="day shape"):
            SyntheticAdapter(frequency=Frequency.parse("1h", calendar=weekday_24h))


# --- the equity series must not move ------------------------------------------


class TestEquityShapeUnmoved:
    """The default mode's bars, positions and scaling are byte-for-byte untouched."""

    def test_the_pinned_epoch_bar_is_unchanged(self) -> None:
        # The same golden test_synthetic.py pins, repeated here so this file fails
        # too if the continuous mode is built by moving the equity series.
        assert SyntheticAdapter(seed=7).get_bars("AAA", EPOCH, EPOCH) == [
            Bar(
                symbol="AAA",
                ts=EPOCH,
                open=111.7207,
                high=113.0466,
                low=111.2719,
                close=112.8799,
                volume=1_489_648,
            )
        ]

    def test_equity_gbm_scaling_is_the_same_number_it_always_was(self) -> None:
        # The generator used to divide by frequency.TRADING_DAYS_PER_YEAR and now
        # divides by its calendar's days_per_year. For the equity calendar those are
        # the same float, which is why the equity series cannot move.
        assert TRADING_DAYS_PER_YEAR == US_EQUITY.days_per_year == 252.0

    @pytest.mark.parametrize(("label", "per_session"), [("1h", 7), ("30m", 13), ("5m", 78)])
    def test_equity_sessions_keep_their_slot_counts(self, label: str, per_session: int) -> None:
        day = datetime(2022, 1, 3, tzinfo=UTC)  # a Monday
        bars = SyntheticAdapter(frequency=Frequency.parse(label)).get_bars("AAA", day, day)
        assert len(bars) == per_session
        assert bars[0].ts.timetz() == time(13, 30, tzinfo=UTC)

    def test_equity_weekends_still_emit_nothing(self) -> None:
        saturday = datetime(2021, 1, 2, tzinfo=UTC)
        assert SyntheticAdapter().get_bars("AAA", saturday, saturday) == []
        assert (
            SyntheticAdapter(frequency=Frequency.parse("1h")).get_bars("AAA", saturday, saturday)
            == []
        )


# --- ADR-0030 on the new counting function ------------------------------------


class TestContinuousRangeIndependence:
    """A bar is a pure function of its absolute position from EPOCH — on calendar days."""

    _PARENT_START = datetime(2019, 1, 1, tzinfo=UTC)
    _SUB_START = datetime(2020, 3, 14, tzinfo=UTC)  # a Saturday
    _END = datetime(2021, 7, 2, tzinfo=UTC)

    def test_daily_sub_range_equals_the_tail_of_the_parent(self) -> None:
        adapter = _adapter(seed=5)
        parent = adapter.get_bars("BTC", self._PARENT_START, self._END)
        sub = adapter.get_bars("BTC", self._SUB_START, self._END)
        assert len(sub) > 400
        assert sub == _at_or_after(parent, self._SUB_START)

    def test_daily_interior_window_equals_the_parent_slice(self) -> None:
        adapter = _adapter(seed=5)
        parent = adapter.get_bars("BTC", self._PARENT_START, self._END)
        inner_start = datetime(2020, 5, 2, tzinfo=UTC)  # Saturday
        inner_end = datetime(2020, 8, 9, tzinfo=UTC)  # Sunday
        inner = adapter.get_bars("BTC", inner_start, inner_end)
        assert len(inner) == 100
        assert inner == [b for b in parent if inner_start <= b.ts <= inner_end]

    def test_two_fresh_adapters_agree_across_different_ranges(self) -> None:
        parent = _adapter(seed=5).get_bars("BTC", self._PARENT_START, self._END)
        sub = _adapter(seed=5).get_bars("BTC", self._SUB_START, self._END)
        assert sub == _at_or_after(parent, self._SUB_START)

    def test_two_different_spans_are_not_the_same_path(self) -> None:
        # The pre-ADR-0030 tell: a per-call walk made every span replay the same
        # path from its own first bar.
        adapter = _adapter(seed=5)
        first = adapter.get_bars("BTC", self._PARENT_START, datetime(2020, 3, 13, tzinfo=UTC))
        second = adapter.get_bars("BTC", self._SUB_START, self._END)
        shared = min(len(first), len(second))
        assert shared > 100
        assert _values(first[:shared]) != _values(second[:shared])

    def test_a_window_that_starts_and_ends_on_a_weekend_is_a_true_slice(self) -> None:
        # Where the two indexings differ most: the equity mode has no position at
        # all for these timestamps, so getting them right is entirely new arithmetic.
        adapter = _adapter(seed=2)
        parent = adapter.get_bars("BTC", datetime(2021, 1, 1, tzinfo=UTC), self._END)
        sat, sun = datetime(2021, 5, 8, tzinfo=UTC), datetime(2021, 5, 16, tzinfo=UTC)
        weekend = adapter.get_bars("BTC", sat, sun)
        assert len(weekend) == 9
        assert weekend == [b for b in parent if sat <= b.ts <= sun]

    def test_sub_daily_sub_range_equals_the_tail_of_the_parent(self) -> None:
        adapter = _adapter("1h", seed=3)
        parent_start = datetime(2021, 3, 5, tzinfo=UTC)
        sub_start = datetime(2021, 3, 12, tzinfo=UTC)
        end = datetime(2021, 3, 18, tzinfo=UTC)
        parent = adapter.get_bars("BTC", parent_start, end)
        sub = adapter.get_bars("BTC", sub_start, end)
        assert len(sub) == 7 * 24
        assert sub == _at_or_after(parent, sub_start)

    def test_sub_daily_single_day_equals_the_parent_slice(self) -> None:
        adapter = _adapter("30m", seed=3)
        parent = adapter.get_bars(
            "BTC", datetime(2021, 3, 5, tzinfo=UTC), datetime(2021, 3, 11, tzinfo=UTC)
        )
        sunday = datetime(2021, 3, 7, tzinfo=UTC)
        one_day = adapter.get_bars("BTC", sunday, sunday)
        assert len(one_day) == 48
        assert one_day == [b for b in parent if b.ts.date() == sunday.date()]

    def test_every_day_advances_the_series_the_index_is_injective(self) -> None:
        # Found by breaking the fix and watching which tests noticed: every test
        # above passes with the *weekday* index left on a continuous series, because
        # a wrong-but-pure position function is still a pure function of the
        # timestamp — range independence constrains purity, not injectivity. So this
        # pins the other half: distinct trading days must get distinct positions.
        # Under the weekday index a Saturday and a Sunday share the preceding
        # Friday's position and come back as byte-identical bars.
        bars = _adapter(seed=6).get_bars(
            "BTC", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 7, 31, tzinfo=UTC)
        )
        assert len(bars) == 212
        assert len({_values([b])[0] for b in bars}) == len(bars)
        assert all(_values([a]) != _values([b]) for a, b in pairwise(bars))

    def test_the_position_is_the_calendar_day_count_from_the_epoch(self) -> None:
        # The counting function itself: 365 consecutive daily bars from the epoch,
        # one per calendar day, so bar i sits at EPOCH + i days.
        bars = _adapter(seed=4).get_bars("BTC", EPOCH, EPOCH + 364 * _ONE_DAY)
        assert [b.ts for b in bars] == [EPOCH + i * _ONE_DAY for i in range(365)]


# --- the bridge spans the whole day -------------------------------------------


class TestBridgeSpansTheWholeDay:
    """1h/30m/5m must agree with 1d at every day's close, over 1440 minutes."""

    _START = datetime(2021, 4, 1, tzinfo=UTC)
    _END = datetime(2021, 4, 10, tzinfo=UTC)

    def _daily_closes(self) -> dict[object, float]:
        daily = _adapter(seed=3).get_bars("BTC", self._START, self._END)
        assert len(daily) == 10
        return {b.ts.date(): b.close for b in daily}

    @pytest.mark.parametrize("label", ["1h", "30m", "5m"])
    def test_the_last_bar_of_a_day_closes_on_the_daily_bar(self, label: str) -> None:
        intraday = _adapter(label, seed=3).get_bars("BTC", self._START, self._END)
        last_of_day = {b.ts.date(): b.close for b in intraday}  # dict keeps the last
        assert last_of_day == self._daily_closes()

    def test_the_agreement_holds_at_one_minute_bars_too(self) -> None:
        day = datetime(2021, 4, 5, tzinfo=UTC)
        minute = _adapter("1m", seed=3).get_bars("BTC", day, day)
        daily = _adapter(seed=3).get_bars("BTC", day, day)
        assert len(minute) == 1440
        assert minute[-1].close == daily[0].close

    def test_the_bridge_covers_the_day_it_does_not_stop_at_the_session(self) -> None:
        # The failure this guards against: bridging the daily close across only
        # 6.5 hours and leaving 17.5 hours of the day unpriced. The last bar's
        # window must end exactly at the next daily bar's start.
        day = datetime(2021, 4, 5, tzinfo=UTC)
        bars = _adapter("1h", seed=3).get_bars("BTC", day, day)
        assert bars[-1].ts + timedelta(hours=1) == day + _ONE_DAY


# --- the GBM scaling correction, measured --------------------------------------


class TestRealizedVolatility:
    """A 24/7 series must realize the annual_vol it was configured with.

    A bar's per-step sigma is ``annual_vol / sqrt(days_per_year)``, so leaving the
    divisor at the equity 252 on a 365-bar year would emit a series whose realized
    annualized volatility is ``sqrt(365/252) = 1.2035x`` the configured one. These
    tests *measure* the realized figure rather than asserting the formula.
    """

    _VOL = 0.60  # crypto-like: three times the equity default
    _DRIFT = 0.30
    _START = datetime(2000, 1, 1, tzinfo=UTC)
    _END = datetime(2020, 1, 1, tzinfo=UTC)

    def _daily_log_returns(self) -> list[float]:
        params = SyntheticParams(annual_drift=self._DRIFT, annual_vol=self._VOL)
        bars = _adapter(seed=1, params=params).get_bars("BTC", self._START, self._END)
        assert len(bars) > 7_000, "want a few thousand draws for a stable estimate"
        return [math.log(later.close / earlier.close) for earlier, later in pairwise(bars)]

    def test_realized_annualized_vol_matches_the_configured_vol(self) -> None:
        # Measured over 7,305 daily bars (2000-2020): 0.5892 against a configured
        # 0.6000, i.e. -1.8%, about two standard errors of a sample stdev at this
        # size (1/sqrt(2n) = 0.83%). The equity series measures 0.5899 for the same
        # configuration, so the two markets realize the same vol on their own years.
        returns = self._daily_log_returns()
        realized = stdev(returns) * math.sqrt(365.0)
        assert abs(realized - self._VOL) < 0.03 * self._VOL, realized

    def test_the_per_step_sigma_uses_365_days_and_measurably_not_252(self) -> None:
        # The discriminating measurement: the per-bar sigma the generator actually
        # emitted. Had the divisor stayed at the equity 252, the realized per-step
        # stdev would sit on the *other* number here — the two differ by 1.2035x,
        # which is ~24 measured standard errors apart at this sample size.
        realized = stdev(self._daily_log_returns())
        on_365 = self._VOL / math.sqrt(365.0)
        on_252 = self._VOL / math.sqrt(252.0)
        assert abs(realized - on_365) < 0.03 * on_365, realized
        assert abs(realized - on_252) > 0.15 * on_252, realized

    def test_realized_drift_matches_the_configured_drift(self) -> None:
        returns = self._daily_log_returns()
        realized = fmean(returns) * 365.0
        # Drift is estimated far less precisely than vol (the standard error is
        # vol/sqrt(years) ~ 0.13 here), so this is a sanity band, not a tight one.
        assert abs(realized - self._DRIFT) < 0.30, realized

    def test_sub_daily_bars_annualize_to_the_same_vol(self) -> None:
        # The bridge distributes the day's move across its slots, so an hourly 24/7
        # series annualized on 365*24 must land on the same configured vol as the
        # daily one. Measured over one year of 24/7 hourly bars: 0.6124 against a
        # configured 0.6000 (+2.1%); at 30m 0.6023 and at 5m 0.5991 over the same
        # span, so the agreement tightens as the slot count grows. It is not exact
        # in either direction: the bridge pins the day's total move before drawing
        # its path, so per-bar variance is conditional-on-the-day (ADR-0030).
        params = SyntheticParams(annual_drift=self._DRIFT, annual_vol=self._VOL)
        bars = _adapter("1h", seed=1, params=params).get_bars(
            "BTC", datetime(2019, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(bars) > 8_000
        returns = [math.log(later.close / earlier.close) for earlier, later in pairwise(bars)]
        realized = stdev(returns) * math.sqrt(365.0 * 24.0)
        assert abs(realized - self._VOL) < 0.05 * self._VOL, realized


# --- what this fixture is NOT (ADR-0040's lesson) -----------------------------


class TestStandInLimits:
    """Pin the ways the continuous mode is knowingly unlike a real crypto venue.

    ADR-0047's real bug (Alpaca answers an absurd start with an empty response, not
    an error) stayed invisible for months because ``SyntheticAdapter`` clips and
    ``FakeAdapter`` filters — stand-ins more forgiving than the provider. The 24/7
    mode inherits the clipping, so these tests exist to make that visible where
    someone might otherwise reach for this adapter to test a provider-shaped
    behaviour.
    """

    def test_a_pre_epoch_start_is_clipped_not_empty(self) -> None:
        # Inherited from ADR-0030 and unchanged: the paper feed's far-past poll must
        # stay cheap. So this adapter CANNOT stand in for the bounded-window
        # behaviour of a real provider (ADR-0047) on the continuous path either.
        adapter = _adapter(seed=5)
        end = datetime(2021, 1, 31, tzinfo=UTC)
        far_past = adapter.get_bars("BTC", datetime.min.replace(tzinfo=UTC), end)
        assert far_past, "a real venue may answer an absurd start with nothing; this clips"
        assert far_past[0].ts == EPOCH
        start = datetime(2021, 1, 1, tzinfo=UTC)
        assert _at_or_after(far_past, start) == adapter.get_bars("BTC", start, end)

    def test_no_inception_date_is_modelled(self) -> None:
        # A real coin has a first trading day; this series starts in 1990 for every
        # symbol. A crypto adapter test that asks for pre-inception data will pass
        # here and fail against a venue.
        bars = _adapter(seed=5).get_bars(
            "BTC", datetime(1990, 1, 1, tzinfo=UTC), datetime(1990, 1, 31, tzinfo=UTC)
        )
        assert len(bars) == 31

    def test_no_maintenance_window_or_venue_outage(self) -> None:
        # Every calendar day is a full day, forever: no exchange maintenance halt,
        # no missing bars, no partial days.
        bars = _adapter("1h", seed=5).get_bars(
            "BTC", datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 12, 31, tzinfo=UTC)
        )
        assert len(bars) == 365 * 24

    def test_still_gbm_no_fat_tails(self) -> None:
        # ADR-0012's caveat, which crypto makes sharper: real 24/7 markets have 20%
        # days. Under GBM at 60% annual vol a 20% daily move is a ~6.4-sigma event,
        # so this series will essentially never produce one.
        params = SyntheticParams(annual_vol=0.60)
        bars = _adapter(seed=8, params=params).get_bars(
            "BTC", datetime(2010, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)
        )
        worst = min(later.close / earlier.close - 1.0 for earlier, later in pairwise(bars))
        assert worst > -0.20, f"GBM produced a 20% down day ({worst:.4f}); check the model"
