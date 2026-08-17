"""Curated, named stock baskets ("universes") kept in-repo.

A basket is a hand-picked set of tickers with a symbol->sector map, addressable
by name from the CLI (``--symbols @blue20``, ``--sector-map @blue20``). It seeds a
run with a diversified, liquid candidate set without hard-coding a comma list, and
gives the sector-cap guardrail (ADR-0019) a matching, hand-checkable map.

Three baskets ship today: ``blue20`` (20 of today's mega-cap US single stocks),
``core10`` (10 long-lived, broad ETFs across asset classes, built for
long-horizon runs), and ``crypto10`` (10 USD-quoted pairs from Alpaca's crypto
venue, for ``--market crypto``). The map values are *bucket labels*, not
necessarily GICS sectors — see caveat 3.

``crypto10`` is the only basket whose symbols are **pairs** (``BTC/USD``), which
is what the ADR-0057 shape guard reads: passing it under a session market is
refused before any fetch. Its survivorship problem is the worst of the three —
see caveat 4.

Honesty caveat 1 — tradability is the broker's fact, checked once on 2026-08-04
------------------------------------------------------------------------------
The names below are curated as high-confidence fractionable US large-caps and
ETFs. That curation was a judgement call; on **2026-08-04** it was finally checked
against a real Alpaca paper account, and both baskets came back **completely
clean**: ``blue20`` 20/20 and ``core10`` 10/10 usable — every symbol reported
``tradable`` *and* ``fractionable``, with nothing unusable and nothing unverified.

Read that for exactly what it is: **a snapshot against one account at one
moment**, not a permanent property of these lists. Tradability and fractionability
are live broker facts that move — a halt, a delisting, a corporate action, a
change in Alpaca's fractional-share coverage, or a different account tier can all
flip a flag tomorrow. Deliberately *not* cached (ADR-0028), because a stale cache
would restore the false confidence the check exists to remove. So the check is
still what stands between you and a divergence, and it is *opt-in and never
automatic*, because it needs credentials and a network the offline bench
deliberately does without. Re-run it before any live use::

    from trading.data.alpaca_client import RealAlpacaClient
    from trading.universe import get_universe, validate_universe

    result = validate_universe(get_universe("blue20"), RealAlpacaClient())
    print("\\n".join(result.report_lines()))  # usable set + every dropped name

or just ``trading verify-universe --symbols @blue20``. Until you have run it
against *your own* account, treat the list as a starting candidate set: a backtest
universe should mirror the broker's tradable + fractionable set, or paper/live
cannot hold what the backtest assumed.

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

**Partially addressed for the S&P 500 specifically (ADR-0064, KAN-631):**
:mod:`trading.data.sp500_membership` reconstructs point-in-time S&P 500
membership (who was actually in the index on a historical date, back to
1996-01-02) from a committed, free-data fixture. It fixes the *selection* half
of this caveat — no more hindsight-picked constituents — but not the *price*
half: yfinance still has no delisted-name history, so a PIT universe still
silently loses names the free price provider cannot serve. Read that module's
docstring and ADR-0064 for the measured residual before treating a PIT
backtest as unbiased. `blue20` itself is unchanged by this — it remains today's
mega-caps, and this caveat still fully applies to it.

Honesty caveat 3 — ``core10`` reduces that bias substantially, and does not remove it
------------------------------------------------------------------------------------
``core10`` exists because of caveat 2. It holds **broad ETFs**, not single names,
and that is the best mitigation available on a yfinance-only data path: a broad
index or Treasury fund does not go bankrupt, get acquired, or get delisted for
failure the way an individual company does, so the missing-losers hole that makes
``blue20`` unreadable over 20 years mostly closes. A fund's own holdings turn over
inside the wrapper — SPY held the 2000 losers and dropped them, and its NAV wore
the loss — so the ticker's price history is a genuine, point-in-time-honest record
of that exposure. For a long-horizon backtest (say 2000-2020), ``core10`` is a
**substantially** more honest universe than ``blue20``, which is the entire reason
it is here.

It is **not** survivorship-bias-free:

- ETFs do close. Hundreds have been liquidated, and yfinance has no history for
  them, so the fund graveyard is as invisible here as the stock graveyard.
- These ten were picked in 2026, with hindsight, *because* they survived and stayed
  liquid for two decades. A 2000-vintage operator choosing ten funds could have
  picked ones that closed or bled assets. That selection is still hindsight —
  it is just applied to wrappers instead of companies, where the survival rate is
  much higher and the surviving names are much closer to the ones a reasonable
  person would have picked anyway.

So: bias reduced, not removed. Everything in ADR-0027 still applies — read the
numbers as an upper bound and weight forward paper results more heavily.

Honesty caveat 4 — ``crypto10``'s survivorship bias is the worst of the three
----------------------------------------------------------------------------
``crypto10`` (ADR-0058) is the same hindsight problem as ``blue20``, in a market
where the failure rate is far higher and the record far thinner. Three
compounding layers, and they are worth separating:

1. **The tokens are 2026's survivors.** Tens of thousands of tokens have gone to
   zero, been abandoned, or been exit-scammed since 2021. None of them are here.
2. **The venue is a survivor filter of its own.** Alpaca lists 73 crypto assets
   and delists ones that fail, so "what Alpaca will trade today" is already a
   curated set of winners before anyone picks ten from it — a filter ``blue20``
   does not have, since a US exchange lists the losers too until they die.
3. **The history has no graveyard at all.** yfinance at least *has* delisted US
   tickers in principle; Alpaca's crypto tape starts at 2021-01-01 and serves only
   currently-listed pairs, so there is no way to put the dead names back even
   manually.

So a ``crypto10`` backtest is an upper bound on an upper bound. Everything in
ADR-0027 applies with more force, and forward paper results — which are
survivorship-free by construction — should outweigh it by more here than
anywhere else in this bench.

Inception dates: expect a shorter universe in the early years
------------------------------------------------------------
Every ``core10`` name traded by 2004, but not by 2000: EEM starts 2003, GLD 2004,
TLT and IEF 2002, EFA 2001, IWM mid-2000. A run beginning in 2000 therefore has
**partial history** for several symbols, and the early years effectively trade a
smaller universe: 2000-2001 is US equity only — SPY, QQQ, IWM from May 2000, plus
the two sector funds — with no bonds, no gold, and no international exposure
available at all. That is correct behavior, not a bug: the engine can only trade bars
that exist. It does mean early-period results are less diversified than the basket
name suggests, and that a metric such as turnover or average exposure is not
comparable between the first two years and the last ten. Expect it rather than
discover it, and each symbol below carries its inception year in a comment.
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

    ``sectors`` values are **opaque bucket labels**, not a fixed taxonomy: the
    per-sector exposure cap (ADR-0019) groups by string equality and gives each
    distinct value its own budget, so a basket may use GICS-style sectors
    (``blue20``) or asset classes (``core10``: ``"treasuries"``, ``"gold"``).
    """

    name: str
    symbols: tuple[str, ...]
    sectors: Mapping[str, str]


# Curated candidate universes. ``blue20`` — 20 mega-cap, highly liquid US names
# spread across 8 sectors, chosen as high-confidence Alpaca-fractionable
# large-caps. See the module docstring's honesty caveats: this curation is not
# broker-verified until you run `validate_universe` against your own account
# (ADR-0028), and it is survivorship-biased by construction (ADR-0027).
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

# ``core10`` — 10 long-lived, broad ETFs spanning asset classes, for long-horizon
# runs (the 2000-2020 kind) where `blue20`'s hindsight-winner problem makes the
# numbers uninterpretable. See the module docstring's caveat 3: broad funds do not
# fail the way single companies do, so most of the survivorship distortion goes
# away — but these ten were still chosen in hindsight, so the bias is *reduced,
# not removed* (ADR-0027).
#
# The map values here are **asset-class bucket labels, not GICS sectors**
# ("treasuries", "gold"). That is deliberate and needs no code change: the
# per-sector exposure cap (ADR-0019) keys on arbitrary strings and treats each
# distinct value as one generic budget, so `--sector-map @core10
# --max-sector-exposure 0.30` caps each bucket at 30% of equity. Two names share
# the "treasuries" label (TLT + IEF), so they share one budget; every other label
# has a single member, where the bucket cap is effectively a second position cap.
#
# Inception year on every line, because a run starting before it silently yields a
# short series for that symbol (see the docstring's inception-date note). Sources
# are the funds' well-known launch dates; anything we were not confident about was
# left out rather than guessed.
_CORE10_SECTORS: dict[str, str] = {
    "SPY": "us_large",  # SPDR S&P 500 — 1993
    "QQQ": "us_tech",  # Invesco QQQ (Nasdaq-100) — 1999
    "IWM": "us_small",  # iShares Russell 2000 — 2000 (May)
    "EFA": "intl_developed",  # iShares MSCI EAFE — 2001
    "EEM": "intl_emerging",  # iShares MSCI Emerging Markets — 2003
    "TLT": "treasuries",  # iShares 20+ Year Treasury — 2002
    "IEF": "treasuries",  # iShares 7-10 Year Treasury — 2002
    "GLD": "gold",  # SPDR Gold Shares — 2004 (Nov)
    "XLE": "energy",  # Energy Select Sector SPDR — 1998
    "XLF": "financials",  # Financial Select Sector SPDR — 1998
}

# ``crypto10`` — 10 USD-quoted pairs from Alpaca's 73-asset crypto venue, for
# `--market crypto --source alpaca` (ADR-0058). Symbols are in the venue's own
# canonical slash form, which is the only form its **data** API accepts
# (`CryptoBarsRequest` refuses `BTCUSD` with `does not match ^[A-Z]+x?/[A-Z]+$`),
# and which the ADR-0057 shape guard is built to recognise.
#
# Every one of these was checked against a real Alpaca paper account on
# 2026-08-14 and came back `tradable` **and** `fractionable` — the same snapshot
# caveat as caveat 1 applies, and `trading verify-universe --symbols @crypto10`
# re-runs it. Across all 73 crypto assets exactly one is unusable, and the flag
# that fails is not the one you would guess: `SHIB/USDT` is tradable but **not
# fractionable** (its `min_order_size` is 86,880 SHIB). So ADR-0028's two flags do
# mean the same thing on this venue, and they do have work to do.
#
# The inception dates below are the first daily bar Alpaca actually serves, read
# off the venue rather than off the token's launch: its crypto history starts
# 2021-01-01 whatever the coin's real age, and several pairs start much later. A
# run beginning before a pair's first bar trades a smaller universe (exactly the
# `core10` behaviour) — and note SOL is listed from 2021-01-01 but returns 1,634
# bars in a 2,052-day span, i.e. this tape has genuine holes, unlike
# `SyntheticAdapter`, which has none.
#
# Stablecoins (`USDC/USD`, `USDT/USD`, `USDG/USD`) and the gold-backed `PAXG/USD`
# are deliberately excluded: a pegged asset has no trend or relative strength to
# rank, so it would sit in a cross-sectional basket contributing only turnover.
_CRYPTO10_SECTORS: dict[str, str] = {
    "BTC/USD": "store_of_value",  # 2021-01-01
    "ETH/USD": "smart_contract",  # 2021-01-01
    "SOL/USD": "smart_contract",  # 2021-01-01 (gaps: 1,634 bars of 2,052 days)
    "LINK/USD": "infrastructure",  # 2021-01-01
    "LTC/USD": "payments",  # 2021-01-01
    "BCH/USD": "payments",  # 2021-01-01
    "DOGE/USD": "payments",  # 2021-01-01
    "UNI/USD": "defi",  # 2021-01-01
    "AAVE/USD": "defi",  # 2021-07-15
    "AVAX/USD": "smart_contract",  # 2021-11-18
}

BASKETS: dict[str, Basket] = {
    "blue20": Basket(
        name="blue20",
        symbols=tuple(_BLUE20_SECTORS),
        sectors=dict(_BLUE20_SECTORS),
    ),
    "core10": Basket(
        name="core10",
        symbols=tuple(_CORE10_SECTORS),
        sectors=dict(_CORE10_SECTORS),
    ),
    "crypto10": Basket(
        name="crypto10",
        symbols=tuple(_CRYPTO10_SECTORS),
        sectors=dict(_CRYPTO10_SECTORS),
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
