"""Fast, offline guards on the Alpaca split-adjustment check (ADR-0045).

The regression these exist for is real and was reproduced live on 2026-08-09:
Alpaca's ``adjustment=all`` returns AAPL's 2020-08-31 bars with the 4:1 split
**not** backed out, so the *adjusted* series carries a bare -74.15% cliff. That is
exactly the phantom-split hazard ADR-0008 exists to prevent, arriving through
``--source alpaca``.

Everything here runs against :class:`FakeAlpacaClient` with no network, no key,
and no SDK, so this module deliberately does NOT import ``alpaca``. The live
half — that the provider itself is still broken/fixed — is the nightly contract
test in ``tests/integration/test_alpaca_contract.py`` (ADR-0046).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from trading.data.alpaca_adapter import AlpacaAdapter, UnadjustedSplitError
from trading.data.alpaca_client import AlpacaClient, FakeAlpacaClient, SplitEvent
from trading.types import Bar

_START = datetime(2020, 8, 24, tzinfo=UTC)
_END = datetime(2020, 9, 4, tzinfo=UTC)
_EX_DATE = date(2020, 8, 31)

# AAPL's real closes around its 4:1 split, exactly as Alpaca served them on
# 2026-08-09 (`Adjustment.RAW` and `Adjustment.ALL`). The adjusted column is the
# broken one: raw/adjusted is a flat 1.031 (dividends only) straight through a
# 4:1 split, so the split is simply absent from both series.
_RAW = {
    date(2020, 8, 24): 503.43,
    date(2020, 8, 25): 499.30,
    date(2020, 8, 26): 506.09,
    date(2020, 8, 27): 500.04,
    date(2020, 8, 28): 499.23,
    date(2020, 8, 31): 129.04,
    date(2020, 9, 1): 134.18,
    date(2020, 9, 2): 131.40,
    date(2020, 9, 3): 120.88,
    date(2020, 9, 4): 120.96,
}
_BROKEN_ADJUSTED = {
    date(2020, 8, 24): 488.31,
    date(2020, 8, 25): 484.31,
    date(2020, 8, 26): 490.89,
    date(2020, 8, 27): 485.03,
    date(2020, 8, 28): 484.24,
    date(2020, 8, 31): 125.17,
    date(2020, 9, 1): 130.15,
    date(2020, 9, 2): 127.45,
    date(2020, 9, 3): 117.25,
    date(2020, 9, 4): 117.33,
}
# What a *correct* provider returns: the same post-split scale on both sides of
# the ex-date, i.e. every pre-split adjusted close divided by the 4.0 ratio.
_GOOD_ADJUSTED = {
    day: close / 4.0 if day < _EX_DATE else close for day, close in _BROKEN_ADJUSTED.items()
}


def _bars(closes: dict[date, float]) -> list[Bar]:
    return [
        Bar(
            symbol="AAPL",
            ts=datetime(day.year, day.month, day.day, tzinfo=UTC),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1_000,
        )
        for day, close in sorted(closes.items())
    ]


class _SplitAwareClient(FakeAlpacaClient):
    """A fake that serves a *different* series for raw and adjusted.

    :class:`FakeAlpacaClient` stores one series and ignores ``adjusted`` (it has
    no corporate actions to model), which is precisely the distinction under test
    here, so this subclass supplies both columns.
    """

    def __init__(
        self,
        raw: dict[date, float],
        adjusted: dict[date, float],
        *,
        splits: list[SplitEvent] | None = None,
    ) -> None:
        super().__init__({"AAPL": _bars(adjusted)})
        self._raw_bars = _bars(raw)
        self._adjusted_bars = _bars(adjusted)
        self.raw_fetches = 0
        self.split_lookups = 0
        if splits is not None:
            self.set_splits("AAPL", splits)

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        if not adjusted:
            self.raw_fetches += 1
        series = self._adjusted_bars if adjusted else self._raw_bars
        return [b for b in series if start <= b.ts <= end]

    def get_splits(self, symbol: str, start: datetime, end: datetime) -> list[SplitEvent]:
        self.split_lookups += 1
        return super().get_splits(symbol, start, end)


_SPLIT = [SplitEvent(symbol="AAPL", ex_date=_EX_DATE, ratio=4.0)]


def _broken() -> _SplitAwareClient:
    return _SplitAwareClient(_RAW, _BROKEN_ADJUSTED, splits=_SPLIT)


def _correct() -> _SplitAwareClient:
    return _SplitAwareClient(_RAW, _GOOD_ADJUSTED, splits=_SPLIT)


class TestTheRegressionItself:
    """The AAPL 2020-08-31 numbers Alpaca actually served on 2026-08-09."""

    def test_an_unapplied_split_is_refused(self) -> None:
        adapter = AlpacaAdapter(_broken())
        with pytest.raises(UnadjustedSplitError) as excinfo:
            adapter.get_bars("AAPL", _START, _END, adjusted=True)
        message = str(excinfo.value)
        assert "AAPL" in message
        assert "2020-08-31" in message
        assert "ADR-0008" in message
        # Actionable, per ADR-0034's precedent: name the way out.
        assert "--source yfinance" in message
        assert "verify_adjustments=False" in message

    def test_the_fixture_really_carries_the_phantom_crash(self) -> None:
        # Guard on the guard: if the fixture stopped containing a split cliff the
        # test above would pass for the wrong reason.
        closes = [c for _, c in sorted(_BROKEN_ADJUSTED.items())]
        worst = min(b / a - 1 for a, b in pairwise(closes))
        assert worst < -0.70, f"fixture no longer carries the phantom crash ({worst:.2%})"

    def test_a_correctly_adjusted_series_passes(self) -> None:
        # Self-healing: the day Alpaca applies the split again, nothing refuses.
        adapter = AlpacaAdapter(_correct())
        bars = adapter.get_bars("AAPL", _START, _END, adjusted=True)
        assert len(bars) == len(_GOOD_ADJUSTED)
        closes = [b.close for b in bars]
        worst = min(b / a - 1 for a, b in pairwise(closes))
        assert worst > -0.10


class TestTheCheckIsScoped:
    """What the check must never touch."""

    def test_raw_fetches_are_never_verified(self) -> None:
        # ADR-0021: the paper/live feed asks for RAW quotes, where an unapplied
        # split is not a defect at all — raw is *supposed* to carry the cliff.
        # This is the property Monday's live run depends on.
        client = _broken()
        bars = client.get_daily_bars("AAPL", _START, _END, adjusted=False)
        client.raw_fetches = 0

        adapter = AlpacaAdapter(client)
        assert adapter.get_bars("AAPL", _START, _END, adjusted=False) == bars
        assert client.split_lookups == 0, "a raw fetch must not cost a corporate-actions call"
        assert client.raw_fetches == 1, "a raw fetch must not cost a second bars call"

    def test_a_window_with_no_split_costs_one_lookup_and_no_raw_fetch(self) -> None:
        client = _SplitAwareClient(_RAW, _BROKEN_ADJUSTED, splits=[])
        adapter = AlpacaAdapter(client)
        adapter.get_bars("AAPL", _START, _END, adjusted=True)
        assert client.split_lookups == 1
        assert client.raw_fetches == 0, "no split in range -> no raw cross-check needed"

    def test_a_split_outside_the_returned_series_is_not_checked(self) -> None:
        # Only a split with bars on BOTH sides inside the returned window can put
        # a cliff *in* the series; one at the very edge cannot.
        client = _broken()
        adapter = AlpacaAdapter(client)
        after_only = datetime(2020, 8, 31, tzinfo=UTC)
        assert adapter.get_bars("AAPL", after_only, _END, adjusted=True)

    def test_the_lookup_is_memoized_per_symbol_and_window(self) -> None:
        client = _SplitAwareClient(_RAW, _BROKEN_ADJUSTED, splits=[])
        adapter = AlpacaAdapter(client)
        for _ in range(3):
            adapter.get_bars("AAPL", _START, _END, adjusted=True)
        assert client.split_lookups == 1

    def test_the_escape_hatch_is_a_constructor_parameter(self) -> None:
        client = _broken()
        adapter = AlpacaAdapter(client, verify_adjustments=False)
        assert adapter.get_bars("AAPL", _START, _END, adjusted=True)
        assert client.split_lookups == 0


class TestWeCouldNotAsk:
    """A failed lookup is not evidence the data is bad (ADR-0028's third bucket)."""

    def test_a_failed_split_lookup_warns_and_lets_the_bars_through(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _broken()
        client.set_splits_failure("AAPL", "corporate actions unavailable")
        adapter = AlpacaAdapter(client)
        with caplog.at_level(logging.WARNING, logger="trading.data.alpaca_adapter"):
            assert adapter.get_bars("AAPL", _START, _END, adjusted=True)
        assert any(
            "could not verify" in record.message.lower() and "AAPL" in record.message
            for record in caplog.records
        ), caplog.text

    def test_a_failed_raw_cross_check_warns_and_lets_the_bars_through(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _NoRaw(_SplitAwareClient):
            def get_daily_bars(
                self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
            ) -> list[Bar]:
                if not adjusted:
                    raise RuntimeError("raw tape unavailable")
                return super().get_daily_bars(symbol, start, end, adjusted=adjusted)

        adapter = AlpacaAdapter(_NoRaw(_RAW, _BROKEN_ADJUSTED, splits=_SPLIT))
        with caplog.at_level(logging.WARNING, logger="trading.data.alpaca_adapter"):
            assert adapter.get_bars("AAPL", _START, _END, adjusted=True)
        assert any("could not verify" in record.message.lower() for record in caplog.records)


class TestTheArithmeticIsExact:
    """The detector cancels the stock's own move, so it is not a shape heuristic."""

    @pytest.mark.parametrize("move", [-0.30, -0.05, 0.0, 0.05, 0.30])
    def test_a_big_move_on_the_ex_date_does_not_fake_an_unapplied_split(self, move: float) -> None:
        # A correctly adjusted series stays correct however violently the stock
        # moved that day: raw and adjusted move together, so raw/adjusted is
        # unchanged by the move and only the split factor survives.
        adjusted = dict(_GOOD_ADJUSTED)
        raw = dict(_RAW)
        for day in (_EX_DATE,):
            adjusted[day] *= 1 + move
            raw[day] *= 1 + move
        adapter = AlpacaAdapter(_SplitAwareClient(raw, adjusted, splits=_SPLIT))
        assert adapter.get_bars("AAPL", _START, _END, adjusted=True)

    @pytest.mark.parametrize("move", [-0.30, 0.0, 0.30])
    def test_a_big_move_does_not_hide_an_unapplied_split(self, move: float) -> None:
        adjusted = dict(_BROKEN_ADJUSTED)
        raw = dict(_RAW)
        for day in (_EX_DATE,):
            adjusted[day] *= 1 + move
            raw[day] *= 1 + move
        adapter = AlpacaAdapter(_SplitAwareClient(raw, adjusted, splits=_SPLIT))
        with pytest.raises(UnadjustedSplitError):
            adapter.get_bars("AAPL", _START, _END, adjusted=True)

    def test_a_reverse_split_is_checked_the_same_way(self) -> None:
        # 1:10 reverse: ratio 0.1. Applied -> raw/adjusted drops by 10x across the
        # ex-date; unapplied -> it does not move.
        raw = {day: close for day, close in _RAW.items()}
        broken = {day: close / 1.031 for day, close in raw.items()}
        client = _SplitAwareClient(
            raw, broken, splits=[SplitEvent(symbol="AAPL", ex_date=_EX_DATE, ratio=0.1)]
        )
        with pytest.raises(UnadjustedSplitError):
            AlpacaAdapter(client).get_bars("AAPL", _START, _END, adjusted=True)


class TestTheSeam:
    """``get_splits`` is the seam's seventh call (ADR-0017's anticipated widening)."""

    def test_the_fake_satisfies_the_widened_protocol(self) -> None:
        assert isinstance(FakeAlpacaClient(), AlpacaClient)

    def test_the_fake_reports_no_splits_by_default(self) -> None:
        assert FakeAlpacaClient().get_splits("AAPL", _START, _END) == []

    def test_scripted_splits_are_filtered_to_the_window(self) -> None:
        client = FakeAlpacaClient()
        client.set_splits(
            "AAPL",
            [
                SplitEvent(symbol="AAPL", ex_date=date(2019, 1, 2), ratio=2.0),
                SplitEvent(symbol="AAPL", ex_date=_EX_DATE, ratio=4.0),
            ],
        )
        assert [s.ex_date for s in client.get_splits("AAPL", _START, _END)] == [_EX_DATE]

    def test_a_split_event_rejects_a_nonsense_ratio(self) -> None:
        with pytest.raises(ValueError):
            SplitEvent(symbol="AAPL", ex_date=_EX_DATE, ratio=0.0)

    def test_intraday_adjusted_fetches_are_verified_too(self) -> None:
        class _IntradayClient(_SplitAwareClient):
            def get_bars(
                self,
                symbol: str,
                start: datetime,
                end: datetime,
                *,
                adjusted: bool = True,
                interval: timedelta = timedelta(days=1),
            ) -> list[Bar]:
                return self.get_daily_bars(symbol, start, end, adjusted=adjusted)

        adapter = AlpacaAdapter(
            _IntradayClient(_RAW, _BROKEN_ADJUSTED, splits=_SPLIT), interval=timedelta(hours=1)
        )
        with pytest.raises(UnadjustedSplitError):
            adapter.get_bars("AAPL", _START, _END, adjusted=True)
