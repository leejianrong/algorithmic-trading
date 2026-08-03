"""Small indicator helpers for strategies. No look-ahead: these only ever see the
bars a strategy is handed, which end at the current bar (ADR-0001).
"""

from __future__ import annotations

from trading.types import Bar


def sma(bars: list[Bar], n: int) -> float | None:
    """Simple moving average of the last ``n`` closes, or ``None`` if too few bars."""
    if n <= 0:
        raise ValueError(f"sma window must be positive, got {n}")
    if len(bars) < n:
        return None
    window = bars[-n:]
    return sum(bar.close for bar in window) / n
