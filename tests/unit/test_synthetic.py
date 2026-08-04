"""Fast unit tests for the synthetic data generator (ADR-0012, ADR-0022)."""

from __future__ import annotations

from datetime import UTC, datetime, time

from trading.data.synthetic import SyntheticAdapter
from trading.frequency import DAILY, Frequency
from trading.interfaces import DataAdapter

_START = datetime(2022, 1, 1, tzinfo=UTC)
_END = datetime(2022, 3, 31, tzinfo=UTC)

# The nominal session the intraday generator fills (mirrors synthetic.py).
_SESSION_OPEN = time(13, 30, tzinfo=UTC)
_SESSION_CLOSE = time(20, 0, tzinfo=UTC)


def test_satisfies_the_data_adapter_protocol() -> None:
    assert isinstance(SyntheticAdapter(), DataAdapter)


def test_same_seed_is_byte_for_byte_reproducible() -> None:
    a = SyntheticAdapter(seed=42).get_bars("AAA", _START, _END)
    b = SyntheticAdapter(seed=42).get_bars("AAA", _START, _END)
    assert a == b and len(a) > 0


def test_different_seed_or_symbol_differs() -> None:
    base = SyntheticAdapter(seed=1).get_bars("AAA", _START, _END)
    other_seed = SyntheticAdapter(seed=2).get_bars("AAA", _START, _END)
    other_symbol = SyntheticAdapter(seed=1).get_bars("BBB", _START, _END)
    assert base != other_seed
    assert base != other_symbol


def test_bars_are_valid_and_weekday_only() -> None:
    bars = SyntheticAdapter(seed=7).get_bars("AAA", _START, _END)
    for bar in bars:
        assert bar.ts.tzinfo is not None
        assert bar.ts.weekday() < 5  # no weekends
        assert bar.open > 0 and bar.close > 0
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)
        assert bar.high >= bar.low
        assert bar.volume > 0
    # ascending, unique timestamps
    stamps = [b.ts for b in bars]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_raw_equals_adjusted_no_corporate_actions() -> None:
    # ADR-0021: synthetic GBM has no corporate actions, so the adjusted flag must
    # not change the numbers — the same series drives both feeds.
    adapter = SyntheticAdapter(seed=3)
    adjusted = adapter.get_bars("AAA", _START, _END, adjusted=True)
    raw = adapter.get_bars("AAA", _START, _END, adjusted=False)
    assert adjusted == raw and len(raw) > 0


def test_count_matches_weekdays_in_range() -> None:
    # 2022-01-03 .. 2022-01-07 is Mon..Fri = 5 trading days.
    bars = SyntheticAdapter().get_bars(
        "AAA", datetime(2022, 1, 3, tzinfo=UTC), datetime(2022, 1, 9, tzinfo=UTC)
    )
    assert len(bars) == 5


def test_default_frequency_is_daily_and_unchanged() -> None:
    # The default construction must be byte-identical to an explicit DAILY one, so
    # every existing daily number is untouched by the new frequency plumbing.
    default = SyntheticAdapter(seed=3).get_bars("AAA", _START, _END)
    explicit_daily = SyntheticAdapter(seed=3, frequency=DAILY).get_bars("AAA", _START, _END)
    assert default == explicit_daily
    # Daily bars remain stamped at midnight UTC.
    assert all(b.ts.timetz() == time(0, tzinfo=UTC) for b in default)


class TestIntraday:
    # A single trading day: Monday 2022-01-03.
    _DAY_START = datetime(2022, 1, 3, tzinfo=UTC)
    _DAY_END = datetime(2022, 1, 3, tzinfo=UTC)

    def _hourly(self) -> SyntheticAdapter:
        return SyntheticAdapter(seed=11, frequency=Frequency.parse("1h"))

    def test_hourly_bar_starts_within_one_session(self) -> None:
        bars = self._hourly().get_bars("AAA", self._DAY_START, self._DAY_END)
        stamps = [b.ts for b in bars]
        # Starts step by 1h from 13:30 while strictly before 20:00:
        # 13:30, 14:30, 15:30, 16:30, 17:30, 18:30, 19:30 → 7 bars.
        expected = [self._DAY_START.replace(hour=h, minute=30) for h in range(13, 20)]
        assert stamps == expected
        assert all(b.ts.tzinfo is not None for b in bars)

    def test_thirty_minute_count_per_day(self) -> None:
        bars = SyntheticAdapter(frequency=Frequency.parse("30m")).get_bars(
            "AAA", self._DAY_START, self._DAY_END
        )
        # 390-minute session / 30 = 13 bars, all inside [13:30, 20:00).
        assert len(bars) == 13
        assert all(_SESSION_OPEN <= b.ts.timetz() < _SESSION_CLOSE for b in bars)

    def test_multiple_days_each_get_a_full_session(self) -> None:
        # Mon-Tue 2022-01-03..04 → two sessions of 13 thirty-minute bars each.
        bars = SyntheticAdapter(frequency=Frequency.parse("30m")).get_bars(
            "AAA", datetime(2022, 1, 3, tzinfo=UTC), datetime(2022, 1, 4, tzinfo=UTC)
        )
        assert len(bars) == 26
        per_day: dict[object, int] = {}
        for b in bars:
            per_day[b.ts.date()] = per_day.get(b.ts.date(), 0) + 1
        assert set(per_day.values()) == {13}

    def test_deterministic_across_two_constructions(self) -> None:
        a = self._hourly().get_bars("AAA", self._DAY_START, datetime(2022, 1, 7, tzinfo=UTC))
        b = self._hourly().get_bars("AAA", self._DAY_START, datetime(2022, 1, 7, tzinfo=UTC))
        assert a == b and len(a) > 0

    def test_intraday_bars_are_valid_ohlcv(self) -> None:
        bars = SyntheticAdapter(seed=5, frequency=Frequency.parse("5m")).get_bars(
            "AAA", self._DAY_START, self._DAY_END
        )
        assert len(bars) == 78  # 390 / 5
        for bar in bars:
            assert bar.open > 0 and bar.close > 0
            assert bar.high >= max(bar.open, bar.close)
            assert bar.low <= min(bar.open, bar.close)
            assert bar.high >= bar.low
            assert bar.volume > 0
        stamps = [b.ts for b in bars]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)  # unique, ascending

    def test_intraday_skips_weekends(self) -> None:
        # Fri..Mon 2022-01-07..10: only Fri and Mon are sessions.
        bars = SyntheticAdapter(frequency=Frequency.parse("1h")).get_bars(
            "AAA", datetime(2022, 1, 7, tzinfo=UTC), datetime(2022, 1, 10, tzinfo=UTC)
        )
        weekdays = {b.ts.weekday() for b in bars}
        assert weekdays <= {0, 4}  # Monday and Friday only, no Sat/Sun
        assert bars, "expected bars on the two weekday sessions"
