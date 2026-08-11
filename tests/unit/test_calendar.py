"""Fast, no-infra tests for the market-calendar seam behind annualization (ADR-0054).

Two things are pinned here, and they pull in opposite directions.

1. **The equity calendar is exactly what the two former module constants were.**
   ``252`` trading days and a ``390``-minute session, producing the identical
   ``periods_per_year`` for every standard label. Those literals are written out
   below rather than derived from :data:`~trading.calendar.US_EQUITY`, so a change
   to the calendar cannot quietly redefine the number this bench has always
   reported.
2. **The 24/7 calendar is a genuinely different number.** ``365`` days x ``1440``
   minutes, again as literals. Reverting ``periods_per_year`` to the equity
   constants turns these red — which is the whole point of the card: no crypto
   number may ever be annualized on an equity calendar.

No 24/7 *bars* are needed for any of it. Annualization is arithmetic on a return
series, so the Sharpe tests build their :class:`EquityPoint` series by hand;
``SyntheticAdapter`` still emits weekday-only 13:30-20:00 UTC sessions and this
slice does not change that.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from math import sqrt

import pytest

from trading.calendar import (
    CALENDARS,
    CRYPTO_24_7,
    MINUTES_PER_CALENDAR_DAY,
    US_EQUITY,
    MarketCalendar,
    get_calendar,
)
from trading.engine import EquityPoint
from trading.frequency import (
    DAILY,
    REGULAR_SESSION_MINUTES,
    STANDARD_FREQUENCIES,
    TRADING_DAYS_PER_YEAR,
    Frequency,
    frequencies_for,
)
from trading.metrics import sharpe

# The factors this bench has always reported for US equities, as literals.
EQUITY_FACTORS = {
    "1d": 252.0,
    "1h": 1638.0,  # 252 x 6.5 bars per 390-minute session
    "30m": 3276.0,  # 252 x 13
    "5m": 19656.0,  # 252 x 78
    "1m": 98280.0,  # 252 x 390
}

# What a 24/7 market is worth: 365 days x 1440 minutes.
CRYPTO_FACTORS = {
    "1d": 365.0,
    "1h": 8760.0,  # 365 x 24
    "30m": 17520.0,  # 365 x 48
    "5m": 105120.0,  # 365 x 288
    "1m": 525600.0,  # 365 x 1440
}


def _curve(returns: list[float], *, start_equity: float = 1000.0) -> list[EquityPoint]:
    """An equity curve whose per-bar simple returns are exactly ``returns``."""
    ts = datetime(2020, 1, 1, tzinfo=UTC)
    equity = start_equity
    points = [EquityPoint(ts, equity)]
    for i, r in enumerate(returns, start=1):
        equity *= 1.0 + r
        points.append(EquityPoint(ts + timedelta(days=i), equity))
    return points


class TestEquityCalendarIsUnchanged:
    """The equity calendar must reproduce the pre-ADR-0054 constants exactly."""

    def test_named_instance_carries_the_former_module_constants(self) -> None:
        assert US_EQUITY.days_per_year == 252.0
        assert US_EQUITY.minutes_per_day == 390.0

    def test_module_constants_still_resolve_to_the_equity_calendar(self) -> None:
        # report.py and data/synthetic.py import these names; they must keep working.
        assert TRADING_DAYS_PER_YEAR == 252.0
        assert REGULAR_SESSION_MINUTES == 390.0

    @pytest.mark.parametrize(("label", "expected"), sorted(EQUITY_FACTORS.items()))
    def test_every_standard_label_keeps_its_factor(self, label: str, expected: float) -> None:
        assert Frequency.parse(label).periods_per_year == expected

    @pytest.mark.parametrize(("label", "expected"), sorted(EQUITY_FACTORS.items()))
    def test_the_calendar_computes_the_same_factor(self, label: str, expected: float) -> None:
        assert US_EQUITY.periods_per_year(Frequency.parse(label).delta) == expected

    def test_parse_defaults_to_the_equity_calendar(self) -> None:
        assert Frequency.parse("5m").calendar == US_EQUITY
        assert DAILY.calendar == US_EQUITY

    def test_parse_still_takes_exactly_one_positional_argument(self) -> None:
        # cli.py calls Frequency.parse(interval) and must need no change: the
        # crypto path is a keyword, never a new required argument.
        params = list(inspect.signature(Frequency.parse).parameters.values())
        positional = [
            p
            for p in params
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        ]
        assert [p.name for p in positional] == ["label"]

    def test_standard_frequencies_are_the_equity_set(self) -> None:
        assert [f.label for f in STANDARD_FREQUENCIES] == ["1d", "1h", "30m", "5m", "1m"]
        assert all(f.calendar == US_EQUITY for f in STANDARD_FREQUENCIES)


class TestCryptoCalendar:
    """A 24/7 market annualizes on 365 x 1440, not 252 x 390."""

    def test_named_instance(self) -> None:
        assert CRYPTO_24_7.days_per_year == 365.0
        assert CRYPTO_24_7.minutes_per_day == 1440.0
        assert CRYPTO_24_7.minutes_per_day == MINUTES_PER_CALENDAR_DAY

    @pytest.mark.parametrize(("label", "expected"), sorted(CRYPTO_FACTORS.items()))
    def test_every_standard_label(self, label: str, expected: float) -> None:
        freq = Frequency.parse(label, calendar=CRYPTO_24_7)
        assert freq.periods_per_year == expected
        assert freq.calendar == CRYPTO_24_7
        assert freq.label == label

    @pytest.mark.parametrize(("label", "expected"), sorted(CRYPTO_FACTORS.items()))
    def test_a_crypto_factor_is_never_an_equity_factor(self, label: str, expected: float) -> None:
        # The regression guard: revert periods_per_year to the equity constants
        # and every one of these fails.
        assert expected != EQUITY_FACTORS[label]
        crypto = Frequency.parse(label, calendar=CRYPTO_24_7)
        assert crypto.periods_per_year != EQUITY_FACTORS[label]

    def test_daily_equals_days_per_year(self) -> None:
        assert Frequency.parse("1d", calendar=CRYPTO_24_7).periods_per_year == 365.0

    def test_a_full_day_of_minutes_agrees_with_the_daily_bar(self) -> None:
        # On a continuous market the two derivations must coincide: 1440 one-minute
        # bars a day is the same year as one daily bar a day.
        one_minute = CRYPTO_24_7.periods_per_year(timedelta(minutes=1))
        one_day = CRYPTO_24_7.periods_per_year(timedelta(days=1))
        assert one_minute == pytest.approx(one_day * 1440.0)

    def test_a_session_market_does_not_have_that_property(self) -> None:
        # And on a session market it must NOT: a daily bar covers a whole session
        # even though the session is only 390 of the day's 1440 minutes.
        one_minute = US_EQUITY.periods_per_year(timedelta(minutes=1))
        one_day = US_EQUITY.periods_per_year(timedelta(days=1))
        assert one_minute == pytest.approx(one_day * 390.0)
        assert one_minute != pytest.approx(one_day * 1440.0)


class TestTheErrorTheCardDescribes:
    """The card's arithmetic, checked rather than quoted."""

    def test_daily_factor_ratio(self) -> None:
        assert CRYPTO_FACTORS["1d"] / EQUITY_FACTORS["1d"] == pytest.approx(1.4484, abs=1e-4)

    def test_five_minute_factor_is_5_3x_out(self) -> None:
        ratio = CRYPTO_FACTORS["5m"] / EQUITY_FACTORS["5m"]
        assert ratio == pytest.approx(5.3479, abs=1e-4)
        assert sqrt(ratio) == pytest.approx(2.3125, abs=1e-4)

    def test_a_daily_sharpe_on_the_wrong_calendar_understates_by_about_a_fifth(self) -> None:
        # A fixed return series: the ONLY thing that changes is the calendar.
        curve = _curve([0.004, -0.002, 0.006, 0.001, -0.003] * 40)
        equity_sharpe = sharpe(curve, US_EQUITY.periods_per_year(timedelta(days=1)))
        crypto_sharpe = sharpe(curve, CRYPTO_24_7.periods_per_year(timedelta(days=1)))
        assert crypto_sharpe / equity_sharpe == pytest.approx(sqrt(365.0 / 252.0))
        # Understated by ~17% of the correct figure, i.e. the correct figure is
        # ~20% higher — the card's "about 20%".
        assert 1.0 - equity_sharpe / crypto_sharpe == pytest.approx(0.1691, abs=1e-4)
        assert crypto_sharpe / equity_sharpe - 1.0 == pytest.approx(0.2035, abs=1e-4)

    def test_a_5m_sharpe_on_the_wrong_calendar_is_2_3x_out(self) -> None:
        curve = _curve([0.0004, -0.0002, 0.0006, 0.0001, -0.0003] * 40)
        five_min = timedelta(minutes=5)
        equity_sharpe = sharpe(curve, US_EQUITY.periods_per_year(five_min))
        crypto_sharpe = sharpe(curve, CRYPTO_24_7.periods_per_year(five_min))
        assert crypto_sharpe / equity_sharpe == pytest.approx(2.3125, abs=1e-4)
        # The direction matters: the equity calendar FLATTERS a crypto strategy
        # only in the other direction — here it understates. What flatters is
        # computing a crypto figure on 252x390; see the ADR.
        assert equity_sharpe < crypto_sharpe


class TestFrequencyIdentity:
    """A frequency is a cadence *and* a market; the two must not be conflated."""

    def test_same_label_different_calendar_is_a_different_frequency(self) -> None:
        assert Frequency.parse("1d", calendar=CRYPTO_24_7) != DAILY
        assert Frequency.parse("5m", calendar=CRYPTO_24_7) != Frequency.parse("5m")

    def test_same_calendar_round_trips_to_an_equal_value(self) -> None:
        a = Frequency.parse("1h", calendar=CRYPTO_24_7)
        b = Frequency.parse("1h", calendar=CRYPTO_24_7)
        assert a == b
        assert hash(a) == hash(b)

    def test_intraday_flag_is_about_the_bar_not_the_market(self) -> None:
        assert Frequency.parse("1d", calendar=CRYPTO_24_7).is_intraday is False
        assert Frequency.parse("1h", calendar=CRYPTO_24_7).is_intraday is True

    def test_unknown_label_still_errors_on_a_crypto_calendar(self) -> None:
        with pytest.raises(ValueError, match="unknown frequency '2w'"):
            Frequency.parse("2w", calendar=CRYPTO_24_7)


class TestFrequenciesFor:
    def test_returns_the_standard_set_on_the_given_calendar(self) -> None:
        freqs = frequencies_for(CRYPTO_24_7)
        assert [f.label for f in freqs] == ["1d", "1h", "30m", "5m", "1m"]
        assert [f.periods_per_year for f in freqs] == [CRYPTO_FACTORS[f.label] for f in freqs]

    def test_the_equity_view_is_the_module_level_tuple(self) -> None:
        assert frequencies_for(US_EQUITY) == STANDARD_FREQUENCIES

    def test_finer_bars_have_a_larger_factor_on_either_calendar(self) -> None:
        for cal in (US_EQUITY, CRYPTO_24_7):
            factors = [f.periods_per_year for f in frequencies_for(cal)]
            assert factors == sorted(factors)


class TestCalendarValidation:
    @pytest.mark.parametrize("days", [0.0, -1.0])
    def test_rejects_non_positive_days_per_year(self, days: float) -> None:
        with pytest.raises(ValueError, match="days_per_year must be positive"):
            MarketCalendar("bad", days, 390.0)

    @pytest.mark.parametrize("minutes", [0.0, -1.0])
    def test_rejects_non_positive_minutes_per_day(self, minutes: float) -> None:
        with pytest.raises(ValueError, match="minutes_per_day must be positive"):
            MarketCalendar("bad", 252.0, minutes)

    def test_rejects_more_minutes_than_a_day_has(self) -> None:
        with pytest.raises(ValueError, match="minutes_per_day cannot exceed"):
            MarketCalendar("bad", 252.0, 1441.0)

    def test_rejects_more_days_than_a_year_has(self) -> None:
        with pytest.raises(ValueError, match="days_per_year cannot exceed"):
            MarketCalendar("bad", 367.0, 1440.0)

    def test_rejects_a_blank_name(self) -> None:
        with pytest.raises(ValueError, match="name must not be blank"):
            MarketCalendar("  ", 252.0, 390.0)

    def test_rejects_a_non_positive_interval(self) -> None:
        with pytest.raises(ValueError, match="interval must be positive"):
            US_EQUITY.periods_per_year(timedelta(0))

    def test_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            US_EQUITY.days_per_year = 365.0  # type: ignore[misc]


class TestCalendarRegistry:
    def test_named_lookup(self) -> None:
        assert get_calendar("us_equity") == US_EQUITY
        assert get_calendar("crypto_24_7") == CRYPTO_24_7

    def test_lookup_is_case_and_whitespace_insensitive(self) -> None:
        assert get_calendar("  US_Equity ") == US_EQUITY

    def test_unknown_name_lists_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match="unknown market calendar 'forex'"):
            get_calendar("forex")
        with pytest.raises(ValueError, match="crypto_24_7"):
            get_calendar("forex")

    def test_registry_keys_match_the_instances(self) -> None:
        assert {name: cal.name for name, cal in CALENDARS.items()} == {
            name: name for name in CALENDARS
        }

    def test_continuous_flag(self) -> None:
        assert CRYPTO_24_7.is_continuous is True
        assert US_EQUITY.is_continuous is False


class TestMultiDayBars:
    """Not a supported label today, but the arithmetic must not be nonsense."""

    def test_a_weekly_equity_bar_is_a_fifth_of_the_daily_factor(self) -> None:
        weekly = US_EQUITY.periods_per_year(timedelta(days=7))
        assert weekly == pytest.approx(252.0 / 7.0)

    def test_a_weekly_crypto_bar_is_52_and_change(self) -> None:
        assert CRYPTO_24_7.periods_per_year(timedelta(days=7)) == pytest.approx(365.0 / 7.0)
