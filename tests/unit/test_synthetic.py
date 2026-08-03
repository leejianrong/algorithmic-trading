"""Fast unit tests for the synthetic data generator (ADR-0012)."""

from __future__ import annotations

from datetime import UTC, datetime

from trading.data.synthetic import SyntheticAdapter
from trading.interfaces import DataAdapter

_START = datetime(2022, 1, 1, tzinfo=UTC)
_END = datetime(2022, 3, 31, tzinfo=UTC)


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


def test_count_matches_weekdays_in_range() -> None:
    # 2022-01-03 .. 2022-01-07 is Mon..Fri = 5 trading days.
    bars = SyntheticAdapter().get_bars(
        "AAA", datetime(2022, 1, 3, tzinfo=UTC), datetime(2022, 1, 9, tzinfo=UTC)
    )
    assert len(bars) == 5
