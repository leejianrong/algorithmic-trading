"""Fast unit tests for the CsvAdapter seam and its DataAdapter conformance."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading.data.csv_adapter import CsvAdapter
from trading.interfaces import DataAdapter
from trading.types import Bar

_HEADER = "ts,open,high,low,close,volume\n"


def _write_csv(data_dir: Path, symbol: str, rows: list[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{symbol}.csv").write_text(_HEADER + "".join(row + "\n" for row in rows))


def _range(*, y1: int = 2000, y2: int = 2100) -> tuple[datetime, datetime]:
    return datetime(y1, 1, 1, tzinfo=UTC), datetime(y2, 1, 1, tzinfo=UTC)


def test_satisfies_the_data_adapter_protocol(tmp_path: Path) -> None:
    assert isinstance(CsvAdapter(tmp_path), DataAdapter)


def test_reads_exact_bar_values(tmp_path: Path) -> None:
    _write_csv(tmp_path, "AAPL", ["2024-01-02,10.0,11.5,9.5,11.0,123456"])
    (bar,) = CsvAdapter(tmp_path).get_bars("AAPL", *_range())
    assert bar == Bar(
        symbol="AAPL",
        ts=datetime(2024, 1, 2, tzinfo=UTC),
        open=10.0,
        high=11.5,
        low=9.5,
        close=11.0,
        volume=123456,
    )


def test_returns_bars_sorted_ascending(tmp_path: Path) -> None:
    _write_csv(
        tmp_path,
        "AAPL",
        [
            "2024-01-03,3,3,3,3,10",
            "2024-01-01,1,1,1,1,10",
            "2024-01-02,2,2,2,2,10",
        ],
    )
    bars = CsvAdapter(tmp_path).get_bars("AAPL", *_range())
    assert [b.close for b in bars] == [1.0, 2.0, 3.0]


def test_filters_to_range_inclusive(tmp_path: Path) -> None:
    _write_csv(
        tmp_path,
        "AAPL",
        [
            "2024-01-01,1,1,1,1,10",
            "2024-01-02,2,2,2,2,10",
            "2024-01-03,3,3,3,3,10",
            "2024-01-04,4,4,4,4,10",
        ],
    )
    bars = CsvAdapter(tmp_path).get_bars(
        "AAPL",
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )
    # Bounds are inclusive; rows outside [start, end] are excluded.
    assert [b.ts.day for b in bars] == [2, 3]


def test_uppercases_symbol_for_filename(tmp_path: Path) -> None:
    _write_csv(tmp_path, "AAPL", ["2024-01-02,10,10,10,10,10"])
    assert len(CsvAdapter(tmp_path).get_bars("aapl", *_range())) == 1


def test_non_overlapping_range_is_empty(tmp_path: Path) -> None:
    _write_csv(tmp_path, "AAPL", ["2024-01-02,10,10,10,10,10"])
    bars = CsvAdapter(tmp_path).get_bars(
        "AAPL",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 12, 31, tzinfo=UTC),
    )
    assert bars == []


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="MSFT"):
        CsvAdapter(tmp_path).get_bars("MSFT", *_range())


def test_malformed_price_raises_value_error(tmp_path: Path) -> None:
    _write_csv(tmp_path, "AAPL", ["2024-01-02,not-a-number,10,10,10,10"])
    with pytest.raises(ValueError, match="malformed row"):
        CsvAdapter(tmp_path).get_bars("AAPL", *_range())


def test_wrong_column_count_raises_value_error(tmp_path: Path) -> None:
    _write_csv(tmp_path, "AAPL", ["2024-01-02,10,10,10,10"])  # missing volume
    with pytest.raises(ValueError, match="expected 6 columns"):
        CsvAdapter(tmp_path).get_bars("AAPL", *_range())


def test_bad_header_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "AAPL.csv").write_text("date,o,h,l,c,v\n2024-01-02,10,10,10,10,10\n")
    with pytest.raises(ValueError, match="unexpected header"):
        CsvAdapter(tmp_path).get_bars("AAPL", *_range())


def test_bar_invariant_failure_is_located(tmp_path: Path) -> None:
    # high < low violates a Bar invariant; the error should name the line.
    _write_csv(tmp_path, "AAPL", ["2024-01-02,10,5,9,10,10"])
    with pytest.raises(ValueError, match=r"AAPL\.csv:2"):
        CsvAdapter(tmp_path).get_bars("AAPL", *_range())
