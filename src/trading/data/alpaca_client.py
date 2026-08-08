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
from datetime import datetime, timedelta
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
    """

    symbol: str
    tradable: bool
    fractionable: bool
    exchange: str = ""
    name: str = ""
    shortable: bool = False

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("AssetInfo.symbol must be a non-empty ticker")
        if self.symbol.strip() != self.symbol or " " in self.symbol:
            raise ValueError(f"AssetInfo.symbol must not contain whitespace, got {self.symbol!r}")


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
    """

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        paper: bool = True,
        feed: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("ALPACA_API_KEY")
        secret = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise ValueError(
                "Alpaca credentials required: set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY (or pass them explicitly)"
            )
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - alpaca-py not installed here
            raise ImportError(
                "alpaca-py is required for live trading; pip install alpaca-py"
            ) from exc
        self._data = StockHistoricalDataClient(key, secret)
        self._trading = TradingClient(key, secret, paper=paper)
        self._feed = feed

    @property
    def feed(self) -> str | None:
        """The market-data feed bar requests use (``None`` = the SDK default tape)."""
        return self._feed

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
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL if adjusted else Adjustment.RAW,
            **self._feed_kwargs(),
        )
        try:
            response = self._data.get_stock_bars(request)
        except Exception as exc:
            raise _classify_data_error(exc, symbol, self._feed) from exc
        barset = _require_model(response, "daily bars")
        rows: list[Any] = barset.data.get(symbol, [])
        return self._rows_to_bars(symbol, rows)

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
        interval: timedelta = timedelta(days=1),
    ) -> list[Bar]:  # pragma: no cover - needs the alpaca-py SDK and the network
        from alpaca.data.enums import Adjustment
        from alpaca.data.requests import StockBarsRequest

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=self._to_timeframe(interval),
            start=start,
            end=end,
            adjustment=Adjustment.ALL if adjusted else Adjustment.RAW,
            **self._feed_kwargs(),
        )
        try:
            response = self._data.get_stock_bars(request)
        except Exception as exc:
            raise _classify_data_error(exc, symbol, self._feed) from exc
        barset = _require_model(response, "intraday bars")
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
    def _rows_to_bars(symbol: str, rows: list[Any]) -> list[Bar]:  # pragma: no cover - SDK only
        """Convert SDK bar rows into our :class:`~trading.types.Bar`, ascending by time."""
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

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        """Place a market order, or raise :class:`OrderRejectedError` if refused.

        A venue refusal is *classified*, not leaked: without this the SDK's
        ``APIError`` travelled out of the seam, out of ``AlpacaBroker.submit`` and
        out of a live session, taking the run's artifacts with it (ADR-0041).
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side is Side.BUY else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
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
        """
        try:
            self._trading.cancel_order_by_id(order_id)
        except Exception as exc:  # narrowed below: only a 404 becomes LookupError
            if getattr(exc, "status_code", None) == 404:
                raise LookupError(f"unknown Alpaca order {order_id!r}") from exc
            raise

    def get_account(self) -> AccountSnapshot:
        account = _require_model(self._trading.get_account(), "get_account")
        return AccountSnapshot(
            cash=_require_float(account.cash, "TradeAccount.cash"),
            equity=_require_float(account.equity, "TradeAccount.equity"),
        )

    def list_positions(self) -> list[PositionSnapshot]:
        positions = _require_model(self._trading.get_all_positions(), "get_all_positions")
        return [
            PositionSnapshot(
                symbol=str(position.symbol),
                qty=_require_float(position.qty, "Position.qty"),
                avg_price=_require_float(position.avg_entry_price, "Position.avg_entry_price"),
            )
            for position in positions
        ]

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
        """
        exchange = str(getattr(raw, "exchange", "") or "")
        return AssetInfo(
            symbol=str(getattr(raw, "symbol", "") or symbol),
            tradable=bool(getattr(raw, "tradable", False)),
            fractionable=bool(getattr(raw, "fractionable", False)),
            exchange=exchange.split(".")[-1] if exchange else "",
            name=str(getattr(raw, "name", "") or ""),
            shortable=bool(getattr(raw, "shortable", False)),
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
