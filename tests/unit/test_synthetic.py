"""Fast unit tests for the synthetic data generator (ADR-0012, ADR-0022, ADR-0030)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time
from itertools import pairwise
from statistics import fmean, stdev

import pytest

from trading.data.synthetic import EPOCH, SyntheticAdapter, SyntheticParams
from trading.frequency import DAILY, Frequency
from trading.interfaces import DataAdapter
from trading.types import Bar

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


def test_naive_bounds_are_rejected_with_a_clear_message() -> None:
    # Clipping to the epoch (ADR-0030) compares the bounds, so a naive datetime —
    # which the old generator silently tolerated — must fail by name, not as a bare
    # TypeError out of the comparison.
    with pytest.raises(ValueError, match="timezone-aware"):
        SyntheticAdapter().get_bars("AAA", datetime(2022, 1, 3), datetime(2022, 1, 7))


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


# --- range independence: a range is a SLICE of one canonical series (ADR-0030) --
#
# The load-bearing invariant of any data adapter: the bar for a symbol at a
# timestamp is a property of that symbol and that timestamp, never of the window
# the caller happened to ask for. The generator used to reseed per call and walk
# forward from the requested ``start``, so every range replayed the same path from
# its own first bar — two different spans came back byte-identical, and a
# sub-range disagreed with its parent on every shared timestamp.

_PARENT_START = datetime(2018, 1, 1, tzinfo=UTC)
_SUB_START = datetime(2019, 10, 3, tzinfo=UTC)
_SUB_END = datetime(2021, 7, 2, tzinfo=UTC)


def _at_or_after(bars: list[Bar], start: datetime) -> list[Bar]:
    """The tail of ``bars`` from ``start`` on — what a sub-range must reproduce."""
    return [b for b in bars if b.ts >= start]


def _values(bars: list[Bar]) -> list[tuple[float, float, float, float, int]]:
    """Bar values without their timestamps, to compare two *different* spans."""
    return [(b.open, b.high, b.low, b.close, b.volume) for b in bars]


class TestRangeIndependence:
    """Overlapping requests must agree bar-for-bar on the timestamps they share."""

    def test_sub_range_equals_the_tail_of_the_parent_range(self) -> None:
        adapter = SyntheticAdapter(seed=5)
        parent = adapter.get_bars("AAA", _PARENT_START, _SUB_END)
        sub = adapter.get_bars("AAA", _SUB_START, _SUB_END)
        assert sub, "expected bars in the sub-range"
        assert sub == _at_or_after(parent, _SUB_START)

    def test_interior_sub_range_equals_the_parent_slice(self) -> None:
        # A window carved out of the middle, not sharing either endpoint.
        adapter = SyntheticAdapter(seed=5)
        parent = adapter.get_bars("AAA", _PARENT_START, datetime(2022, 12, 31, tzinfo=UTC))
        inner_start = datetime(2020, 5, 4, tzinfo=UTC)
        inner_end = datetime(2020, 8, 14, tzinfo=UTC)
        inner = adapter.get_bars("AAA", inner_start, inner_end)
        assert inner, "expected bars in the interior window"
        assert inner == [b for b in parent if inner_start <= b.ts <= inner_end]

    def test_two_fresh_adapters_agree_across_different_ranges(self) -> None:
        # Range independence is a property of (symbol, seed), not of one instance:
        # a second construction must place the same bars at the same timestamps.
        parent = SyntheticAdapter(seed=5).get_bars("AAA", _PARENT_START, _SUB_END)
        sub = SyntheticAdapter(seed=5).get_bars("AAA", _SUB_START, _SUB_END)
        assert sub == _at_or_after(parent, _SUB_START)

    def test_two_different_spans_are_not_the_same_path(self) -> None:
        # The old behaviour's tell: seeding per call made every span replay the
        # same path from its own first bar, so two disjoint spans came back with
        # identical values position for position.
        adapter = SyntheticAdapter(seed=5)
        first = adapter.get_bars("AAA", _PARENT_START, datetime(2019, 10, 2, tzinfo=UTC))
        second = adapter.get_bars("AAA", _SUB_START, _SUB_END)
        shared = min(len(first), len(second))
        assert shared > 100, "expected two substantial spans"
        assert _values(first[:shared]) != _values(second[:shared])

    def test_multi_symbol_ranges_are_each_self_consistent(self) -> None:
        adapter = SyntheticAdapter(seed=9)
        for symbol in ("AAA", "BBB", "MSFT"):
            parent = adapter.get_bars(symbol, _PARENT_START, _SUB_END)
            sub = adapter.get_bars(symbol, _SUB_START, _SUB_END)
            assert sub == _at_or_after(parent, _SUB_START), symbol

    def test_intraday_sub_range_equals_the_parent_slice(self) -> None:
        # ADR-0022: the invariant is not daily-only. Mon 2022-03-07 .. Fri 03-18,
        # with the sub-range starting on the second week's Monday.
        adapter = SyntheticAdapter(seed=3, frequency=Frequency.parse("1h"))
        parent_start = datetime(2022, 3, 7, tzinfo=UTC)
        sub_start = datetime(2022, 3, 14, tzinfo=UTC)
        end = datetime(2022, 3, 18, tzinfo=UTC)
        parent = adapter.get_bars("AAA", parent_start, end)
        sub = adapter.get_bars("AAA", sub_start, end)
        assert sub, "expected intraday bars in the sub-range"
        assert sub == _at_or_after(parent, sub_start)

    def test_intraday_single_session_equals_the_parent_slice(self) -> None:
        adapter = SyntheticAdapter(seed=3, frequency=Frequency.parse("30m"))
        parent = adapter.get_bars(
            "AAA", datetime(2022, 3, 7, tzinfo=UTC), datetime(2022, 3, 11, tzinfo=UTC)
        )
        day = datetime(2022, 3, 9, tzinfo=UTC)
        one_session = adapter.get_bars("AAA", day, day)
        assert len(one_session) == 13
        assert one_session == [b for b in parent if b.ts.date() == day.date()]

    def test_intraday_sessions_close_on_the_daily_bar(self) -> None:
        # The daily walk is the backbone at every cadence (ADR-0030): an intraday
        # session is a bridge onto its daily close, so the two frequencies agree at
        # the session close instead of being two unrelated walks.
        start = datetime(2022, 3, 7, tzinfo=UTC)
        end = datetime(2022, 3, 11, tzinfo=UTC)
        daily = SyntheticAdapter(seed=3).get_bars("AAA", start, end)
        hourly = SyntheticAdapter(seed=3, frequency=Frequency.parse("1h")).get_bars(
            "AAA", start, end
        )
        assert len(daily) == 5
        last_of_session = {b.ts.date(): b.close for b in hourly}  # dict keeps the last
        assert last_of_session == {b.ts.date(): b.close for b in daily}

    def test_bars_at_the_epoch_are_the_start_of_the_series(self) -> None:
        # The epoch itself (a Monday) is bar 0, and its open sits near the symbol's
        # base level rather than being carried in from an earlier bar.
        bars = SyntheticAdapter(seed=7).get_bars("AAA", EPOCH, EPOCH)
        assert [b.ts for b in bars] == [EPOCH]
        assert 50.0 < bars[0].open < 150.0

    def test_request_before_the_epoch_is_clipped_not_reanchored(self) -> None:
        # The paper feed polls from ``datetime.min`` (recent_window._FAR_PAST), so a
        # far-past request must clip to the canonical epoch and still line up with a
        # normal range instead of re-anchoring the walk to year 1.
        adapter = SyntheticAdapter(seed=5)
        far_past = adapter.get_bars("AAA", datetime.min.replace(tzinfo=UTC), _SUB_END)
        assert far_past, "expected bars from a far-past request"
        assert far_past[0].ts >= EPOCH
        assert _at_or_after(far_past, _SUB_START) == adapter.get_bars("AAA", _SUB_START, _SUB_END)


# --- the positional draw itself (ADR-0030) ------------------------------------
#
# Range independence rests on the counter-based draw: a ``blake2b`` digest of
# ``(symbol, seed, stream, index)`` turned into normals by Box-Muller. Two things
# can rot silently there, so both are pinned. Neither test asserts anything about
# *which* numbers are "right" — only that they are the same everywhere, and that
# they are actually normal.


class TestPositionalDraws:
    """Guards on the hand-rolled draw: byte stability, and a real distribution."""

    def test_exact_values_are_pinned_across_platforms_and_versions(self) -> None:
        # ``blake2b`` is fixed by its specification: the same key gives the same
        # bytes on every platform, Python version, and process — which is exactly
        # where the builtin ``hash()`` cannot be trusted, since it is salted per
        # process (and pytest runs with the default random PYTHONHASHSEED, so this
        # test re-proves that on every run). These two bars are the whole chain:
        # key -> digest -> Box-Muller -> the cumulative walk -> OHLCV rounding. If
        # they move, the generator changed — that may be fine, but it must be a
        # decision, not a surprise.
        adapter = SyntheticAdapter(seed=7)
        assert adapter.get_bars("AAA", EPOCH, EPOCH) == [
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
        day = datetime(2022, 6, 15, tzinfo=UTC)
        assert adapter.get_bars("AAA", day, day) == [
            Bar(
                symbol="AAA",
                ts=day,
                open=632.5455,
                high=635.5406,
                low=621.5376,
                close=623.719,
                volume=1_191_733,
            )
        ]

    def test_log_returns_are_standard_normal_once_standardized(self) -> None:
        # A transposed Box-Muller term, a missing sqrt, or a mis-scaled sigma would
        # sail past every range-consistency test while quietly skewing every price.
        # Standardizing by the *intended* per-bar drift and vol (8%/yr and 20%/yr
        # over 252 sessions) checks the transform and the scaling together.
        params = SyntheticParams(annual_drift=0.08, annual_vol=0.20)
        bars = SyntheticAdapter(seed=1, params=params).get_bars(
            "AAA", datetime(2000, 1, 3, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)
        )
        assert len(bars) > 5_000, "want a few thousand draws for a stable estimate"
        mu = 0.08 / 252.0
        sigma = 0.20 / math.sqrt(252.0)
        standardized = [
            (math.log(later.close / earlier.close) - mu) / sigma
            for earlier, later in pairwise(bars)
        ]
        assert abs(fmean(standardized)) < 0.05
        assert abs(stdev(standardized) - 1.0) < 0.05
        assert any(z > 0 for z in standardized) and any(z < 0 for z in standardized)
        # Nothing degenerate: a constant or clipped stream would collapse the spread.
        assert max(standardized) > 2.0 and min(standardized) < -2.0
