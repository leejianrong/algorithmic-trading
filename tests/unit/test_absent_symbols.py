"""Absent-symbol tolerance on the backtest fetch path (ADR-0032).

A universe whose members do not all span the requested range must run, reporting
the gaps — a 2000-2020 backtest of today's mega-caps has no META before 2012, and
an early walk-forward fold legitimately predates a whole universe's listings.
Before this, one such symbol raised and killed the entire run.

The counterweight: a universe where *every* symbol is absent is a typo, not a
result, and must fail loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.fake import FakeAdapter
from trading.engine import (
    REASON_FETCH_FAILED,
    REASON_NO_BARS,
    AbsentSymbol,
    EmptyUniverseError,
    Engine,
    load_series,
)
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Bar, Portfolio

_START = datetime(2021, 1, 1, tzinfo=UTC)
_END = datetime(2021, 1, 10, tzinfo=UTC)


def _bars(symbol: str, days: range, price: float = 100.0) -> list[Bar]:
    return [
        Bar(symbol, datetime(2021, 1, d, tzinfo=UTC), price, price, price, price, 1_000)
        for d in days
    ]


class _ExplodingAdapter:
    """Serves one symbol and raises for another — a transport failure, not a gap."""

    def __init__(self, good: list[Bar], bad_symbol: str) -> None:
        self._good = good
        self._bad = bad_symbol

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        if symbol == self._bad:
            raise ConnectionError("upstream reset the connection")
        return [b for b in self._good if b.symbol == symbol]


class TestAbsentSymbolRecord:
    def test_rejects_an_unknown_reason(self) -> None:
        with pytest.raises(ValueError, match="unknown absent reason"):
            AbsentSymbol(symbol="AAA", reason="vibes", detail="because")

    def test_rejects_an_empty_symbol_or_detail(self) -> None:
        with pytest.raises(ValueError, match="non-empty ticker"):
            AbsentSymbol(symbol="", reason=REASON_NO_BARS, detail="d")
        with pytest.raises(ValueError, match="must explain why"):
            AbsentSymbol(symbol="AAA", reason=REASON_NO_BARS, detail="")


class TestLoadSeries:
    def test_symbol_with_bars_is_kept_and_one_without_is_reported(self) -> None:
        adapter = FakeAdapter(_bars("AAA", range(1, 6)))
        series, absent = load_series(adapter, ["AAA", "GHOST"], _START, _END)

        assert list(series) == ["AAA"]
        assert [a.symbol for a in absent] == ["GHOST"]
        assert absent[0].reason == REASON_NO_BARS
        assert "not listed in this window" in absent[0].detail

    def test_a_raising_lookup_is_reported_separately_from_a_gap(self) -> None:
        """ "We could not ask" and "it was not listed" are different facts."""
        adapter = _ExplodingAdapter(_bars("AAA", range(1, 6)), bad_symbol="BOOM")
        series, absent = load_series(adapter, ["AAA", "BOOM", "GHOST"], _START, _END)

        assert list(series) == ["AAA"]
        by_symbol = {a.symbol: a for a in absent}
        assert by_symbol["BOOM"].reason == REASON_FETCH_FAILED
        assert "ConnectionError" in by_symbol["BOOM"].detail
        assert by_symbol["GHOST"].reason == REASON_NO_BARS
        assert by_symbol["BOOM"].reason != by_symbol["GHOST"].reason

    def test_one_failing_symbol_does_not_abort_the_universe(self) -> None:
        adapter = _ExplodingAdapter(_bars("AAA", range(1, 6)), bad_symbol="BOOM")
        series, _absent = load_series(adapter, ["BOOM", "AAA"], _START, _END)
        assert list(series) == ["AAA"]  # the good symbol survives a bad neighbour

    def test_every_requested_symbol_is_accounted_for(self) -> None:
        adapter = _ExplodingAdapter(_bars("AAA", range(1, 6)), bad_symbol="BOOM")
        requested = ["AAA", "BOOM", "GHOST"]
        series, absent = load_series(adapter, requested, _START, _END)
        assert set(series) | {a.symbol for a in absent} == set(requested)
        assert len(series) + len(absent) == len(requested)

    def test_duplicates_are_fetched_once(self) -> None:
        calls: list[str] = []

        class Counting:
            def get_bars(
                self,
                symbol: str,
                start: datetime,
                end: datetime,
                *,
                adjusted: bool = True,
            ) -> list[Bar]:
                calls.append(symbol)
                return _bars(symbol, range(1, 4))

        load_series(Counting(), ["AAA", "AAA", "BBB"], _START, _END)
        assert calls == ["AAA", "BBB"]

    def test_input_order_is_preserved(self) -> None:
        adapter = FakeAdapter(_bars("BBB", range(1, 4)) + _bars("AAA", range(1, 4)))
        series, _ = load_series(adapter, ["BBB", "AAA"], _START, _END)
        assert list(series) == ["BBB", "AAA"]


class TestEngineToleratesPartialUniverse:
    def _engine(self, adapter: object) -> Engine:
        broker = SimulatedBroker(Portfolio(cash=1_000.0))
        return Engine(adapter, broker, Guardrails(RiskConfig()))  # type: ignore[arg-type]

    def test_run_completes_and_reports_the_absent_symbol(self) -> None:
        """The case that motivated this: a late-listing member of a real universe."""
        adapter = FakeAdapter(_bars("AAA", range(1, 6)))
        result = self._engine(adapter).run(
            get_strategy("equal_weight"), ["AAA", "LATER"], _START, _END
        )

        assert result.equity_curve  # the run actually happened
        assert [a.symbol for a in result.absent] == ["LATER"]

    def test_requested_universe_is_preserved_but_traded_set_is_honest(self) -> None:
        adapter = FakeAdapter(_bars("AAA", range(1, 6)))
        result = self._engine(adapter).run(
            get_strategy("equal_weight"), ["AAA", "LATER"], _START, _END
        )

        assert result.symbols == ["AAA", "LATER"]  # what was asked for
        assert result.traded_symbols == ["AAA"]  # what could actually be traded

    def test_a_raising_symbol_does_not_abort_the_run(self) -> None:
        adapter = _ExplodingAdapter(_bars("AAA", range(1, 6)), bad_symbol="BOOM")
        result = self._engine(adapter).run(
            get_strategy("equal_weight"), ["AAA", "BOOM"], _START, _END
        )

        assert result.equity_curve
        assert result.absent[0].reason == REASON_FETCH_FAILED

    def test_a_fully_present_universe_records_nothing_absent(self) -> None:
        """The backward-compatibility guard: unchanged runs stay unchanged."""
        adapter = FakeAdapter(_bars("AAA", range(1, 6)) + _bars("BBB", range(1, 6), 50.0))
        result = self._engine(adapter).run(
            get_strategy("equal_weight"), ["AAA", "BBB"], _START, _END
        )

        assert result.absent == []
        assert result.traded_symbols == result.symbols


class TestEmptyUniverseIsFatal:
    def _engine(self, adapter: object) -> Engine:
        broker = SimulatedBroker(Portfolio(cash=1_000.0))
        return Engine(adapter, broker, Guardrails(RiskConfig()))  # type: ignore[arg-type]

    def test_no_symbol_with_data_raises_rather_than_returning_a_vacuous_result(self) -> None:
        """A mistyped ticker list must fail loudly, not report a flat 0% run."""
        with pytest.raises(EmptyUniverseError, match="no bars for any"):
            self._engine(FakeAdapter([])).run(
                get_strategy("equal_weight"), ["TYPO", "ALSOTYPO"], _START, _END
            )

    def test_the_error_names_every_symbol_it_could_not_find(self) -> None:
        with pytest.raises(EmptyUniverseError) as exc:
            self._engine(FakeAdapter([])).run(
                get_strategy("equal_weight"), ["TYPO", "ALSOTYPO"], _START, _END
            )
        assert "TYPO" in str(exc.value)
        assert "ALSOTYPO" in str(exc.value)
