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
"""

from __future__ import annotations

from dataclasses import replace

from trading.config import RiskConfig
from trading.sizing import SHARE_PRECISION
from trading.types import SHARE_EPS, Order, Portfolio, Side


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

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def is_halted(self) -> bool:
        """Whether the kill switch has latched (without re-marking the book)."""
        return self._halted

    def halted(self, portfolio: Portfolio, prices: dict[str, float]) -> bool:
        """Update the running peak / previous-bar marks and (latch and) report halt.

        Drawdown is measured from the highest equity seen so far; the daily-loss
        breaker, when configured, compares against the previous bar. The halt
        latches: once ``True`` it stays ``True`` for the rest of the session.
        """
        equity = portfolio.equity(prices)
        peak = equity if self._peak is None else max(self._peak, equity)
        self._peak = peak

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
        # Room left under each cap, in shares of this symbol.
        allowed_position = self._config.max_position_pct * equity / price - pos.qty
        allowed_gross = (self._config.max_gross_exposure * equity - current_gross) / price
        allowed = min(order.qty, allowed_position, allowed_gross)

        if allowed <= SHARE_EPS:
            self.last_reason = f"rejected: {self._binding(allowed_position, allowed_gross)}"
            return None

        if allowed < order.qty - SHARE_EPS:
            clamped = round(allowed, SHARE_PRECISION)
            self.last_reason = (
                f"clamped {order.qty:.6f}→{clamped:.6f}: "
                f"{self._binding(allowed_position, allowed_gross)}"
            )
            return replace(order, qty=clamped)

        return order

    def _binding(self, allowed_position: float, allowed_gross: float) -> str:
        """Name the cap that bound (the one leaving the least room)."""
        if allowed_position <= allowed_gross:
            return f"position cap {self._config.max_position_pct:.0%}"
        return f"gross exposure cap {self._config.max_gross_exposure:.0%}"
