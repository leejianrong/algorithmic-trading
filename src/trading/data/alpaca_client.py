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
from datetime import datetime
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


# Order lifecycle statuses we use. Kept as strings (not an enum) so a real
# Alpaca status string round-trips unchanged through :class:`AlpacaOrder`.
STATUS_NEW = "new"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"


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

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
        """Submit a market order for ``qty`` shares (fractional allowed, ADR-0011).

        Long-or-flat only: a sell may not exceed the held quantity (no implicit
        shorting, ADR-0011).
        """
        ...

    def get_order(self, order_id: str) -> AlpacaOrder:
        """Fetch the current state of a previously submitted order."""
        ...

    def get_account(self) -> AccountSnapshot:
        """Current cash and equity."""
        ...

    def list_positions(self) -> list[PositionSnapshot]:
        """All open positions (empty when flat)."""
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
    """

    def __init__(
        self,
        bars: dict[str, list[Bar]] | None = None,
        *,
        cash: float = 100_000.0,
        auto_fill: bool = True,
    ) -> None:
        self._bars: dict[str, list[Bar]] = {
            symbol: sorted(series, key=lambda b: b.ts) for symbol, series in (bars or {}).items()
        }
        self._state = _FakeState(cash=cash)
        self._auto_fill = auto_fill
        self._prices: dict[str, float] = {}
        self._next_id = 1

    # -- test/setup helpers (not part of the protocol) --

    def set_price(self, symbol: str, price: float) -> None:
        """Set the price ``submit_order`` (and :meth:`fill_order`) will fill at."""
        self._prices[symbol] = price

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

    def submit_order(self, symbol: str, qty: float, side: Side) -> AlpacaOrder:
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

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=self._state.cash, equity=self._equity())

    def list_positions(self) -> list[PositionSnapshot]:
        return [pos for pos in self._state.positions.values() if abs(pos.qty) > SHARE_EPS]

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


# --- Real: the live SDK wrapper (guarded, lazy, never runs in the sandbox) -----


class RealAlpacaClient:
    """A thin :class:`AlpacaClient` over the real ``alpaca-py`` SDK.

    The SDK is an optional dependency: it is imported lazily inside ``__init__``
    and the methods, so importing this module never requires ``alpaca-py`` and a
    missing install fails only when someone constructs this client for live use
    (ADR-0018). Credentials come from ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``
    (optionally overridden by constructor arguments); ``paper`` selects the
    paper-trading endpoint. Every method converts SDK responses into our own
    :class:`~trading.types.Bar` and DTOs, so no SDK type escapes this class.
    """

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        paper: bool = True,
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
        )
        response = self._data.get_stock_bars(request)
        rows: list[Any] = response.data.get(symbol, [])
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
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side is Side.BUY else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self._to_order(self._trading.submit_order(request))

    def get_order(self, order_id: str) -> AlpacaOrder:
        return self._to_order(self._trading.get_order_by_id(order_id))

    def get_account(self) -> AccountSnapshot:
        account = self._trading.get_account()
        return AccountSnapshot(cash=float(account.cash), equity=float(account.equity))

    def list_positions(self) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                symbol=str(position.symbol),
                qty=float(position.qty),
                avg_price=float(position.avg_entry_price),
            )
            for position in self._trading.get_all_positions()
        ]

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
