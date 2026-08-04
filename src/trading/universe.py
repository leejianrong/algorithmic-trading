"""Curated, named stock baskets ("universes") kept in-repo.

A basket is a hand-picked set of tickers with a symbol->sector map, addressable
by name from the CLI (``--symbols @blue20``, ``--sector-map @blue20``). It seeds a
run with a diversified, liquid candidate set without hard-coding a comma list, and
gives the sector-cap guardrail (ADR-0019) a matching, hand-checkable map.

Honesty caveat 1 — tradability is the broker's fact, and now checkable
----------------------------------------------------------------------
The ``blue20`` names below are curated as high-confidence fractionable US
large-caps. That curation is a judgement call, **not an authoritative fact**:
whether a symbol is actually tradable and fractionable is authoritative only via
Alpaca's per-asset ``tradable`` / ``fractionable`` flags. Since ADR-0028 the
:class:`~trading.data.alpaca_client.AlpacaClient` seam exposes ``get_asset``, so
that check is available — but it is *opt-in and never automatic*, because it
needs credentials and a network the offline bench deliberately does without.
Verify before any live use::

    from trading.data.alpaca_client import RealAlpacaClient
    from trading.universe import get_universe, validate_universe

    result = validate_universe(get_universe("blue20"), RealAlpacaClient())
    print("\\n".join(result.report_lines()))  # usable set + every dropped name

Until you have run that against your own account, treat the list as a starting
candidate set: a backtest universe should mirror the broker's tradable +
fractionable set, or paper/live cannot hold what the backtest assumed.

Honesty caveat 2 — survivorship bias (ADR-0027)
-----------------------------------------------
``blue20`` is a list of **today's** mega-caps, picked with full knowledge of who
won. Backtesting it over history is therefore closer to "what if I had known in
2018 which stocks would be giants in 2026" than to a strategy result: the losers,
delistings, and bankruptcies that a real 2018 universe contained are simply
absent, and yfinance supplies no delisted or point-in-time constituent data to
put them back. This bias is **accepted and documented, not fixed** (ADR-0027).
It inflates backtest and sweep/walk-forward numbers on any curated basket, so
read them as an **upper bound** and weight forward paper results — which are
survivorship-free by construction — far more heavily. A real fix needs a
point-in-time, survivorship-bias-free constituent database fed in through
``--source csv``; that is a future slice, not done.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trading.data.alpaca_client import AssetInfo


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
# Alpaca-fractionable large-caps. See the module docstring's two honesty caveats:
# this curation is not broker-verified until you run `validate_universe` against
# your own account (ADR-0028), and it is survivorship-biased by construction
# (ADR-0027).
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


# --- Broker verification (ADR-0028) -------------------------------------------
# Closes honesty caveat 1: a curated basket can now be checked against what the
# venue will actually trade. This module stays free of any runtime dependency on
# `trading.data` — the client is typed structurally below, so `universe.py`
# imports nothing from the Alpaca lane at import time.


class AssetSource(Protocol):
    """The one call universe verification needs from a broker client.

    Structural on purpose: :class:`~trading.data.alpaca_client.AlpacaClient` and
    its fake both satisfy it, and this module never imports either (the
    :class:`~trading.data.alpaca_client.AssetInfo` annotation is
    ``TYPE_CHECKING``-only), so a curated basket keeps working with no broker
    module loaded at all.
    """

    def get_asset(self, symbol: str) -> AssetInfo:
        """Broker-authoritative metadata for ``symbol``."""
        ...


# Why a symbol is not in the usable universe. Plain strings so they survive a
# round trip through a CSV/JSON report unchanged.
REASON_NOT_TRADABLE = "not_tradable"
REASON_NOT_FRACTIONABLE = "not_fractionable"
REASON_UNVERIFIED = "unverified"

DROP_REASONS: frozenset[str] = frozenset(
    {REASON_NOT_TRADABLE, REASON_NOT_FRACTIONABLE, REASON_UNVERIFIED}
)


@dataclass(frozen=True, slots=True)
class DroppedSymbol:
    """One symbol kept out of the usable universe, and why.

    ``reason`` is one of the :data:`DROP_REASONS` codes (machine-readable);
    ``detail`` is the human sentence a report prints. Every exclusion produces one
    of these — nothing is ever filtered silently.
    """

    symbol: str
    reason: str
    detail: str

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("DroppedSymbol.symbol must be a non-empty ticker")
        if self.reason not in DROP_REASONS:
            known = ", ".join(sorted(DROP_REASONS))
            raise ValueError(f"unknown drop reason {self.reason!r}; known reasons: {known}")
        if not self.detail:
            raise ValueError(f"DroppedSymbol.detail must explain why {self.symbol} was dropped")


@dataclass(frozen=True, slots=True)
class UniverseValidation:
    """The outcome of checking a candidate universe against the broker.

    Three disjoint buckets that together cover every requested symbol:

    - :attr:`usable` — verified ``tradable and fractionable``; safe to trade.
    - :attr:`unusable` — the broker answered, and the answer was no.
    - :attr:`unverified` — the lookup itself failed, so we do not know.

    :attr:`unverified` is deliberately *not* merged into :attr:`unusable`: a
    network hiccup must never read as a delisted stock. Both are excluded from
    :attr:`usable` (absent permission is not permission), and both are reported.
    """

    requested: tuple[str, ...]
    usable: tuple[str, ...]
    unusable: tuple[DroppedSymbol, ...]
    unverified: tuple[DroppedSymbol, ...]

    @property
    def dropped(self) -> tuple[DroppedSymbol, ...]:
        """Every excluded symbol — verified-unusable first, then unverified."""
        return self.unusable + self.unverified

    @property
    def is_clean(self) -> bool:
        """True when every requested symbol verified as tradable and fractionable."""
        return not self.unusable and not self.unverified

    def report_lines(self) -> list[str]:
        """A human-readable summary: the counts, then one line per dropped symbol."""
        lines = [
            f"Universe check: {len(self.usable)}/{len(self.requested)} symbols usable "
            f"({len(self.unusable)} unusable, {len(self.unverified)} unverified)",
            f"  usable: {', '.join(self.usable) if self.usable else '(none)'}",
        ]
        lines.extend(
            f"  dropped {drop.symbol} [{drop.reason}]: {drop.detail}" for drop in self.dropped
        )
        if self.unverified:
            lines.append(
                "  NOTE: unverified symbols are unknown, not rejected — re-run the "
                "check before trusting the usable set."
            )
        return lines


def validate_universe(symbols: Iterable[str], client: AssetSource) -> UniverseValidation:
    """Check ``symbols`` against the broker and report what is actually usable.

    A symbol is usable only when the broker says it is both **tradable** and
    **fractionable** — tradable because otherwise no order is accepted at all, and
    fractionable because our sizing layer emits fractional quantities (ADR-0011).
    Anything else is returned in a labelled bucket with a reason; **nothing is
    silently filtered**, which is the whole point of this function (ADR-0028).

    Lookup failures are treated as *unverified*, not as rejections: if
    ``client.get_asset`` raises anything (unknown ticker, auth error, rate limit,
    transport failure), the symbol lands in
    :attr:`UniverseValidation.unverified` carrying the exception text, and is
    excluded from the usable set. That keeps a five-second network blip from
    looking exactly like a delisting, while still refusing to trade a name we
    could not confirm. ``BaseException`` (``KeyboardInterrupt``,
    ``SystemExit``) is never caught.

    Duplicate symbols are collapsed to their first occurrence, and input order is
    preserved throughout, so the result is deterministic and each symbol costs one
    broker call.
    """
    requested: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            requested.append(symbol)

    usable: list[str] = []
    unusable: list[DroppedSymbol] = []
    unverified: list[DroppedSymbol] = []

    for symbol in requested:
        try:
            asset = client.get_asset(symbol)
        except Exception as exc:  # any failure means "we do not know", not "rejected"
            unverified.append(
                DroppedSymbol(
                    symbol=symbol,
                    reason=REASON_UNVERIFIED,
                    detail=f"broker lookup failed ({type(exc).__name__}: {exc})",
                )
            )
            continue
        if not asset.tradable:
            unusable.append(
                DroppedSymbol(
                    symbol=symbol,
                    reason=REASON_NOT_TRADABLE,
                    detail="broker reports the asset is not tradable",
                )
            )
            continue
        if not asset.fractionable:
            unusable.append(
                DroppedSymbol(
                    symbol=symbol,
                    reason=REASON_NOT_FRACTIONABLE,
                    detail=(
                        "broker reports the asset is not fractionable; "
                        "fractional-share sizing (ADR-0011) cannot hold it"
                    ),
                )
            )
            continue
        usable.append(symbol)

    return UniverseValidation(
        requested=tuple(requested),
        usable=tuple(usable),
        unusable=tuple(unusable),
        unverified=tuple(unverified),
    )
