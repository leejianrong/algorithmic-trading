"""Curated, named stock baskets ("universes") kept in-repo.

A basket is a hand-picked set of tickers with a symbol->sector map, addressable
by name from the CLI (``--symbols @blue20``, ``--sector-map @blue20``). It seeds a
run with a diversified, liquid candidate set without hard-coding a comma list, and
gives the sector-cap guardrail (ADR-0019) a matching, hand-checkable map.

Honesty caveat (READ THIS before trusting a basket for live trading)
--------------------------------------------------------------------
The ``blue20`` names below are curated as high-confidence fractionable US
large-caps. That curation is a judgement call, **not an authoritative fact**:
whether a symbol is actually tradable and fractionable is authoritative only via
Alpaca's per-asset ``fractionable`` flag, queried through ``get_asset`` at
connect-time. The :class:`~trading.data.alpaca_client.AlpacaClient` seam does NOT
yet expose ``get_asset`` (that extension is planned, see ADR-0024), so nothing
here has been verified against the broker. A backtest universe should mirror the
broker's tradable + fractionable set so paper/live can actually hold what the
backtest assumed; until the seam lands, this list must be verified against the
broker before any live use, not assumed. Treat it as a starting candidate set.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Basket:
    """A named, curated set of symbols with a symbol->sector map.

    ``symbols`` and the keys of ``sectors`` are kept in sync by construction (see
    :data:`BASKETS`); the module-level registry is the single source of truth.
    """

    name: str
    symbols: tuple[str, ...]
    sectors: Mapping[str, str]


# Curated candidate universes. Seeded with one basket, ``blue20`` — 20 mega-cap,
# highly liquid US names spread across 8 sectors, chosen as high-confidence
# Alpaca-fractionable large-caps. See the module docstring's honesty caveat: this
# curation is NOT broker-verified (no get_asset seam yet, ADR-0024).
_BLUE20_SECTORS: dict[str, str] = {
    # Technology
    "AAPL": "tech",
    "MSFT": "tech",
    "NVDA": "tech",
    # Communication Services
    "GOOGL": "comms",
    "META": "comms",
    # Consumer Discretionary
    "AMZN": "discretionary",
    "TSLA": "discretionary",
    "HD": "discretionary",
    # Financials
    "JPM": "financials",
    "V": "financials",
    "BAC": "financials",
    # Health Care
    "UNH": "health",
    "JNJ": "health",
    "LLY": "health",
    # Consumer Staples
    "PG": "staples",
    "COST": "staples",
    # Energy
    "XOM": "energy",
    "CVX": "energy",
    # Industrials
    "CAT": "industrials",
    "HON": "industrials",
}

BASKETS: dict[str, Basket] = {
    "blue20": Basket(
        name="blue20",
        symbols=tuple(_BLUE20_SECTORS),
        sectors=dict(_BLUE20_SECTORS),
    ),
}


def _resolve(name: str) -> Basket:
    """Look up a basket by name, or raise a KeyError naming the known baskets."""
    try:
        return BASKETS[name]
    except KeyError:
        known = ", ".join(sorted(BASKETS))
        raise KeyError(f"unknown basket {name!r}; known baskets: {known}") from None


def get_universe(name: str) -> list[str]:
    """Return the basket's symbols as a fresh list (KeyError on an unknown name)."""
    return list(_resolve(name).symbols)


def get_sector_map(name: str) -> dict[str, str]:
    """Return the basket's symbol->sector map as a fresh dict (KeyError on a miss)."""
    return dict(_resolve(name).sectors)
