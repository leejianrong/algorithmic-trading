"""ADR-0080: share-class ticker translation at the yfinance boundary (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yfinance
from yfinance.exceptions import YFPricesMissingError

from trading.data.yfinance_adapter import YFinanceAdapter, _default_fetch, to_yahoo_symbol
from trading.engine import load_series

_COLS = ["open", "high", "low", "close", "volume"]


def _range() -> tuple[datetime, datetime]:
    return datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [10.0], "high": [10.5], "low": [9.5], "close": [10.2], "volume": [1000]},
        index=pd.Index(pd.to_datetime(["2024-01-02"]), name="ts"),
    )


@pytest.mark.parametrize(
    ("typed", "sent"),
    [
        ("BRK.B", "BRK-B"),
        ("BF.B", "BF-B"),
        ("BRK-B", "BRK-B"),
        ("AAPL", "AAPL"),
        ("VOD.L", "VOD.L"),
        ("0700.HK", "0700.HK"),
        ("BTC/USD", "BTC/USD"),
        ("brk.b", "brk.b"),
    ],
)
def test_translation_is_narrow(typed: str, sent: str) -> None:
    assert to_yahoo_symbol(typed) == sent


def test_fetcher_sees_yahoo_name_but_user_keeps_theirs(tmp_path: Path) -> None:
    seen: list[str] = []

    def spy(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        seen.append(symbol)
        return _frame()

    start, end = _range()
    bars = YFinanceAdapter(tmp_path, spy).get_bars("BRK.B", start, end)

    assert seen == ["BRK-B"]
    assert {b.symbol for b in bars} == {"BRK.B"}
    # cache keyed by the typed name, so existing cache files stay valid
    assert (tmp_path / "BRK.B_20240101_20240104_adj.csv").exists()


def test_absence_is_reported_under_the_typed_name(tmp_path: Path) -> None:
    def absent(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        frame = pd.DataFrame(columns=_COLS)
        frame.index.name = "ts"
        return frame

    start, end = _range()
    _, absences = load_series(YFinanceAdapter(tmp_path, absent), ["BRK.B"], start, end)
    assert [a.symbol for a in absences] == ["BRK.B"]


def test_plain_404_is_still_read_as_absence_adr_0080(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 for a listed name is indistinguishable offline from a delisting."""

    def empty_download(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    class _Missing:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
            raise YFPricesMissingError(self.symbol, "(1d)")

    monkeypatch.setattr(yfinance, "download", empty_download)
    monkeypatch.setattr(yfinance, "Ticker", _Missing)
    assert _default_fetch("BK", *_range()).empty
