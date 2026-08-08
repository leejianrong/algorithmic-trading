"""Alpaca-backed :class:`~trading.interfaces.DataAdapter` over the client seam.

A thin adapter that turns the :class:`~trading.data.alpaca_client.AlpacaClient`
seam (ADR-0017) into a :class:`~trading.interfaces.DataAdapter`: ``get_bars``
delegates to :meth:`AlpacaClient.get_daily_bars`. The ``adjusted`` notion is a
**per-mode policy carried by the feed**, not the adapter (ADR-0021): the backtest
feed asks for adjusted total-return prices (ADR-0008), while the paper/live feed
asks for RAW actual quotes so the strategy decides and marks in the same dollars
the :class:`~trading.brokers.alpaca.AlpacaBroker` reconciles from the real
account. So the per-call ``adjusted`` keyword controls each fetch; the constructor
``adjusted`` param only supplies the default used when a caller omits the keyword.
The seam already serves raw via ``Adjustment.RAW``.

Since ADR-0045 an **adjusted** fetch is also verified: Alpaca's ``adjustment=all``
is not self-verifying, and on 2026-08-09 it served AAPL's 2020-08-31 bars with the
4:1 split not backed out — a bare -74.15% cliff inside a series that claims to
have none, i.e. exactly the phantom-split hazard ADR-0008 exists to prevent. The
check is cross-referenced against Alpaca's own corporate-actions record, so it
names a specific symbol and ex-date rather than refusing the provider wholesale,
and it stops firing the day the provider is fixed. RAW fetches are never verified
and never cost an extra request — an unapplied split is not a defect in a raw
series, it is what raw *means*.

Following the injectable-dependency pattern (dev-playbook seam), the client is
constructor-injected -- the fast test layer passes a
:class:`~trading.data.alpaca_client.FakeAlpacaClient`, so no test needs the
network, a key, or the SDK. When no client is supplied, a
:class:`~trading.data.alpaca_client.RealAlpacaClient` is built lazily and its
import is deferred, so importing this module never requires credentials or
``alpaca-py`` (ADR-0018).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

from trading.data.alpaca_client import AlpacaClient, SplitEvent
from trading.types import Bar

logger = logging.getLogger(__name__)

# How far the measured adjustment factor may sit from the split's own ratio (or
# from 1.0) before the verdict is "neither, and we cannot say". The measurement
# divides out the stock's price move exactly (see `_verify_splits_applied`), so
# the only residue is rounding in the provider's own two-decimal closes plus any
# same-day dividend adjustment -- both well under a percent. The two outcomes it
# discriminates between differ by the split ratio itself (>= 1.5x by the filter
# below), so this tolerance is nowhere near either boundary.
_FACTOR_TOLERANCE = 0.02

# Splits too small to measure reliably against two-decimal closes are skipped
# rather than guessed at. A 4:1 (ratio 4.0) or 1:10 (ratio 0.1) is unmistakable;
# a hypothetical 1.05:1 would not be, and a false accusation is worse than a
# missed one here -- the phantom crashes that motivate the guard are all large.
_MIN_MEANINGFUL_RATIO = 1.5


class UnadjustedSplitError(RuntimeError):
    """Alpaca served an *adjusted* series that still carries an unadjusted split.

    A classified provider-data defect, in the spirit of
    :class:`~trading.data.alpaca_client.DataSubscriptionError` (ADR-0034) and
    :class:`~trading.data.alpaca_client.OrderRejectedError` (ADR-0041): the
    request was well formed, authenticated and answered, and the answer is wrong
    in a specific, named way. Raised instead of returning the bars because a
    phantom split is not a cosmetic blemish -- it is a ~-75% single-day return
    that wrecks Sharpe and max drawdown and can trip the ADR-0013 drawdown kill
    switch on a corporate action that never happened (ADR-0008, ADR-0045).

    Never raised for a RAW fetch: raw prices are *supposed* to carry the split
    cliff, so the paper/live feed (ADR-0021) can never see this.
    """


class AlpacaAdapter:
    """Serves adjusted bars from Alpaca through the client seam.

    The bar cadence is fixed at construction by ``interval`` (default one day, the
    original daily behaviour). A daily interval routes to
    :meth:`AlpacaClient.get_daily_bars` so the daily path is byte-identical to
    before; a sub-daily interval routes to the interval-aware
    :meth:`AlpacaClient.get_bars` (ADR-0022). Either way the returned bars satisfy
    the daily-shaped :class:`~trading.interfaces.DataAdapter` protocol — the
    interval is an adapter property, never a ``get_bars`` argument.

    ``feed`` selects the market-data tape for a client this adapter builds itself
    (ADR-0034); it is rejected alongside an injected ``client``, which carries its
    own feed. ``None`` keeps the SDK's consolidated-SIP default, which is what a
    historical backtest wants; the live paper feed passes ``"iex"`` because a free
    data plan refuses recent SIP bars.

    ``verify_adjustments`` (default on) cross-checks every **adjusted** fetch
    against Alpaca's corporate-actions record and refuses a series whose splits
    were not applied (ADR-0045). It is the documented escape hatch for an operator
    who has read the ADR and wants the raw-scaled series anyway — deliberately a
    constructor parameter rather than a CLI flag, because "give me prices I have
    been told are wrong" should cost a line of Python, not a flag someone copies
    out of a runbook.
    """

    def __init__(
        self,
        client: AlpacaClient | None = None,
        *,
        adjusted: bool = True,
        interval: timedelta = timedelta(days=1),
        feed: str | None = None,
        verify_adjustments: bool = True,
    ) -> None:
        if client is None:
            from trading.data.alpaca_client import RealAlpacaClient

            client = RealAlpacaClient(feed=feed)
        elif feed is not None:
            raise ValueError("feed applies only when AlpacaAdapter builds its own client")
        self._client = client
        self._adjusted = adjusted
        self._interval = interval
        self._verify_adjustments = verify_adjustments
        # (symbol, start-date, end-date) -> splits, or None when the lookup failed.
        # A backtest fetches each symbol once, but the ADV screen (ADR-0029), the
        # benchmark leg and a --once paper replay all re-enter, so one corporate-
        # actions request per window rather than per call.
        self._split_cache: dict[tuple[str, date, date], list[SplitEvent] | None] = {}

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool | None = None,
    ) -> list[Bar]:
        """Return ``symbol``'s bars in ``[start, end]``, ascending by time.

        The per-call ``adjusted`` keyword controls the fetch (ADR-0021): pass
        ``True`` for split/dividend-adjusted total-return prices (the backtest
        feed) or ``False`` for RAW actual quotes (the paper/live feed); when it is
        omitted the constructor default applies. The construction-time ``interval``
        selects the cadence (ADR-0022): a daily interval uses ``get_daily_bars``
        (byte-identical to before); a sub-daily one uses the interval-aware
        ``get_bars``. Either seam already returns ascending, range-filtered bars;
        we re-filter and sort here defensively (both cheap).

        An adjusted fetch is verified before it is returned and raises
        :class:`UnadjustedSplitError` if the provider left a split in it
        (ADR-0045). A raw fetch returns exactly as it always did.
        """
        effective = self._adjusted if adjusted is None else adjusted
        filtered = self._fetch(symbol, start, end, adjusted=effective)
        if effective and self._verify_adjustments:
            self._verify_splits_applied(symbol, filtered, start, end)
        return filtered

    def _fetch(self, symbol: str, start: datetime, end: datetime, *, adjusted: bool) -> list[Bar]:
        """One trip to the seam, range-filtered and sorted."""
        if self._interval >= timedelta(days=1):
            bars = self._client.get_daily_bars(symbol, start, end, adjusted=adjusted)
        else:
            bars = self._client.get_bars(
                symbol, start, end, adjusted=adjusted, interval=self._interval
            )
        filtered = [b for b in bars if start <= b.ts <= end]
        filtered.sort(key=lambda b: b.ts)
        return filtered

    def _splits_in(self, symbol: str, start: datetime, end: datetime) -> list[SplitEvent] | None:
        """Splits with an ex-date in the window, or ``None`` if we could not ask."""
        key = (symbol, start.date(), end.date())
        if key not in self._split_cache:
            try:
                self._split_cache[key] = self._client.get_splits(symbol, start, end)
            except Exception as exc:
                logger.warning(
                    "could not verify split adjustments for %s over [%s, %s]: the "
                    "corporate-actions lookup failed (%s). The bars are returned "
                    "unverified — a failed lookup is not evidence the data is wrong "
                    "(ADR-0045).",
                    symbol,
                    start.date(),
                    end.date(),
                    exc,
                )
                self._split_cache[key] = None
        return self._split_cache[key]

    def _verify_splits_applied(
        self, symbol: str, bars: list[Bar], start: datetime, end: datetime
    ) -> None:
        """Raise if an in-window split is missing from this adjusted series (ADR-0045).

        The measurement is exact arithmetic, not a shape heuristic, which is what
        makes it safe to act on. For any bar, ``factor = raw_close /
        adjusted_close`` is the cumulative adjustment the provider claims to have
        applied at that point. Across a split's ex-date the *ratio of factors*
        must equal the split ratio if the split was applied, and 1.0 if it was
        not — and because raw and adjusted closes both contain the stock's own
        move that day, the move divides out entirely. A -30% ex-date and a +30%
        ex-date give the same answer; the two verdicts are separated by the split
        ratio itself.

        Measured live on 2026-08-09 (paper plan, ``Adjustment.ALL`` vs
        ``Adjustment.RAW``): AAPL 4:1 2020-08-31 -> 1.0000 (**not applied**), while
        TSLA 5:1 2020-08-31 -> 5.0001, NVDA 10:1 2024-06-10 -> 10.0000, GOOGL 20:1
        2022-07-18 -> 19.9988 and CMG 50:1 2024-06-26 -> 50.0006 are all applied.
        The defect is one symbol's data, not the provider's pipeline, which is
        exactly why this refuses per symbol and per window instead of refusing
        ``--source alpaca`` outright.

        Only a split with bars on **both sides** inside the returned series can put
        a cliff *in* that series: a window that starts at or after the ex-date is
        uniformly post-split, and a uniform rescaling changes no return. A split
        with no straddling pair is therefore skipped, not assumed bad.
        """
        splits = self._splits_in(symbol, start, end)
        if not splits or len(bars) < 2:
            return
        straddling = [
            (split, pair)
            for split in splits
            if _is_measurable(split.ratio)
            for pair in [_straddle(bars, split.ex_date)]
            if pair is not None
        ]
        if not straddling:
            return

        raw_by_ts = {bar.ts: bar for bar in self._raw_for_cross_check(symbol, start, end) or []}
        if not raw_by_ts:
            return
        adjusted_by_ts = {bar.ts: bar for bar in bars}

        for split, (before, on_or_after) in straddling:
            factors: list[float] = []
            for bar in (before, on_or_after):
                raw = raw_by_ts.get(bar.ts)
                adj = adjusted_by_ts.get(bar.ts)
                if raw is None or adj is None or adj.close <= 0.0:
                    factors = []
                    break
                factors.append(raw.close / adj.close)
            if len(factors) != 2 or factors[1] <= 0.0:
                continue
            applied = factors[0] / factors[1]
            if abs(applied - 1.0) > _FACTOR_TOLERANCE:
                continue  # the split *is* backed out (or something else moved) — fine
            raise UnadjustedSplitError(
                f"Alpaca returned an adjusted series for {symbol!r} that still "
                f"carries its {_describe(split.ratio)} split of {split.ex_date} "
                f"(adjusted {before.close:g} -> {on_or_after.close:g}, a "
                f"{on_or_after.close / before.close - 1:+.1%} phantom move; the "
                f"raw/adjusted factor is unchanged at {factors[0]:.4f} across the "
                f"ex-date, so no split adjustment was applied). Alpaca's own "
                f"corporate-actions record reports the split, so its bars and its "
                f"corporate actions disagree — this is a provider defect, not a "
                f"real corporate action. Backtesting on it violates ADR-0008 and "
                f"produces a phantom ~{on_or_after.close / before.close - 1:+.0%} "
                f"day that wrecks Sharpe and drawdown. Use --source yfinance for "
                f"adjusted history, or pass AlpacaAdapter(verify_adjustments=False) "
                f"if you have read ADR-0045 and want the unadjusted series anyway."
            )

    def _raw_for_cross_check(self, symbol: str, start: datetime, end: datetime) -> list[Bar] | None:
        """The RAW series for the same window, or ``None`` if we could not fetch it.

        The second (and last) extra request the guard can cost, and it is only
        paid when a meaningful split really does straddle bars in the window —
        which for almost every backtest window is never.
        """
        try:
            return self._fetch(symbol, start, end, adjusted=False)
        except Exception as exc:
            logger.warning(
                "could not verify split adjustments for %s over [%s, %s]: the raw "
                "cross-check fetch failed (%s). The adjusted bars are returned "
                "unverified (ADR-0045).",
                symbol,
                start.date(),
                end.date(),
                exc,
            )
            return None


def _is_measurable(ratio: float) -> bool:
    """Whether a split is big enough to read off two-decimal closes.

    Measured in log space so a 4:1 (4.0) and a 1:4 (0.25) count as equally large.
    """
    return abs(math.log(ratio)) >= math.log(_MIN_MEANINGFUL_RATIO)


def _describe(ratio: float) -> str:
    """Render a split ratio the way a human writes it: ``4:1``, ``1:10``."""
    if ratio >= 1.0:
        return f"{ratio:g}:1"
    return f"1:{1 / ratio:g}"


def _straddle(bars: list[Bar], ex_date: date) -> tuple[Bar, Bar] | None:
    """The last bar before ``ex_date`` and the first on-or-after it, if both exist.

    Uses on-or-after rather than exactly-on so a holiday, a data gap, or an
    intraday cadence cannot make the check silently vacuous.
    """
    before = [bar for bar in bars if bar.ts.date() < ex_date]
    after = [bar for bar in bars if bar.ts.date() >= ex_date]
    if not before or not after:
        return None
    return before[-1], after[0]
