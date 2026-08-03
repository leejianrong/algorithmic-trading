"""Small indicator helpers for strategies. No look-ahead: these only ever see the
bars a strategy is handed, which end at the current bar (ADR-0001).
"""

from __future__ import annotations

import math
from itertools import pairwise

from trading.types import Bar


def sma(bars: list[Bar], n: int) -> float | None:
    """Simple moving average of the last ``n`` closes, or ``None`` if too few bars."""
    if n <= 0:
        raise ValueError(f"sma window must be positive, got {n}")
    if len(bars) < n:
        return None
    window = bars[-n:]
    return sum(bar.close for bar in window) / n


def rolling_std(bars: list[Bar], n: int) -> float | None:
    """Population standard deviation of the last ``n`` closes, or ``None`` if too few.

    Population (divide by ``n``, not ``n - 1``) so the value is a plain RMS spread
    around the same window's mean that :func:`sma` reports.
    """
    if n <= 0:
        raise ValueError(f"rolling_std window must be positive, got {n}")
    if len(bars) < n:
        return None
    window = bars[-n:]
    mean = sum(bar.close for bar in window) / n
    variance = sum((bar.close - mean) ** 2 for bar in window) / n
    return math.sqrt(variance)


def bollinger(bars: list[Bar], n: int, num_std: float = 2.0) -> tuple[float, float, float] | None:
    """Bollinger bands as ``(lower, mid, upper)``, or ``None`` if too few bars.

    ``mid`` is the ``n``-period :func:`sma`; the bands sit ``num_std`` population
    standard deviations either side of it.
    """
    if num_std < 0:
        raise ValueError(f"num_std must be non-negative, got {num_std}")
    mid = sma(bars, n)
    sd = rolling_std(bars, n)
    if mid is None or sd is None:
        return None
    return (mid - num_std * sd, mid, mid + num_std * sd)


def rsi(bars: list[Bar], period: int) -> float | None:
    """RSI over the last ``period`` close-to-close changes.

    Uses a simple average of gains and losses (Cutler's variant) so the value is
    a pure function of the window and easy to reason about. Needs ``period + 1``
    closes (``period`` changes); returns ``None`` when there are too few bars.
    Ranges in ``[0, 100]``: ``100`` when every change is a gain, ``0`` when every
    change is a loss, and ``50`` for a perfectly flat window.
    """
    if period <= 0:
        raise ValueError(f"rsi period must be positive, got {period}")
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1) :]
    gains = 0.0
    losses = 0.0
    for prev, cur in pairwise(window):
        delta = cur.close - prev.close
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
