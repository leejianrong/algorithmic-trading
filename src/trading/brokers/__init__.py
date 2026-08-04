"""Broker implementations behind the :class:`~trading.interfaces.Broker` seam.

``SimulatedBroker`` (in :mod:`trading.broker`) remains the backtest reference;
this package holds live-venue brokers. First up: :class:`~trading.brokers.alpaca.AlpacaBroker`,
a submit-then-poll paper broker over the Alpaca client seam (ADR-0020).
"""

from __future__ import annotations

from trading.brokers.alpaca import AlpacaBroker

__all__ = ["AlpacaBroker"]
