"""Enforced risk guardrails: the pre-trade checker and the kill switch (ADR-0009).

Two layers sit on the shared execution path so every mode is protected equally
(ADR-0002):

1. **Pre-trade check** (:meth:`Guardrails.check`) on each order — a per-symbol
   position cap and a gross-exposure cap. An over-cap buy is *clamped* down to the
   cap rather than rejected outright (target weights routinely overshoot by a
   sliver); a clamp that collapses to nothing becomes a rejection. Sells that
   reduce a holding are exits and always pass. Cash sufficiency stays with the
   broker — the caps only keep buys within equity (ADR-0013).
2. **Portfolio monitor** (:meth:`Guardrails.halted`) each bar — a stateful
   drawdown-from-peak and optional single-bar-loss kill switch. Once tripped it
   *latches* for the session: new entries are blocked, exits still allowed.

The implementation-level choices (latching, exits-allowed-while-halted,
clamp-not-reject, cash left with the broker) are recorded in ADR-0013.

A third, opt-in layer (ADR-0015) sits on the same per-bar equity the drawdown
monitor already observes: when ``RiskConfig.target_volatility`` is set, the
monitor maintains a rolling window of portfolio returns, estimates realized
(annualized) volatility, and scales the *effective* gross-exposure cap by
``target_vol / max(realized_vol, floor)`` (clamped to a sane maximum). A calm book
is allowed more gross, a turbulent one less. When the target is unset the scale is
a constant ``1.0`` and every path behaves exactly as before.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from math import sqrt

from trading.config import RiskConfig
from trading.sizing import SHARE_PRECISION
from trading.types import SHARE_EPS, Order, Portfolio, Side

# Volatility-target tuning (ADR-0015). The window is the lookback (in bars) over
# which realized volatility is estimated; TRADING_DAYS annualizes the per-bar
# standard deviation (same 252 basis as the Sharpe metric, Q17). The floor keeps a
# near-flat book from dividing by ~zero and demanding infinite leverage, and the
# max scale caps how far the cap can be levered up when realized vol is very low.
_VOL_WINDOW = 20
_TRADING_DAYS = 252
_VOL_FLOOR = 1e-6
_MAX_VOL_SCALE = 3.0


class Guardrails:
    """Stateful implementation of the :class:`~trading.interfaces.RiskGuardrails` seam.

    The monitor carries state across bars (running equity peak, previous-bar
    equity, and the latched halt), so one instance belongs to one run. The engine
    calls :meth:`halted` once per bar to update the latch, then :meth:`check` on
    each sized order. After every :meth:`check` the caller may read
    :attr:`last_reason` for the clamp/rejection explanation, and :attr:`halt_reason`
    once the switch has tripped.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self._config = config or RiskConfig()
        self._peak: float | None = None
        self._prev_equity: float | None = None
        self._halted = False
        self.halt_reason: str | None = None
        # Set on every check() call so the engine can log the clamp/reject reason
        # (the RiskGuardrails protocol returns only Order | None).
        self.last_reason: str | None = None
        # Within-bar tally of exposure this bar's orders have already committed.
        # Same-bar orders queue and don't fill until the next bar, so the pre-trade
        # portfolio doesn't move between checks; without this, N orders each under a
        # cap would collectively breach it. Reset once per bar by halted().
        self._committed_gross = 0.0
        self._committed_qty: dict[str, float] = {}
        # Same-bar committed notional per sector, for the per-sector cap (ADR-0019).
        # Parallel to _committed_gross but keyed by sector so sibling orders in the
        # same sector see the room a prior one took. Reset per bar by halted().
        self._committed_sector: dict[str, float] = {}
        # Volatility-target state (ADR-0015): a rolling window of portfolio returns
        # and the resulting multiplier on the gross cap. The scale stays 1.0 (a
        # no-op) until a target is configured and there are enough returns to
        # estimate realized volatility, so the unset path is byte-identical.
        self._returns: deque[float] = deque(maxlen=_VOL_WINDOW)
        self._vol_scale = 1.0

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def vol_scale(self) -> float:
        """Current multiplier applied to the gross-exposure cap (ADR-0015).

        ``1.0`` when volatility targeting is off or there is not yet enough return
        history; otherwise ``target_vol / max(realized_vol, floor)`` clamped to
        ``[0, _MAX_VOL_SCALE]``. Recomputed once per bar in :meth:`halted`.
        """
        return self._vol_scale

    @property
    def is_halted(self) -> bool:
        """Whether the kill switch has latched (without re-marking the book)."""
        return self._halted

    def halted(self, portfolio: Portfolio, prices: dict[str, float]) -> bool:
        """Update the running peak / previous-bar marks and (latch and) report halt.

        Drawdown is measured from the highest equity seen so far; the daily-loss
        breaker, when configured, compares against the previous bar. The halt
        latches: once ``True`` it stays ``True`` for the rest of the session.

        The engine calls this exactly once per bar, immediately before the check
        loop, so it also **begins a new bar for the pre-trade tally**: the
        within-bar committed-exposure counters are reset here so this bar's orders
        accumulate against a clean slate (ADR-0013).
        """
        # Begin a new bar: forget last bar's committed exposure.
        self._committed_gross = 0.0
        self._committed_qty = {}
        self._committed_sector = {}

        equity = portfolio.equity(prices)
        peak = equity if self._peak is None else max(self._peak, equity)
        self._peak = peak

        # Feed the volatility-target window with this bar's portfolio return, then
        # refresh the gross-cap multiplier off the updated window (ADR-0015). Done
        # on the same equity the drawdown monitor observes so the two share one mark.
        if self._prev_equity is not None and self._prev_equity > 0:
            self._returns.append(equity / self._prev_equity - 1.0)
        self._vol_scale = self._compute_vol_scale()

        if not self._halted:
            drawdown = (peak - equity) / peak if peak > 0 else 0.0
            if drawdown >= self._config.max_drawdown_pct:
                self._halted = True
                self.halt_reason = (
                    f"drawdown {drawdown:.1%} ≥ max {self._config.max_drawdown_pct:.1%}"
                )

        if not self._halted and self._config.max_daily_loss_pct is not None:
            prev = self._prev_equity
            if prev is not None and prev > 0:
                loss = (prev - equity) / prev
                if loss >= self._config.max_daily_loss_pct:
                    self._halted = True
                    self.halt_reason = (
                        f"daily loss {loss:.1%} ≥ max {self._config.max_daily_loss_pct:.1%}"
                    )

        self._prev_equity = equity
        return self._halted

    def check(
        self,
        order: Order,
        portfolio: Portfolio,
        prices: dict[str, float],
    ) -> Order | None:
        """Return an accepted (possibly clamped) order, or ``None`` to reject.

        While halted, only exits pass; new entries and increases are rejected.
        Otherwise buys are clamped down to the tighter of the per-symbol position
        cap and the gross-exposure cap, and a clamp that leaves ~nothing to trade
        becomes a rejection. Sells are exits/reductions and pass unchanged.
        """
        self.last_reason = None
        pos = portfolio.position(order.symbol)
        is_exit = order.side is Side.SELL and pos.qty > SHARE_EPS

        # Kill switch: block new entries/increases, but never a way out.
        if self._halted and not is_exit:
            self.last_reason = self.halt_reason or "halted: new entries blocked"
            return None

        # Sells reduce exposure and can't breach a long cap; the broker still
        # guards against overselling (no implicit shorting, ADR-0011).
        if order.side is Side.SELL:
            return order

        price = prices.get(order.symbol)
        if price is None or price <= 0:
            # No usable mark; leave it to sizing/broker rather than guess a cap.
            return order

        equity = portfolio.equity(prices)
        if equity <= 0:
            self.last_reason = "non-positive equity: no new exposure"
            return None

        current_gross = sum(
            abs(p.market_value(prices[s]))
            for s, p in portfolio.positions.items()
            if abs(p.qty) > SHARE_EPS and s in prices
        )
        # Room left under each cap, in shares of this symbol — net of what this
        # bar's earlier orders have already committed (they queue, so the pre-trade
        # portfolio above doesn't yet reflect them). Equity stays the pre-trade
        # snapshot as the denominator, consistent with sizing.
        committed_qty = self._committed_qty.get(order.symbol, 0.0)
        allowed_position = self._config.max_position_pct * equity / price - pos.qty - committed_qty
        # Volatility targeting scales only the gross cap (ADR-0015); the per-symbol
        # position cap is a concentration limit and stays fixed. The scale is 1.0
        # whenever targeting is off, so this term is unchanged in that case.
        effective_gross = self._config.max_gross_exposure * self._vol_scale
        allowed_gross = (effective_gross * equity - current_gross - self._committed_gross) / price

        # Per-sector gross cap (ADR-0019): scoped to the order symbol's sector, this
        # is the gross clamp restricted to same-sector holdings. Off unless both a
        # sector map and a cap are configured, and unconstrained (room = ∞) for a
        # symbol absent from the map — different sectors never cross-limit.
        sector, committed_sector = self._sector_of(order.symbol)
        allowed_sector = float("inf")
        if sector is not None and self._config.max_sector_exposure is not None:
            current_sector = sum(
                abs(p.market_value(prices[s]))
                for s, p in portfolio.positions.items()
                if abs(p.qty) > SHARE_EPS and s in prices and self._sector_of(s)[0] == sector
            )
            allowed_sector = (
                self._config.max_sector_exposure * equity - current_sector - committed_sector
            ) / price

        allowed = min(order.qty, allowed_position, allowed_gross, allowed_sector)
        binding = self._binding(allowed_position, allowed_gross, allowed_sector, sector)

        # Round to a placeable share count first: a positive `allowed` that rounds
        # to zero (a clamp finer than share precision) is a rejection, not a
        # zero-qty Order, which the type forbids.
        accepted_qty = round(allowed, SHARE_PRECISION)
        if accepted_qty <= SHARE_EPS:
            self.last_reason = f"rejected: {binding}"
            return None

        if accepted_qty < order.qty - SHARE_EPS:
            self.last_reason = f"clamped {order.qty:.6f}→{accepted_qty:.6f}: {binding}"
            result = replace(order, qty=accepted_qty)
        else:
            result = order

        # Commit the approved notional so later same-bar orders see less room.
        self._committed_gross += accepted_qty * price
        self._committed_qty[order.symbol] = committed_qty + accepted_qty
        if sector is not None:
            self._committed_sector[sector] = committed_sector + accepted_qty * price
        return result

    def _sector_of(self, symbol: str) -> tuple[str | None, float]:
        """The symbol's sector (or ``None`` if unmapped/off) and its committed tally.

        Returns ``(None, 0.0)`` when no sector map is configured or the symbol is
        absent from it — such a symbol is unconstrained by the per-sector cap.
        """
        sector_map = self._config.sector_map
        if sector_map is None:
            return None, 0.0
        sector = sector_map.get(symbol)
        if sector is None:
            return None, 0.0
        return sector, self._committed_sector.get(sector, 0.0)

    def _compute_vol_scale(self) -> float:
        """The gross-cap multiplier from realized vs. target volatility (ADR-0015).

        Returns ``1.0`` (a no-op) when targeting is off or fewer than two returns
        are in the window. Otherwise estimates realized volatility as the sample
        standard deviation of the windowed returns annualized by ``√252``, and
        returns ``target_vol / max(realized_vol, floor)`` clamped to
        ``[0, _MAX_VOL_SCALE]`` so a calm book earns more gross and a turbulent one
        less, without ever demanding unbounded leverage.
        """
        target = self._config.target_volatility
        if target is None or len(self._returns) < 2:
            return 1.0
        n = len(self._returns)
        mean = sum(self._returns) / n
        variance = sum((r - mean) ** 2 for r in self._returns) / (n - 1)
        realized_vol = sqrt(variance) * sqrt(_TRADING_DAYS)
        scale = target / max(realized_vol, _VOL_FLOOR)
        return min(scale, _MAX_VOL_SCALE)

    def _binding(
        self,
        allowed_position: float,
        allowed_gross: float,
        allowed_sector: float = float("inf"),
        sector: str | None = None,
    ) -> str:
        """Name the cap that bound (the one leaving the least room).

        The per-sector cap (ADR-0019) is reported when it is the tightest; it is
        ``∞`` (never binding) whenever the feature is off, so the position/gross
        wording is byte-identical in that case.
        """
        sector_is_tightest = (
            sector is not None
            and allowed_sector <= allowed_position
            and allowed_sector <= allowed_gross
        )
        if sector_is_tightest:
            pct = self._config.max_sector_exposure or 0.0
            return f"sector cap {pct:.0%} ({sector})"
        if allowed_position <= allowed_gross:
            return f"position cap {self._config.max_position_pct:.0%}"
        return f"gross exposure cap {self._config.max_gross_exposure:.0%}"
