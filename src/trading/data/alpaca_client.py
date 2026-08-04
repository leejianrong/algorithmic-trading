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
# Alpaca status string round-trips unchanged through :class:`AlpacaOrder`.
STATUS_NEW = "new"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"

# Exchange string :class:`FakeAlpacaClient` stamps on the assets it invents. It is
# a placeholder, not a claim about where a symbol really lists.
_FAKE_EXCHANGE = "FAKE"


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
        )
        response = self._data.get_stock_bars(request)
        rows: list[Any] = response.data.get(symbol, [])
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
        return self._to_asset(symbol, raw)

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
