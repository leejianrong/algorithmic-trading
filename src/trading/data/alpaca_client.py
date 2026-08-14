"""The Alpaca client seam: a thin protocol, our own DTOs, a fake, and a real wrapper.

Everything the coming Alpaca data adapter and paper broker need from Alpaca goes
through one small :class:`AlpacaClient` protocol (ADR-0017). Its return types are
*our* value types -- :class:`~trading.types.Bar` and the frozen DTOs below -- so
nothing downstream ever imports an ``alpaca-py`` SDK type. That keeps the SDK a
lazy, optional dependency (ADR-0018): :class:`FakeAlpacaClient` drives every
offline test with no network, no key, no wall clock, and no RNG, while
:class:`RealAlpacaClient` wraps the SDK behind a guarded import that only bites
when someone actually trades live.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from trading.types import SHARE_EPS, Bar, Side

# --- DTOs: our decoupled view of the SDK's responses --------------------------
# Small, frozen, and free of any SDK import so the whole bench can pass these
# around without ever depending on alpaca-py (ADR-0017).


@dataclass(frozen=True, slots=True)
class AlpacaOrder:
    """A submitted order and its current fill state, in our terms.

    ``status`` mirrors Alpaca's order lifecycle as a plain string (e.g. ``"new"``
    while working, ``"filled"`` once complete, ``"rejected"`` if refused).
    ``filled_avg_price`` is ``None`` until at least one share fills.
    """

    id: str
    symbol: str
    qty: float
    side: Side
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """A point-in-time view of the account: settled ``cash`` and total ``equity``."""

    cash: float
    equity: float


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """One open position: (fractional) ``qty`` held and its average entry price."""

    symbol: str
    qty: float
    avg_price: float


@dataclass(frozen=True, slots=True)
class AssetInfo:
    """Per-asset metadata the broker owns: can we trade it, and in fractions?

    This is the authoritative answer to the question a curated universe can only
    guess at (ADR-0024, ADR-0028): ``tradable`` is whether Alpaca will accept an
    order in the name at all, and ``fractionable`` is whether it accepts the
    fractional quantities our sizing layer produces (ADR-0011). A backtest
    universe should mirror ``tradable and fractionable``, or paper/live cannot
    hold what the backtest assumed.

    ``exchange`` and ``name`` are descriptive only (useful when reporting a drop
    to a human) and default to empty when the SDK omits them; ``shortable`` is
    recorded for completeness and is unused by this long-or-flat bench (ADR-0011).
    Values are reported exactly as the broker gives them — no field is "fixed up"
    into a more usable-looking combination.

    ``min_order_size`` is the smallest quantity the venue publishes for the asset,
    ``None`` when it publishes none (every US equity). It is **recorded, not
    enforced** (ADR-0058), and the reason is a measurement rather than a
    preference: on 2026-08-14 the paper venue refused a ``BTC/USD`` order of
    ``0.000155`` (~$9.73) with ``403``/``40310000`` *"cost basis must be >= minimal
    amount of order 10"* and accepted ``0.00016`` (~$10.05), while the published
    ``min_order_size`` for the same asset was ``1.5739e-05`` (~$0.99). The binding
    floor is therefore a **$10 notional** the metadata does not carry, so a
    client-side gate built on this number would pass orders the venue then
    refuses — a false negative dressed as a safety check. The venue's own refusal
    already reaches ``rejections`` verbatim through
    :func:`_classify_order_error` (ADR-0041), which is the legible answer.
    """

    symbol: str
    tradable: bool
    fractionable: bool
    exchange: str = ""
    name: str = ""
    shortable: bool = False
    min_order_size: float | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("AssetInfo.symbol must be a non-empty ticker")
        if self.symbol.strip() != self.symbol or " " in self.symbol:
            raise ValueError(f"AssetInfo.symbol must not contain whitespace, got {self.symbol!r}")


@dataclass(frozen=True, slots=True)
class SplitEvent:
    """One stock split, as the broker's corporate-actions record describes it.

    ``ratio`` is ``new_rate / old_rate``: ``4.0`` for a 4-for-1 forward split
    (one old share becomes four), ``0.1`` for a 1-for-10 reverse split. It is the
    factor a *correct* adjusted series divides every pre-``ex_date`` price by, and
    therefore exactly the factor :mod:`trading.data.alpaca_adapter` checks for
    (ADR-0045).

    ``ex_date`` is the first session that trades at the post-split price, which is
    the boundary the check straddles — not ``payable_date`` or ``record_date``,
    which say nothing about which bar carries the new price level.
    """

    symbol: str
    ex_date: date
    ratio: float

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("SplitEvent.symbol must be a non-empty ticker")
        if self.ratio <= 0.0:
            raise ValueError(f"SplitEvent.ratio must be positive, got {self.ratio}")


# Order lifecycle statuses we use. Kept as strings (not an enum) so a real
# Alpaca status string round-trips unchanged through :class:`AlpacaOrder`. The
# literals below are alpaca-py's ``OrderStatus`` *values*, verified against the
# installed SDK (0.43.5) rather than assumed -- see ADR-0033.
STATUS_NEW = "new"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_CANCELED = "canceled"
STATUS_EXPIRED = "expired"
STATUS_REPLACED = "replaced"

# Statuses from which an order will never fill any further. Alpaca's other 13
# statuses (``accepted``, ``new``, ``partially_filled``, ``done_for_day``,
# ``held``, ``pending_*``, ...) are *working* states: the order may still fill,
# so a poll must keep waiting rather than give up (ADR-0020, ADR-0033).
#
# ``filled`` and ``rejected`` are terminal too, but the broker handles each
# specially (emit a fill / record a rejection), so they are named separately.
TERMINAL_UNFILLED_STATUSES = frozenset({STATUS_CANCELED, STATUS_EXPIRED, STATUS_REPLACED})
TERMINAL_STATUSES = frozenset({STATUS_FILLED, STATUS_REJECTED}) | TERMINAL_UNFILLED_STATUSES

# Exchange string :class:`FakeAlpacaClient` stamps on the assets it invents. It is
# a placeholder, not a claim about where a symbol really lists.
_FAKE_EXCHANGE = "FAKE"


# --- Asset classes: one client serves one venue (ADR-0058) --------------------
# Alpaca's crypto and equity tapes are *different services*, not a parameter on
# one: crypto bars come from ``CryptoHistoricalDataClient.get_crypto_bars`` and
# the stock client answers a slash symbol with ``APIError: invalid symbol:
# BTC/USD`` (measured 2026-08-14). So the asset class is a **construction**
# property of the client, exactly as ``feed`` is (ADR-0034) and the interval is of
# an adapter (ADR-0022): one client serves one venue, and the run's ``--market``
# picks it. Plain strings rather than an enum, matching ``feed``.
ASSET_CLASS_US_EQUITY = "us_equity"
ASSET_CLASS_CRYPTO = "crypto"
ASSET_CLASSES: frozenset[str] = frozenset({ASSET_CLASS_US_EQUITY, ASSET_CLASS_CRYPTO})

# The order duration each venue accepts. Not a style choice — measured against the
# paper venue on 2026-08-14: a crypto market order with ``TimeInForce.DAY`` is
# refused ``422``/``42210000`` *"invalid crypto time_in_force"*, and the same order
# with ``GTC`` is accepted and fills. Because that refusal is a 4xx carrying an
# Alpaca error code, ADR-0041's classifier would have turned it into a perfectly
# legible :class:`OrderRejectedError` on **every single order**, so a crypto
# session would have traded nothing while narrating a rejection per bar.
#
# ``IOC`` is the considered alternative and is deliberately not used: it would
# cancel an unfilled remainder immediately, turning every partial fill (ADR-0033)
# into a permanent one and quietly changing what a fill means between markets.
# ``GTC`` is the closest analogue of the equity ``DAY`` order — the cost is that a
# crypto order that does *not* fill never expires either (ADR-0058).
_TIME_IN_FORCE: dict[str, str] = {
    ASSET_CLASS_US_EQUITY: "day",
    ASSET_CLASS_CRYPTO: "gtc",
}


def require_asset_class(name: str) -> str:
    """Normalize and validate an asset class, or raise naming the known ones.

    Raises rather than falling back to equity, for the reason
    :func:`~trading.calendar.get_calendar` does (ADR-0054): an unrecognised venue
    that silently became the equity one would send crypto orders to the stock tape
    and get "invalid symbol" on every bar.
    """
    key = name.strip().lower()
    if key not in ASSET_CLASSES:
        known = ", ".join(sorted(ASSET_CLASSES))
        raise ValueError(f"unknown asset class {name!r}; known asset classes: {known}")
    return key


def time_in_force_for(asset_class: str) -> str:
    """The order duration ``asset_class`` accepts (``"day"`` / ``"gtc"``)."""
    return _TIME_IN_FORCE[require_asset_class(asset_class)]


def is_crypto_asset_class(raw: object) -> bool:
    """Whether an SDK-reported asset class means crypto, however it renders.

    alpaca-py's ``AssetClass`` enum stringifies as ``"AssetClass.CRYPTO"`` on some
    versions and ``"crypto"`` on others — the same ambiguity :meth:`_to_order` and
    :meth:`_to_asset` already handle for sides, statuses and exchanges.
    """
    return "crypto" in str(raw).lower()


def canonical_crypto_symbol(symbol: str, asset_class: object, symbol_map: dict[str, str]) -> str:
    """Restore the venue's canonical slash form for a position symbol (ADR-0058).

    **Alpaca disagrees with itself about how a crypto symbol is spelled**, and this
    is the load-bearing consequence. Measured on 2026-08-14 against the paper
    account, for one round trip in one asset: ``submit_order`` was given
    ``"BTC/USD"``, the order it echoed back said ``symbol='BTC/USD'``, and the
    position that fill created reported ``symbol='BTCUSD'``.

    :meth:`AlpacaBroker._reconcile <trading.brokers.alpaca.AlpacaBroker._reconcile>`
    keys its :class:`~trading.types.Portfolio` on whatever this returns, and the
    engine, the sizer and the guardrails all key on the symbol the *bars* carry —
    ``BTC/USD``. Left concatenated, a held position is invisible to every one of
    them: gross exposure reads zero, the target-weight sizer sees an unmet target
    forever, and the run buys the same coin every bar until the cash runs out. It
    is silent, and it is the exact shape ADR-0036 fixed for parked orders arriving
    through a different door.

    The map is the **venue's own asset listing** (``get_all_assets`` with
    ``asset_class=crypto``), not a suffix rule, so there is nothing to keep in sync
    and a pair Alpaca adds tomorrow resolves without a code change. A suffix rule
    over the four live quote currencies was checked against it and agrees on all
    73 pairs with no collisions — that agreement is pinned as a *nightly contract
    test* rather than shipped as a second mechanism (ADR-0035's reuse rule).

    A symbol that is already canonical passes through. A symbol absent from the map
    that the venue *calls crypto* raises: reconciling a crypto position under a key
    nothing else uses is worse than stopping (ADR-0028's bias toward propagating).
    Anything else — a stock position sitting on the same account — is returned
    unchanged, because it is not ours to rewrite.
    """
    if "/" in symbol:
        return symbol
    mapped = symbol_map.get(symbol)
    if mapped is not None:
        return mapped
    if is_crypto_asset_class(asset_class):
        raise ValueError(
            f"Alpaca reported a crypto position in {symbol!r}, which is not in its own "
            f"crypto asset listing, so its canonical pair symbol is unknown. Refusing to "
            f"reconcile it: a position keyed differently from the bars is invisible to "
            f"sizing and the guardrails, and the run would keep buying it (ADR-0058)."
        )
    return symbol


class OrderRejectedError(RuntimeError):
    """The venue refused a specific order outright, at submit time (ADR-0041).

    Distinct from a transport, credential, or rate-limit failure: the request was
    well formed and authenticated, and Alpaca decided *this order* may not be
    placed -- no order id exists and nothing is working. Distinct too from
    :data:`TERMINAL_UNFILLED_STATUSES`, which is a venue decision about an order it
    already accepted (ADR-0033); this one never got that far.

    Raised so :class:`~trading.brokers.alpaca.AlpacaBroker` can record it as a
    rejection instead of letting a raw SDK ``APIError`` escape the seam and kill a
    live session (ADR-0017: no SDK type leaves this module).
    """


# HTTP statuses that are never a refusal of the order itself, whatever the body
# says: 401 is a credential problem and 429 is a rate limit -- both mean "we could
# not ask", the distinction ADR-0028 draws for asset lookups. Everything 5xx is
# excluded by the 4xx range check below.
_NOT_AN_ORDER_REFUSAL = frozenset({401, 429})


@runtime_checkable
class AlpacaClient(Protocol):
    """Exactly what the Alpaca data adapter and paper broker need from Alpaca.

    Two concrete implementations satisfy it: :class:`FakeAlpacaClient` (in-memory,
    deterministic, for the fast test layer) and :class:`RealAlpacaClient` (the
    live SDK wrapper). Every method returns our own types, never an SDK type.
    """

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool
    ) -> list[Bar]:
        """Daily bars for ``symbol`` in ``[start, end]``, ascending by time.

        ``adjusted`` selects split/dividend-adjusted vs raw prices (ADR-0008).
        """
        ...

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool, interval: timedelta
    ) -> list[Bar]:
        """Bars for ``symbol`` in ``[start, end]`` at the given ``interval`` (ADR-0022).

        The interval selects the bar cadence (daily or intraday); ``get_daily_bars``
        is the ``interval == 1 day`` special case. Bars are ascending by time with
        START timestamps, ``adjusted`` selecting adjusted vs raw prices (ADR-0008).
        """
        ...

    def get_splits(self, symbol: str, start: datetime, end: datetime) -> list[SplitEvent]:
        """Stock splits for ``symbol`` with an ex-date inside ``[start, end]`` (ADR-0045).

        The seventh call on the seam, and the second widening ADR-0017
        anticipated. It exists because the bars endpoint's ``adjustment=all`` is
        not self-verifying: on 2026-08-09 Alpaca served AAPL's 2020-08-31 bars
        with the 4:1 split **not** backed out while still answering the adjusted
        request, i.e. a phantom-split cliff (ADR-0008) inside a series that claims
        to have none. This call is the independent record that makes the defect
        detectable rather than merely suspected — and Alpaca's own corporate-actions
        endpoint *does* carry the split, so the two halves of the provider
        disagree with each other, not with us.

        A transport/plan failure raises, exactly like :meth:`get_asset`: "we could
        not ask" is never the same answer as "there were no splits" (ADR-0028).
        """
        ...

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        """Submit a market order for ``qty`` shares (fractional allowed, ADR-0011).

        Long-or-flat only: a sell may not exceed the held quantity (no implicit
        shorting, ADR-0011).
        """
        ...

    def get_order(self, order_id: str) -> AlpacaOrder:
        """Fetch the current state of a previously submitted order."""
        ...

    def cancel_order(self, order_id: str) -> None:
        """Ask the venue to cancel a working order (ADR-0036).

        The sixth call on the seam, and the widening ADR-0017 anticipated. It is
        what lets an operator (or a test) clear an order the venue has *parked* --
        a market order placed while the market is closed queues for the next open
        and stays working indefinitely, so without this there is no way to take it
        back short of the Alpaca dashboard.

        Cancellation is a *request*, not a result: it returns nothing and the
        order reaches ``canceled`` asynchronously, so callers must re-read it via
        :meth:`get_order` (which is exactly what the broker's poll already does).
        Cancelling an order that is already terminal succeeds silently -- verified
        against the live paper venue, which answers a repeat cancel with 200, not
        an error. An unknown order id raises :class:`LookupError`, keeping "we
        never heard of it" apart from "we cancelled it" the same way
        :meth:`get_asset` does.
        """
        ...

    def get_account(self) -> AccountSnapshot:
        """Current cash and equity."""
        ...

    def list_positions(self) -> list[PositionSnapshot]:
        """All open positions (empty when flat)."""
        ...

    def get_asset(self, symbol: str) -> AssetInfo:
        """Broker-authoritative metadata for ``symbol`` (ADR-0028).

        This is how a curated universe (ADR-0024) gets verified against what the
        venue will actually trade. An unknown ticker raises :class:`LookupError`;
        a transport/API failure surfaces as whatever the underlying client raises,
        so a network hiccup is never mistaken for a delisted stock.
        """
        ...


# --- Fake: the offline workhorse ----------------------------------------------


@dataclass
class _FakeState:
    """Mutable account state the fake maintains; kept off the public surface."""

    cash: float
    positions: dict[str, PositionSnapshot] = field(default_factory=dict)
    orders: dict[str, AlpacaOrder] = field(default_factory=dict)


class FakeAlpacaClient:
    """Deterministic, in-memory :class:`AlpacaClient` for the fast test layer.

    Construct it with a per-symbol bar history and starting cash. By default
    ``submit_order`` fills immediately -- at an explicitly set price, else the
    symbol's most recent bar close -- and updates cash and positions so
    ``get_account`` / ``list_positions`` reflect the trade at once.

    Set ``auto_fill=False`` for a scriptable pending mode: ``submit_order`` leaves
    the order ``"new"`` and untouched until :meth:`fill_order` advances it, which
    is what lets a broker lane test submit-then-poll (and timeout) behaviour.
    There is no wall clock and no RNG; order ids are a monotonic counter.

    :meth:`get_asset` answers "tradable + fractionable" for every symbol by
    default; :meth:`set_asset` scripts a specific answer (e.g. a non-fractionable
    or halted name) and :meth:`set_asset_failure` scripts a lookup that blows up,
    which is what lets the universe validator (ADR-0028) be tested offline.
    """

    def __init__(
        self,
        bars: dict[str, list[Bar]] | None = None,
        *,
        cash: float = 100_000.0,
        auto_fill: bool = True,
        assets: dict[str, AssetInfo] | None = None,
    ) -> None:
        self._bars: dict[str, list[Bar]] = {
            symbol: sorted(series, key=lambda b: b.ts) for symbol, series in (bars or {}).items()
        }
        self._state = _FakeState(cash=cash)
        self._auto_fill = auto_fill
        self._prices: dict[str, float] = {}
        self._assets: dict[str, AssetInfo] = dict(assets or {})
        self._asset_failures: dict[str, str] = {}
        self._splits: dict[str, list[SplitEvent]] = {}
        self._split_failures: dict[str, str] = {}
        # (symbol, side-or-None) -> the exception submit_order should raise.
        self._submit_failures: dict[tuple[str, Side | None], Exception] = {}
        self._next_id = 1

    # -- test/setup helpers (not part of the protocol) --

    def set_price(self, symbol: str, price: float) -> None:
        """Set the price ``submit_order`` (and :meth:`fill_order`) will fill at."""
        self._prices[symbol] = price

    def set_asset(
        self,
        symbol: str,
        *,
        tradable: bool = True,
        fractionable: bool = True,
        shortable: bool = True,
        exchange: str = _FAKE_EXCHANGE,
        name: str = "",
        min_order_size: float | None = None,
    ) -> AssetInfo:
        """Script the :meth:`get_asset` answer for ``symbol`` and return it.

        Defaults to a fully usable asset, so a test only names the flag it cares
        about (``set_asset("BRK.A", fractionable=False)``).
        """
        asset = AssetInfo(
            symbol=symbol,
            tradable=tradable,
            fractionable=fractionable,
            exchange=exchange,
            name=name,
            shortable=shortable,
            min_order_size=min_order_size,
        )
        self._assets[symbol] = asset
        self._asset_failures.pop(symbol, None)
        return asset

    def set_order_status(
        self,
        order_id: str,
        status: str,
        *,
        filled_qty: float | None = None,
        filled_avg_price: float | None = None,
    ) -> AlpacaOrder:
        """Script what the venue *reports* for an order, with no account effects.

        Unlike :meth:`fill_order` (which moves cash and positions), this only
        rewrites the reported :class:`AlpacaOrder`, because that is exactly what a
        real venue does when it cancels or expires a working order -- and, for a
        partially-filled-then-canceled order, the reported quantity legitimately
        differs from what the order asked for. It is how the broker's terminal
        status handling gets tested offline (ADR-0033).
        """
        order = self._state.orders[order_id]
        updated = replace(
            order,
            status=status,
            filled_qty=order.filled_qty if filled_qty is None else filled_qty,
            filled_avg_price=(
                order.filled_avg_price if filled_avg_price is None else filled_avg_price
            ),
        )
        self._state.orders[order_id] = updated
        return updated

    def set_submit_refusal(self, symbol: str, message: str, *, side: Side | None = None) -> None:
        """Make :meth:`submit_order` refuse ``symbol`` with an :class:`OrderRejectedError`.

        The fake accepted every order until ADR-0041, which is exactly why the live
        duplicate-guard test asserted an exit the real venue refuses. ``side``
        scopes the refusal to one direction, because that is the shape the venue
        actually has: a parked BUY makes the *SELL* a "potential wash trade", while
        the BUY itself is still accepted.
        """
        self._submit_failures[(symbol, side)] = OrderRejectedError(message)

    def set_submit_failure(
        self, symbol: str, error: Exception, *, side: Side | None = None
    ) -> None:
        """Make :meth:`submit_order` raise ``error`` -- a *transport* failure, not a refusal.

        The other side of ADR-0041's classification: something that means "we could
        not ask" must still propagate out of the broker rather than being recorded
        as the venue's decision.
        """
        self._submit_failures[(symbol, side)] = error

    def clear_submit_refusals(self) -> None:
        """Drop every scripted submit failure (the venue stops refusing)."""
        self._submit_failures.clear()

    def set_asset_failure(self, symbol: str, message: str = "asset lookup failed") -> None:
        """Make :meth:`get_asset` raise for ``symbol`` (an unknown ticker or API error)."""
        self._asset_failures[symbol] = message
        self._assets.pop(symbol, None)

    def set_splits(self, symbol: str, splits: list[SplitEvent]) -> None:
        """Script the corporate-actions record :meth:`get_splits` will report."""
        self._splits[symbol] = list(splits)
        self._split_failures.pop(symbol, None)

    def set_splits_failure(self, symbol: str, message: str = "split lookup failed") -> None:
        """Make :meth:`get_splits` raise for ``symbol`` -- "we could not ask" (ADR-0028).

        The distinction the adapter's guard turns on: a failed lookup is not
        evidence that the adjusted series is wrong, so it may not be reported as
        one (ADR-0045).
        """
        self._split_failures[symbol] = message
        self._splits.pop(symbol, None)

    def _fill_price(self, symbol: str, override: float | None) -> float:
        """Resolve a fill price: explicit override, set price, else last bar close."""
        if override is not None:
            return override
        if symbol in self._prices:
            return self._prices[symbol]
        series = self._bars.get(symbol)
        if series:
            return series[-1].close
        raise ValueError(f"No price available to fill {symbol!r}; call set_price or supply bars")

    # -- AlpacaClient protocol --

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        """Return supplied bars for ``symbol`` within ``[start, end]`` inclusive.

        The fake stores whatever bars it was given, so ``adjusted`` is accepted
        for signature parity but does not re-derive prices.
        """
        return [b for b in self._bars.get(symbol, []) if start <= b.ts <= end]

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
        interval: timedelta = timedelta(days=1),
    ) -> list[Bar]:
        """Return supplied bars for ``symbol`` within ``[start, end]`` inclusive.

        The fake serves back exactly the bars it was constructed with (their own
        timestamps carry the real cadence), so ``interval`` and ``adjusted`` are
        accepted for signature parity but do not re-derive or re-bucket prices.
        """
        return [b for b in self._bars.get(symbol, []) if start <= b.ts <= end]

    def get_splits(self, symbol: str, start: datetime, end: datetime) -> list[SplitEvent]:
        """Return the scripted splits for ``symbol`` with an ex-date in ``[start, end]``.

        Empty by default: the fake models no corporate actions, so a test that
        cares about one says so explicitly via :meth:`set_splits`.
        """
        if symbol in self._split_failures:
            raise RuntimeError(f"{self._split_failures[symbol]}: {symbol!r}")
        return [
            split
            for split in self._splits.get(symbol, [])
            if start.date() <= split.ex_date <= end.date()
        ]

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        scripted = self._submit_failures.get((symbol, side)) or self._submit_failures.get(
            (symbol, None)
        )
        if scripted is not None:
            # Nothing is recorded and no id is issued: a refused order does not
            # exist at the venue, which is what the broker's guard depends on.
            raise scripted
        if qty <= 0:
            raise ValueError(f"order qty must be positive, got {qty}")
        order_id = str(self._next_id)
        self._next_id += 1
        if self._auto_fill:
            order = self._make_filled(order_id, symbol, qty, side, price=None)
        else:
            order = AlpacaOrder(id=order_id, symbol=symbol, qty=qty, side=side, status=STATUS_NEW)
        self._state.orders[order_id] = order
        return order

    def fill_order(self, order_id: str, price: float | None = None) -> AlpacaOrder:
        """Advance a pending order to filled, applying cash and position effects.

        Only meaningful in ``auto_fill=False`` mode; filling an already-filled
        order is a no-op that returns the current state.
        """
        order = self._state.orders[order_id]
        if order.status == STATUS_FILLED:
            return order
        filled = self._make_filled(order.id, order.symbol, order.qty, order.side, price=price)
        self._state.orders[order_id] = filled
        return filled

    def get_order(self, order_id: str) -> AlpacaOrder:
        return self._state.orders[order_id]

    def cancel_order(self, order_id: str) -> None:
        """Mark a working order ``canceled``; a terminal one is left alone.

        Mirrors what the live paper venue was observed to do (ADR-0036): a repeat
        cancel is accepted silently rather than raising, and any partial fill the
        order already got stays on the record so the broker can still emit it.
        Unlike the venue, the transition is immediate -- there is no clock here.
        """
        order = self._state.orders.get(order_id)
        if order is None:
            raise LookupError(f"unknown Alpaca order {order_id!r}")
        if order.status in TERMINAL_STATUSES:
            return
        self._state.orders[order_id] = replace(order, status=STATUS_CANCELED)

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=self._state.cash, equity=self._equity())

    def list_positions(self) -> list[PositionSnapshot]:
        return [pos for pos in self._state.positions.values() if abs(pos.qty) > SHARE_EPS]

    def get_asset(self, symbol: str) -> AssetInfo:
        """Return the scripted asset for ``symbol``, else a fully usable default.

        A symbol registered via :meth:`set_asset_failure` raises
        :class:`LookupError`, which is how the "unverified" path gets exercised.
        """
        if symbol in self._asset_failures:
            raise LookupError(f"{self._asset_failures[symbol]}: {symbol!r}")
        existing = self._assets.get(symbol)
        if existing is not None:
            return existing
        return AssetInfo(
            symbol=symbol,
            tradable=True,
            fractionable=True,
            exchange=_FAKE_EXCHANGE,
            shortable=True,
        )

    # -- internal accounting (mirrors Portfolio.apply_fill semantics) --

    def _make_filled(
        self, order_id: str, symbol: str, qty: float, side: Side, *, price: float | None
    ) -> AlpacaOrder:
        fill_price = self._fill_price(symbol, price)
        self._apply(symbol, qty, side, fill_price)
        return AlpacaOrder(
            id=order_id,
            symbol=symbol,
            qty=qty,
            side=side,
            status=STATUS_FILLED,
            filled_qty=qty,
            filled_avg_price=fill_price,
        )

    def _apply(self, symbol: str, qty: float, side: Side, price: float) -> None:
        """Update cash and the position for one fill; reject implicit shorts."""
        pos = self._state.positions.get(symbol, PositionSnapshot(symbol, 0.0, 0.0))
        if side is Side.SELL and qty > pos.qty + SHARE_EPS:
            raise ValueError(
                f"cannot sell {qty} of {symbol}; only {pos.qty} held "
                "(implicit shorting is disallowed)"
            )
        signed = qty if side is Side.BUY else -qty
        self._state.cash -= signed * price
        new_qty = pos.qty + signed
        if abs(new_qty) <= SHARE_EPS:
            self._state.positions.pop(symbol, None)
            return
        if side is Side.BUY:
            cost_basis = pos.qty * pos.avg_price + qty * price
            avg_price = cost_basis / new_qty
        else:
            avg_price = pos.avg_price
        self._state.positions[symbol] = replace(pos, qty=new_qty, avg_price=avg_price)

    def _equity(self) -> float:
        """Cash plus each position marked at its resolvable price."""
        total = self._state.cash
        for symbol, pos in self._state.positions.items():
            if abs(pos.qty) <= SHARE_EPS:
                continue
            total += pos.qty * self._fill_price(symbol, None)
        return total


# --- Real: reading the SDK's responses safely ---------------------------------
# Every alpaca-py client method is annotated ``Model | Dict[str, Any]``: the dict
# arm is what a client constructed with ``raw_data=True`` returns. We never do
# that, so the dict arm is unreachable -- but it is unreachable by *our*
# construction, not by the SDK's type, so we assert it loudly instead of
# assuming it away (ADR-0033).


def _require_model[Model](response: Model | dict[str, Any], what: str) -> Model:
    """Return the SDK model arm of a ``Model | dict`` response, or fail loudly.

    A dict here means the underlying client was built with ``raw_data=True``,
    which :class:`RealAlpacaClient` never does; getting one back means the SDK's
    contract changed under us, and reading fields off a dict with ``getattr``
    would silently produce empty/False values instead (ADR-0028's "absent
    permission is not permission" would then mislabel every asset).
    """
    if isinstance(response, dict):
        raise TypeError(
            f"Alpaca returned raw dict data for {what}; RealAlpacaClient expects "
            "SDK models (it never sets raw_data=True)"
        )
    return response


class DataSubscriptionError(RuntimeError):
    """Alpaca refused the request because the account's data plan forbids it.

    Distinct from a transport failure or an unknown symbol: the request was well
    formed and authenticated, the plan simply does not cover that data. Raised so
    the paper/live feed fails with an actionable message naming ``--data-feed``
    instead of a raw SDK traceback (ADR-0034).
    """


# What a data-plan refusal looks like in the API's error body. Alpaca answers
# HTTP 403 with e.g. ``{"message":"subscription does not permit querying recent
# SIP data"}``; the free plan hits this for anything inside the last ~15 minutes
# on the SIP feed, which is exactly what the live paper feed asks for (ADR-0034).
_SUBSCRIPTION_MARKER = "subscription does not permit"


def _classify_data_error(exc: Exception, symbol: str, feed: str | None) -> Exception:
    """Map an SDK bar-fetch failure to our own error type, or pass it through.

    A data-plan refusal (HTTP 403 + "subscription does not permit") becomes a
    :class:`DataSubscriptionError` naming the feed that would work; everything
    else -- auth, rate limit, transport -- propagates unchanged, mirroring how
    ``get_asset`` keeps "the broker said no" apart from "we could not ask"
    (ADR-0028).
    """
    if _SUBSCRIPTION_MARKER not in str(exc):
        return exc
    return DataSubscriptionError(
        f"Alpaca's data plan does not cover this request for {symbol!r} "
        f"(feed={feed or 'sip (SDK default)'}): {exc}. "
        "The free plan serves recent bars only on the IEX feed — pass "
        "--data-feed iex (paper/live default) or request an older window."
    )


def _order_error_code(exc: Exception) -> int | None:
    """Alpaca's numeric error code for a refused order, or ``None`` if it gave none.

    This is the structural discriminator :func:`_classify_order_error` turns on,
    and it was read off the wire rather than assumed (2026-08-08, paper account):
    a refusal of a *specific order* always carries an eight-digit ``code`` in the
    body (``40310000`` insufficient buying power / wash trade, ``42210000`` unknown
    asset / fractional short), while a credential failure answers a bare
    ``{"message": "unauthorized."}`` with no code at all.

    ``APIError.code`` is a *property that raises* in that no-code case -- alpaca-py
    implements it as ``json.loads(self._error)["code"]``, so it throws ``KeyError``
    rather than returning ``None`` -- which is why this cannot be a plain
    ``getattr`` with a default.
    """
    try:
        code: object = exc.code  # type: ignore[attr-defined]
    except Exception:
        return None
    return code if isinstance(code, int) else None


def _classify_order_error(exc: Exception, symbol: str, qty: float, side: Side) -> Exception:
    """Map a failed order submission to our own type, or pass it through (ADR-0041).

    "The venue refused this order" becomes an :class:`OrderRejectedError` carrying
    the venue's body verbatim; "we could not ask" -- bad credentials, a rate limit,
    a 5xx, a socket that died -- propagates unchanged, exactly the way
    :meth:`RealAlpacaClient.get_asset` keeps an unknown ticker apart from a
    transport failure (ADR-0028).

    Getting this backwards is expensive in both directions. Treating a refusal as
    fatal kills a live session mid-bar and loses its artifacts (the bug this
    function exists to fix). Treating an outage as a refusal would let a run
    continue quietly recording rejections and never trading -- so the pass-through
    side is deliberately the default, and only a 4xx that Alpaca *named* with an
    error code is claimed as a refusal.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or not (400 <= status < 500):
        return exc
    if status in _NOT_AN_ORDER_REFUSAL:
        return exc
    code = _order_error_code(exc)
    if code is None:
        return exc
    return OrderRejectedError(
        f"Alpaca refused {side.value} {qty:g} {symbol} (HTTP {status}, code {code}): {exc}"
    )


def _require_float(value: str | float | None, what: str) -> float:
    """Coerce an Alpaca numeric field (often a string) to float, or fail loudly.

    Alpaca sends most numbers as JSON strings and types many of them
    ``Optional``, so a silent ``float(None)`` crash with no context is a real
    possibility on a field the venue chose to omit (``TradeAccount.cash`` and
    ``.equity`` are both ``Optional[str]`` in alpaca-py 0.43.5).
    """
    if value is None:
        raise ValueError(f"Alpaca omitted {what}; cannot use the account without it")
    return float(value)


# --- Real: the live SDK wrapper (guarded, lazy) --------------------------------


class RealAlpacaClient:
    """A thin :class:`AlpacaClient` over the real ``alpaca-py`` SDK.

    The SDK is an optional dependency: it is imported lazily inside ``__init__``
    and the methods, so importing this module never requires ``alpaca-py`` and a
    missing install fails only when someone constructs this client for live use
    (ADR-0018). Credentials come from ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``
    (optionally overridden by constructor arguments); ``paper`` selects the
    paper-trading endpoint. Every method converts SDK responses into our own
    :class:`~trading.types.Bar` and DTOs, so no SDK type escapes this class.

    ``feed`` picks the market-data feed for bar requests (ADR-0034). ``None``
    leaves the SDK's default (the consolidated SIP tape), which is what a
    historical backtest wants; a data plan that does not cover recent SIP bars
    needs ``"iex"`` for anything inside the last ~15 minutes, which is exactly
    what the live paper feed asks for. The feed is a *construction* property, like
    the interval (ADR-0022): one client serves one tape.

    ``asset_class`` picks the venue (ADR-0058), and is a construction property for
    the same reason: Alpaca's crypto bars live behind a **different SDK client**
    (``CryptoHistoricalDataClient.get_crypto_bars``), and the stock client answers a
    slash symbol with ``invalid symbol: BTC/USD``. Selecting crypto also changes the
    order duration the venue will accept, restores the canonical pair symbol on
    positions, and drops the corporate-actions cross-check — see each method. Three
    things it does **not** change: the trading endpoint (one ``TradingClient`` serves
    both), the credentials, and this class's public surface.

    One asymmetry worth knowing: **crypto market data needs no credentials at all**
    (measured — a bare ``CryptoHistoricalDataClient()`` returned bars byte-identical
    to a keyed one), and there is no ``feed`` to choose, so ADR-0034's free-plan SIP
    restriction has no crypto analogue. Credentials are still required here because
    the trading client needs them; passing ``feed`` alongside crypto is a
    ``ValueError`` rather than a silently ignored argument.
    """

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        paper: bool = True,
        feed: str | None = None,
        asset_class: str = ASSET_CLASS_US_EQUITY,
    ) -> None:
        key = api_key or os.environ.get("ALPACA_API_KEY")
        secret = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise ValueError(
                "Alpaca credentials required: set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY (or pass them explicitly)"
            )
        self._asset_class = require_asset_class(asset_class)
        if self._asset_class == ASSET_CLASS_CRYPTO and feed is not None:
            raise ValueError(
                f"feed={feed!r} does not apply to the crypto venue: CryptoBarsRequest has "
                "no feed field at all (its fields are currency, end, limit, sort, start, "
                "symbol_or_symbols, timeframe), so there is no IEX/SIP choice to make and "
                "ADR-0034's data-plan restriction has no crypto analogue (ADR-0058)."
            )
        try:
            from alpaca.data.historical import (
                CryptoHistoricalDataClient,
                StockHistoricalDataClient,
            )
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - alpaca-py not installed here
            raise ImportError(
                "alpaca-py is required for live trading; pip install alpaca-py"
            ) from exc
        self._data: Any = (
            CryptoHistoricalDataClient()
            if self._asset_class == ASSET_CLASS_CRYPTO
            else StockHistoricalDataClient(key, secret)
        )
        self._trading = TradingClient(key, secret, paper=paper)
        self._feed = feed
        # Built on first use only: the corporate-actions endpoint is a *different*
        # Alpaca service with its own base URL, and most runs never ask for a
        # split, so a client that only trades pays nothing for it.
        self._key = key
        self._secret = secret
        self._corporate_actions: Any | None = None
        # Concatenated -> slash pair symbols, from the venue's own asset listing.
        # One request per client, and only when a crypto position is first read.
        self._crypto_symbols: dict[str, str] | None = None

    @property
    def feed(self) -> str | None:
        """The market-data feed bar requests use (``None`` = the SDK default tape)."""
        return self._feed

    @property
    def asset_class(self) -> str:
        """The venue this client serves: ``"us_equity"`` or ``"crypto"`` (ADR-0058)."""
        return self._asset_class

    def _feed_kwargs(self) -> dict[str, Any]:
        """The ``feed=`` keyword for a bars request, empty when unset (ADR-0034).

        Omitting the keyword entirely -- rather than passing ``None`` -- keeps a
        default-constructed client's request bytes identical to before this
        parameter existed, so the historical/backtest path is unchanged.
        """
        if self._feed is None:
            return {}
        from alpaca.data.enums import DataFeed

        return {"feed": DataFeed(self._feed)}

    def get_daily_bars(
        self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
    ) -> list[Bar]:
        from alpaca.data.timeframe import TimeFrame

        return self._fetch_bars(symbol, start, end, TimeFrame.Day, adjusted, "daily bars")

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
        interval: timedelta = timedelta(days=1),
    ) -> list[Bar]:  # pragma: no cover - needs the alpaca-py SDK and the network
        return self._fetch_bars(
            symbol, start, end, self._to_timeframe(interval), adjusted, "intraday bars"
        )

    def _fetch_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Any,
        adjusted: bool,
        what: str,
    ) -> list[Bar]:  # pragma: no cover - needs the alpaca-py SDK and the network
        """One bar request against whichever tape this client serves (ADR-0058).

        The equity arm is exactly what ``get_daily_bars``/``get_bars`` sent before
        this branch existed — same request type, same ``Adjustment``, same
        ``feed`` kwarg — so no equity request byte moves.

        The crypto arm asks a **different client** with a **different request
        type**, and it deliberately carries neither of the equity arm's two extras:

        * **No ``adjustment``.** ``CryptoBarsRequest`` has no such field, so
          ``adjusted=True`` and ``adjusted=False`` return the same bars. That is
          not the flag being ignored — a crypto pair has no splits and no
          dividends, so raw prices *are* the total-return series and ADR-0008 and
          ADR-0021 ask for the same thing here. The honest limit, recorded in
          ADR-0058: a token redenomination or an exchange rewriting its history
          would arrive as an unadjusted cliff, Alpaca publishes no
          corporate-actions record for crypto to catch it with (measured: the
          endpoint answers a crypto symbol with empty data, not an error), and
          nothing in this bench would notice.
        * **No ``feed``.** Rejected at construction; see ``__init__``.
        """
        if self._asset_class == ASSET_CLASS_CRYPTO:
            from alpaca.data.requests import CryptoBarsRequest

            crypto_request = CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end
            )
            try:
                response = self._data.get_crypto_bars(crypto_request)
            except Exception as exc:
                raise _classify_data_error(exc, symbol, self._feed) from exc
        else:
            from alpaca.data.enums import Adjustment
            from alpaca.data.requests import StockBarsRequest

            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                adjustment=Adjustment.ALL if adjusted else Adjustment.RAW,
                **self._feed_kwargs(),
            )
            try:
                response = self._data.get_stock_bars(request)
            except Exception as exc:
                raise _classify_data_error(exc, symbol, self._feed) from exc
        barset = _require_model(response, what)
        rows: list[Any] = barset.data.get(symbol, [])
        return self._rows_to_bars(symbol, rows)

    @staticmethod
    def _to_timeframe(interval: timedelta) -> Any:  # pragma: no cover - needs the SDK
        """Map a bar ``interval`` to an alpaca-py ``TimeFrame`` (ADR-0022).

        A day-or-longer interval is ``TimeFrame.Day``; an hour-multiple maps to
        ``TimeFrameUnit.Hour``; anything finer maps to whole-minute
        ``TimeFrameUnit.Minute`` bars.
        """
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        if interval >= timedelta(days=1):
            return TimeFrame.Day
        total_minutes = int(interval.total_seconds() // 60)
        if total_minutes >= 60 and total_minutes % 60 == 0:
            return TimeFrame(total_minutes // 60, TimeFrameUnit.Hour)
        return TimeFrame(total_minutes, TimeFrameUnit.Minute)

    @staticmethod
    def _rows_to_bars(symbol: str, rows: list[Any]) -> list[Bar]:
        """Convert SDK bar rows into our :class:`~trading.types.Bar`, ascending by time.

        **Known lossy conversion on crypto, recorded not fixed (ADR-0058):**
        :class:`~trading.types.Bar` types ``volume`` as an ``int`` because a share
        count is one, and ``int()`` truncates. Crypto volume is a *coin* count and
        is fractional — the venue served ``BTC/USD`` daily volumes of ``1.205``
        and ``0.147`` on 2026-08-13/14, which land here as ``1`` and **``0``**. A
        zero-volume bar is a lie about a day that did trade.

        The blast radius is exactly one caller: ``volume`` is read only by
        :mod:`trading.liquidity`'s ADV screen (ADR-0029), which is opt-in via
        ``--min-adv``. That screen is unusable on this tape for a larger and
        separate reason anyway — Alpaca's crypto venue volume is its own, not the
        global market's, so BTC/USD averages tens of thousands of dollars a day
        against an equity-calibrated $20M floor. Widening ``Bar.volume`` to a float
        touches ``types.py`` and every adapter, so it is a follow-up card rather
        than this lane's to land.
        """
        bars = [
            Bar(
                symbol=symbol,
                ts=row.timestamp,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=int(row.volume),
            )
            for row in rows
        ]
        bars.sort(key=lambda b: b.ts)
        return bars

    def get_splits(self, symbol: str, start: datetime, end: datetime) -> list[SplitEvent]:
        """Stock splits for ``symbol`` with an ex-date in ``[start, end]`` (ADR-0045).

        Reads Alpaca's corporate-actions endpoint -- a *different* service from
        the bars endpoint, which is the whole point: it is an independent record
        of the events the adjusted bars are supposed to have backed out. Verified
        against the live paper plan on 2026-08-09: it returns AAPL's 2020-08-31
        ``forward_splits`` entry (``new_rate=4.0, old_rate=1.0``) even though the
        bars endpoint's adjusted series ignores it.

        Both ``forward_splits`` and ``reverse_splits`` are read; the other
        corporate-action types Alpaca reports (cash dividends, name changes,
        mergers, ...) do not rescale a price series and are skipped. Failures
        propagate: a plan or transport error means "we could not ask", which the
        caller must not read as "there were no splits" (ADR-0028).
        """
        if self._asset_class == ASSET_CLASS_CRYPTO:
            # Not "we could not ask" (ADR-0028) and not a skipped request we are
            # guessing about: Alpaca's corporate-actions endpoint *was* asked for
            # BTC/USD on 2026-08-14 and answered cleanly with **no data keys at
            # all**. A crypto pair has no splits or dividends to back out, so
            # there is nothing here to report and no request worth paying for.
            # The limit ADR-0058 records rather than solves: a token
            # redenomination is a real rescaling that this endpoint does not
            # carry, so it would reach a backtest as an uncaught cliff.
            return []
        from alpaca.data.historical.corporate_actions import CorporateActionsClient
        from alpaca.data.requests import CorporateActionsRequest

        if self._corporate_actions is None:
            self._corporate_actions = CorporateActionsClient(self._key, self._secret)
        response = self._corporate_actions.get_corporate_actions(
            CorporateActionsRequest(symbols=[symbol], start=start.date(), end=end.date())
        )
        actions = _require_model(response, "corporate actions")
        data: dict[str, list[Any]] = actions.data
        splits: list[SplitEvent] = []
        for kind in ("forward_splits", "reverse_splits"):
            for row in data.get(kind, []):
                old_rate = float(row.old_rate)
                if old_rate <= 0.0:
                    continue  # a rate of zero is not a rescaling we can reason about
                splits.append(
                    SplitEvent(
                        symbol=str(getattr(row, "symbol", "") or symbol),
                        ex_date=row.ex_date,
                        ratio=float(row.new_rate) / old_rate,
                    )
                )
        splits.sort(key=lambda s: s.ex_date)
        return splits

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        """Place a market order, or raise :class:`OrderRejectedError` if refused.

        A venue refusal is *classified*, not leaked: without this the SDK's
        ``APIError`` travelled out of the seam, out of ``AlpacaBroker.submit`` and
        out of a live session, taking the run's artifacts with it (ADR-0041).

        The order's **duration comes from the asset class** (``day`` for equities,
        ``gtc`` for crypto — see :data:`_TIME_IN_FORCE`). Hard-coding ``DAY`` made
        every crypto order a venue refusal; the quantity, side and order type are
        unchanged across venues.

        The venue also **truncates the quantity** rather than refusing an
        over-precise one: ``0.00021739130434782607`` (what the target-weight sizer
        actually emits) came back as ``0.000217391``, i.e. rounded to the nine
        decimals ``min_trade_increment`` publishes. So no rounding happens on this
        side, and none should be added — but a venue that started refusing instead
        would break every order, which is why the nightly contract test pins it.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side is Side.BUY else OrderSide.SELL,
            time_in_force=TimeInForce(time_in_force_for(self._asset_class)),
        )
        try:
            placed = self._trading.submit_order(request)
        except Exception as exc:
            raise _classify_order_error(exc, symbol, qty, side) from exc
        return self._to_order(_require_model(placed, "submit_order"))

    def get_order(self, order_id: str) -> AlpacaOrder:
        return self._to_order(
            _require_model(self._trading.get_order_by_id(order_id), "get_order_by_id")
        )

    def cancel_order(self, order_id: str) -> None:
        """Cancel a working order at the venue (ADR-0036).

        The SDK's ``cancel_order_by_id`` returns ``None`` and the order reaches
        ``canceled`` a moment later, so this returns nothing and callers re-read
        the status. An unknown id (the SDK's 404) becomes a :class:`LookupError`,
        matching :meth:`get_asset`; anything else -- auth, rate limit, transport,
        or a venue that refuses the cancel -- propagates unchanged, so "we could
        not ask" is never mistaken for "it is cancelled".

        **Idempotence is now enforced here rather than assumed of the venue**
        (ADR-0058 amending ADR-0036). ADR-0036 recorded a repeat cancel as
        succeeding silently, and it does -- for an order that is already
        ``canceled``. A **filled** order is different: measured 2026-08-14, the
        venue answers ``422``/``42210000`` *"order is already in \\"filled\\"
        state"*. That case had simply never been executed, because the equity test
        that established the contract ran with the market shut, where nothing
        fills. It is not crypto-specific as far as anyone can tell; crypto is just
        the first venue on which this bench could fill an order on demand.

        The distinction is drawn on the order's **state**, not on the error text:
        a failed cancel re-reads the order, and a terminal one means the caller
        already has what they asked for -- nothing is working. ADR-0041's rule
        against matching message substrings is why this is a second request rather
        than a cheaper string check, and the request is only paid on the failure
        path. If the re-read says the order is still working, the original failure
        propagates unchanged.
        """
        try:
            self._trading.cancel_order_by_id(order_id)
        except Exception as exc:  # narrowed below: only a 404 becomes LookupError
            if getattr(exc, "status_code", None) == 404:
                raise LookupError(f"unknown Alpaca order {order_id!r}") from exc
            if self._is_already_terminal(order_id):
                return
            raise

    def _is_already_terminal(self, order_id: str) -> bool:  # pragma: no cover - SDK only
        """Whether a cancel failed only because the order had already settled.

        A best-effort re-read: if *this* lookup fails too, the honest answer is
        "we do not know", which means the original cancel failure must stand.
        """
        try:
            return self.get_order(order_id).status in TERMINAL_STATUSES
        except Exception:
            return False

    def get_account(self) -> AccountSnapshot:
        account = _require_model(self._trading.get_account(), "get_account")
        return AccountSnapshot(
            cash=_require_float(account.cash, "TradeAccount.cash"),
            equity=_require_float(account.equity, "TradeAccount.equity"),
        )

    def list_positions(self) -> list[PositionSnapshot]:
        """Open positions, with crypto symbols restored to the venue's slash form.

        On the equity venue this is exactly what it always was. On crypto it is
        not cosmetic: Alpaca echoes ``BTC/USD`` on the order and ``BTCUSD`` on the
        position it creates, and the concatenated key would make the holding
        invisible to sizing and the guardrails. See
        :func:`canonical_crypto_symbol` for the measurement and the consequence.
        """
        positions = _require_model(self._trading.get_all_positions(), "get_all_positions")
        return [
            PositionSnapshot(
                symbol=self._position_symbol(position),
                qty=_require_float(position.qty, "Position.qty"),
                avg_price=_require_float(position.avg_entry_price, "Position.avg_entry_price"),
            )
            for position in positions
        ]

    def _position_symbol(self, position: Any) -> str:  # pragma: no cover - SDK only
        """The canonical symbol for one reported position (ADR-0058)."""
        symbol = str(position.symbol)
        if self._asset_class != ASSET_CLASS_CRYPTO:
            return symbol
        return canonical_crypto_symbol(
            symbol, getattr(position, "asset_class", ""), self._crypto_symbol_map()
        )

    def _crypto_symbol_map(self) -> dict[str, str]:  # pragma: no cover - SDK only
        """Concatenated -> slash pair symbols, from the venue's own asset listing.

        Built lazily and cached for the client's lifetime: one extra request per
        session, paid the first time a crypto position is read, never per bar. A
        failure propagates rather than degrading to the concatenated key — a run
        that cannot name what it holds must stop, not narrate (ADR-0028).
        """
        if self._crypto_symbols is None:
            from alpaca.trading.enums import AssetClass
            from alpaca.trading.requests import GetAssetsRequest

            assets = self._trading.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO))
            symbol_map: dict[str, str] = {}
            for asset in assets:
                # alpaca-py types the listing as ``list[Asset | str]``; the str arm
                # is the raw-data mode we never enable, so it is unreachable by our
                # construction and asserted rather than assumed (see _require_model).
                raw_symbol = getattr(asset, "symbol", None)
                if raw_symbol is None:
                    raise TypeError(
                        "Alpaca returned a crypto asset listing entry with no symbol "
                        f"({type(asset).__name__}); crypto position symbols cannot be "
                        "mapped without it"
                    )
                symbol = str(raw_symbol)
                symbol_map[symbol.replace("/", "")] = symbol
            self._crypto_symbols = symbol_map
        return self._crypto_symbols

    def get_asset(self, symbol: str) -> AssetInfo:  # pragma: no cover - needs the SDK
        """Look up broker-authoritative asset metadata for ``symbol`` (ADR-0028).

        An unknown ticker (the SDK's 404) is re-raised as a clear
        :class:`LookupError`; any other failure (auth, rate limit, transport)
        propagates unchanged, so the universe validator can tell "the broker says
        no" apart from "we could not ask" (see
        :func:`trading.universe.validate_universe`).
        """
        try:
            raw = self._trading.get_asset(symbol)
        except Exception as exc:  # narrowed below: only a 404 becomes LookupError
            if getattr(exc, "status_code", None) == 404:
                raise LookupError(f"unknown Alpaca asset {symbol!r}") from exc
            raise
        if raw is None:
            raise LookupError(f"unknown Alpaca asset {symbol!r}")
        return self._to_asset(symbol, _require_model(raw, "get_asset"))

    @staticmethod
    def _to_asset(symbol: str, raw: Any) -> AssetInfo:  # pragma: no cover - SDK only
        """Convert an SDK ``Asset`` model into our :class:`AssetInfo`.

        ``raw`` is untyped (alpaca-py ships no stubs), field presence varies by
        SDK version, and its enums render as e.g. ``"AssetExchange.NASDAQ"`` or
        ``"NASDAQ"``, so every field is read defensively and the exchange enum
        prefix is stripped. Missing ``tradable`` / ``fractionable`` default to
        ``False``: absent permission is not permission.

        ``min_order_size`` is crypto-only in practice (every US equity omits it)
        and is carried through as ``None`` when absent — not as ``0.0``, which
        would read as "no minimum" rather than "the venue did not say".
        """
        exchange = str(getattr(raw, "exchange", "") or "")
        min_size = getattr(raw, "min_order_size", None)
        return AssetInfo(
            symbol=str(getattr(raw, "symbol", "") or symbol),
            tradable=bool(getattr(raw, "tradable", False)),
            fractionable=bool(getattr(raw, "fractionable", False)),
            exchange=exchange.split(".")[-1] if exchange else "",
            name=str(getattr(raw, "name", "") or ""),
            shortable=bool(getattr(raw, "shortable", False)),
            min_order_size=None if min_size is None else float(min_size),
        )

    @staticmethod
    def _to_order(raw: Any) -> AlpacaOrder:
        """Convert an SDK order object into our :class:`AlpacaOrder`.

        ``raw`` is the SDK's ``Order`` model (untyped here, since alpaca-py ships
        no stubs); its enum fields render as e.g. ``"OrderSide.BUY"`` or ``"buy"``
        depending on version, so both are normalized to our lowercase values.
        """
        avg = getattr(raw, "filled_avg_price", None)
        return AlpacaOrder(
            id=str(raw.id),
            symbol=str(raw.symbol),
            qty=float(raw.qty),
            side=Side(str(raw.side).lower().removeprefix("orderside.")),
            status=str(raw.status).lower().removeprefix("orderstatus."),
            filled_qty=float(getattr(raw, "filled_qty", 0.0) or 0.0),
            filled_avg_price=float(avg) if avg is not None else None,
        )
