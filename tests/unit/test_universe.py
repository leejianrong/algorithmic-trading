"""Unit tests for the curated stock-basket registry (universe.py)."""

from __future__ import annotations

import pytest

from trading.universe import BASKETS, get_sector_map, get_universe


def test_blue20_has_exactly_twenty_symbols() -> None:
    symbols = get_universe("blue20")
    assert len(symbols) == 20
    # No accidental duplicates.
    assert len(set(symbols)) == 20


def test_blue20_sector_map_covers_all_and_only_the_symbols() -> None:
    symbols = get_universe("blue20")
    sectors = get_sector_map("blue20")
    assert set(sectors) == set(symbols)
    # The curated spread is 8 named sectors.
    assert set(sectors.values()) == {
        "tech",
        "comms",
        "discretionary",
        "financials",
        "health",
        "staples",
        "energy",
        "industrials",
    }


def test_get_universe_round_trips_registry() -> None:
    assert get_universe("blue20") == list(BASKETS["blue20"].symbols)


def test_get_sector_map_round_trips_registry() -> None:
    assert get_sector_map("blue20") == dict(BASKETS["blue20"].sectors)


def test_getters_return_fresh_copies() -> None:
    # Mutating a returned collection must not corrupt the registry.
    get_universe("blue20").append("BOGUS")
    get_sector_map("blue20")["BOGUS"] = "junk"
    assert "BOGUS" not in get_universe("blue20")
    assert "BOGUS" not in get_sector_map("blue20")


def test_unknown_universe_raises_keyerror_naming_known_baskets() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_universe("nope")
    assert "blue20" in str(excinfo.value)


def test_unknown_sector_map_raises_keyerror_naming_known_baskets() -> None:
    with pytest.raises(KeyError) as excinfo:
        get_sector_map("nope")
    assert "blue20" in str(excinfo.value)
