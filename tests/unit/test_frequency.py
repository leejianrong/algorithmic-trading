"""Fast, no-infra unit tests for the bar-frequency abstraction (ADR-0022)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from trading.frequency import (
    DAILY,
    REGULAR_SESSION_MINUTES,
    STANDARD_FREQUENCIES,
    TRADING_DAYS_PER_YEAR,
    Frequency,
)


class TestDaily:
    def test_daily_annualization_is_exactly_252(self) -> None:
        # Must match the metrics basis so daily runs are unchanged by this module.
        assert DAILY.periods_per_year == 252.0
        assert DAILY.delta == timedelta(days=1)
        assert DAILY.label == "1d"

    def test_daily_is_not_intraday(self) -> None:
        assert DAILY.is_intraday is False


class TestParse:
    @pytest.mark.parametrize("label", ["1d", "1h", "30m", "5m", "1m"])
    def test_round_trips_the_label(self, label: str) -> None:
        freq = Frequency.parse(label)
        assert freq.label == label
        assert Frequency.parse(label) == freq  # value equality

    def test_is_case_and_whitespace_insensitive(self) -> None:
        assert Frequency.parse("  1H ") == Frequency.parse("1h")

    def test_unknown_label_errors_clearly(self) -> None:
        with pytest.raises(ValueError, match="unknown frequency '2w'"):
            Frequency.parse("2w")

    def test_error_lists_the_known_frequencies(self) -> None:
        with pytest.raises(ValueError, match="1h"):
            Frequency.parse("nope")


class TestIntradayAnnualization:
    @pytest.mark.parametrize(
        ("label", "expected_ppy"),
        [
            ("1h", 252.0 * 390 / 60),  # 6.5 bars/session → 1638
            ("30m", 252.0 * 13),  # 13 bars/session → 3276
            ("5m", 252.0 * 78),  # 78 bars/session → 19656
            ("1m", 252.0 * 390),  # 390 bars/session → 98280
        ],
    )
    def test_scales_by_bars_per_session(self, label: str, expected_ppy: float) -> None:
        assert Frequency.parse(label).periods_per_year == pytest.approx(expected_ppy)

    def test_intraday_flag(self) -> None:
        for label in ("1h", "30m", "5m", "1m"):
            assert Frequency.parse(label).is_intraday is True

    def test_finer_bars_have_a_larger_factor(self) -> None:
        factors = [Frequency.parse(x).periods_per_year for x in ("1d", "1h", "30m", "5m", "1m")]
        assert factors == sorted(factors)  # strictly increasing granularity


class TestConstruction:
    def test_rejects_non_positive_delta(self) -> None:
        with pytest.raises(ValueError, match="delta must be positive"):
            Frequency("bad", timedelta(0), 252.0)

    def test_rejects_non_positive_periods_per_year(self) -> None:
        with pytest.raises(ValueError, match="periods_per_year must be positive"):
            Frequency("bad", timedelta(hours=1), 0.0)


class TestRegistry:
    def test_standard_set_is_daily_first_then_finer(self) -> None:
        labels = [f.label for f in STANDARD_FREQUENCIES]
        assert labels == ["1d", "1h", "30m", "5m", "1m"]

    def test_module_constants(self) -> None:
        assert TRADING_DAYS_PER_YEAR == 252.0
        assert REGULAR_SESSION_MINUTES == 390.0
