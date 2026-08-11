"""Deterministic synthetic price data — an offline :class:`DataAdapter` (ADR-0012).

Generates geometric-Brownian-motion bars so the engine, strategies, and CLI can be
exercised end to end without a network or a real provider. There are no corporate
actions to model, so raw == adjusted (ADR-0021): the per-call ``adjusted`` flag does
not change the numbers, and the same series drives both the adjusted backtest feed
(ADR-0008) and the raw paper feed.

**Range independence (ADR-0030).** There is exactly one canonical series per
``(symbol, seed, params, frequency)``, anchored at a fixed :data:`EPOCH`, and
``get_bars`` returns a *slice* of it. A bar is a pure function of the symbol and the
bar's absolute position in time — never of the window the caller asked for — so two
overlapping requests agree bar-for-bar on every timestamp they share. The original
generator reseeded per call and walked forward from the requested ``start``, which
made every span replay the same path from its own first bar; a walk-forward's
in-sample and out-of-sample spans came back byte-identical, and a sub-range
disagreed with its parent on every shared bar.

Positional draws. Each bar's random numbers come from a counter-based stream: a
``blake2b`` digest of ``(symbol, seed, stream name, index)`` turned into uniforms
and then into standard normals by Box-Muller. Nothing depends on call order, on a
global RNG, on the wall clock, or on ``PYTHONHASHSEED`` — only on the tuple in the
key. Prices still compound (``close[i]`` grows out of ``close[i-1]``), so the level
at bar *i* needs the cumulative log return from :data:`EPOCH` to *i*: that walk is
``O(bars from the epoch)``, memoized per symbol on the adapter instance. It costs
about 250 steps per calendar year (roughly 9,000 for a request in 2026), which is
milliseconds; nothing hides it, and there is no cheaper honest way to keep the walk
coherent. Bars before :data:`EPOCH` do not exist — a request reaching further back
is clipped, which is also what makes the paper feed's ``datetime.min`` poll window
(``data/recent_window.py``) cheap instead of a walk from year 1.

The bar cadence is a construction-time :class:`~trading.frequency.Frequency`
(default :data:`~trading.frequency.DAILY`, ADR-0022). Daily bars are stamped at
midnight UTC, one per weekday. Intraday bars are stamped at their START time
(ADR-0022 convention) and spaced by the interval across a nominal regular session —
13:30-20:00 UTC (9:30-16:00 ET) — for each trading weekday in the range. The daily
series is the backbone at every frequency: one session's intraday path is a
Brownian *bridge* between the previous daily close and that session's daily close,
so the last intraday bar of a session closes exactly on the daily bar's close and an
intraday run annualizes to the same shape as the daily one. That also bounds the
walk: intraday costs ``O(days from the epoch)``, not ``O(minutes)``.

**Which market (ADR-0056).** Everything above describes one market's shape — a
weekday session — and it was the only shape until KAN-830. The generator now emits
two, chosen by the :class:`~trading.calendar.MarketCalendar` the construction-time
``Frequency`` already carries (ADR-0054), so there is no second switch to disagree
with the annualization basis and "24/7 bars annualized on 252 days" is
unrepresentable. :data:`~trading.calendar.US_EQUITY` is the default and its series
is byte-identical to before. :data:`~trading.calendar.CRYPTO_24_7` — any calendar
whose ``is_continuous`` holds — trades **every calendar day**, and its intraday grid
steps the whole 1440-minute day from UTC midnight with no overnight gap (ADR-0053's
convention: a 24/7 daily bar is a rolling 24-hour window closing at UTC midnight).
The bridge and the daily backbone are unchanged in kind; the day they span is longer
and there are more of them per year. A calendar that is neither shape is **refused
at construction** rather than silently emitting 6.5-hour days.

Two consequences worth stating. The GBM scaling follows the calendar too — a bar's
sigma is ``annual_vol / sqrt(days_per_year)``, so a 365-day year divides by 365, not
252, and a continuous series realizes the volatility it was configured with. And a
continuous day is a *different counting function* on the same :data:`EPOCH`:
calendar days rather than weekdays. ADR-0030's invariant is unchanged — a bar is a
pure function of its absolute position — but the position is per-market, which is
also why two calendars are two canonical series (the calendar is part of a
``Frequency``'s identity).

**This is a fixture, not a venue.** It is deliberately less faithful than a real
crypto provider in ways ``tests/unit/test_synthetic_247.py`` pins rather than
hides: a pre-:data:`EPOCH` start is *clipped* (so this cannot stand in for
ADR-0047's empty-response behaviour), there is no inception date, no maintenance
window, and it is still GBM — no fat tails, which is a sharper caveat for a market
that has 20% days than for the one ADR-0012 wrote it about.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from itertools import groupby
from typing import TYPE_CHECKING

from trading.calendar import US_EQUITY, MarketCalendar
from trading.frequency import DAILY, Frequency
from trading.types import Bar

if TYPE_CHECKING:
    from collections.abc import Iterator

# The fixed anchor of every synthetic series: bar 0 of the canonical daily walk
# (ADR-0030). 1990-01-01 is a Monday, so the epoch is itself a trading day. It
# predates any plausible offline scenario; earlier requests are clipped to it.
EPOCH = datetime(1990, 1, 1, tzinfo=UTC)

# Nominal regular US-equity cash session in UTC: 13:30-20:00 = 9:30-16:00 ET.
# Intraday bars start at SESSION_OPEN and step by the interval while strictly
# before SESSION_CLOSE (a bar whose start reaches the close is not emitted).
_SESSION_OPEN = time(13, 30, tzinfo=UTC)
_SESSION_CLOSE = time(20, 0, tzinfo=UTC)
_SESSION_LENGTH = timedelta(
    hours=_SESSION_CLOSE.hour - _SESSION_OPEN.hour,
    minutes=_SESSION_CLOSE.minute - _SESSION_OPEN.minute,
)

# A continuous market's "session" is the whole calendar day, opening at UTC
# midnight — ADR-0053's convention, so the daily bar and the intraday grid it
# carries share one anchor and the last intraday bar's window ends exactly where
# the next daily bar begins (ADR-0056).
_CONTINUOUS_OPEN = time(0, 0, tzinfo=UTC)
_ONE_DAY = timedelta(days=1)

# Counter-based draw plumbing: 64-bit words out of blake2b, uniforms in (0, 1).
_WORD_BYTES = 8
_WORDS_PER_BLOCK = 8  # 64-byte blake2b digest
_UINT64_SCALE = 1.0 / float(1 << 64)
_TWO_PI = 2.0 * math.pi

# Daily returns drawn per key: one digest yields eight words = four normals, and the
# epoch walk wants every one of them (see ``_cumulative_through``).
_RETURNS_PER_KEY = 4


@dataclass(frozen=True, slots=True)
class SyntheticParams:
    """Shape of the generated series (annualized drift/vol, base price/volume).

    ``base_price`` is the level at :data:`EPOCH`, not at the start of whatever range
    you request (ADR-0030): a series that has compounded ``annual_drift`` since 1990
    trades well above it in the 2020s. That is the price of one canonical series —
    fractional shares (ADR-0011) make the absolute level irrelevant to sizing.
    """

    base_price: float = 100.0
    annual_drift: float = 0.08  # ~8%/yr expected return
    annual_vol: float = 0.20  # 20%/yr volatility
    base_volume: int = 1_000_000

    def __post_init__(self) -> None:
        if self.base_price <= 0:
            raise ValueError("base_price must be positive")
        if self.annual_vol < 0:
            raise ValueError("annual_vol must be non-negative")
        if self.base_volume <= 0:
            raise ValueError("base_volume must be positive")


def _uniforms(key: bytes, count: int) -> list[float]:
    """``count`` uniforms in (0, 1), a pure function of ``key`` (ADR-0030).

    Words come from ``blake2b`` digests of ``key`` chained with a block counter, so
    any number of draws is available from one key and each word's value depends only
    on the key and its own position. ``blake2b`` is the right hash for this: it is
    fixed by its specification, so the same key yields the same bytes on every
    platform and Python version — unlike the builtin ``hash()``, which is salted per
    process. ``+ 0.5`` keeps the result strictly inside (0, 1) so ``log(u)`` is
    always finite.
    """
    words: list[int] = []
    block = 0
    while len(words) < count:
        digest = hashlib.blake2b(
            key + b"#" + block.to_bytes(4, "big"),
            digest_size=_WORD_BYTES * _WORDS_PER_BLOCK,
        ).digest()
        words.extend(struct.unpack(f">{_WORDS_PER_BLOCK}Q", digest))
        block += 1
    return [(word + 0.5) * _UINT64_SCALE for word in words[:count]]


def _box_muller(first: float, second: float) -> tuple[float, float]:
    """Two independent standard normals from two uniforms in (0, 1).

    The textbook polar transform: ``sqrt(-2 ln u1)`` is the radius and ``2 pi u2``
    the angle, so ``(r cos t, r sin t)`` is an independent standard-normal pair.
    Hand-rolled because a per-bar :class:`random.Random` costs ~16 us against
    ~2 us here, and the epoch walk draws thousands of bars per symbol (ADR-0030) —
    guarded by a distribution test, since a transposed term would skew every price.
    """
    radius = math.sqrt(-2.0 * math.log(first))
    angle = _TWO_PI * second
    return radius * math.cos(angle), radius * math.sin(angle)


def _standard_normals(key: bytes, count: int) -> list[float]:
    """``count`` standard normals from the counter-based stream keyed by ``key``.

    Exactly Gaussian, and — unlike drawing from a sequential
    :class:`random.Random` — a pure function of ``key``, so a bar's draws never
    depend on how many bars were generated before it.
    """
    pairs = (count + 1) // 2
    uniforms = _uniforms(key, 2 * pairs)
    normals: list[float] = []
    for index in range(pairs):
        normals.extend(_box_muller(uniforms[2 * index], uniforms[2 * index + 1]))
    return normals[:count]


def _stream_key(symbol: str, seed: int, stream: str, index: int) -> bytes:
    """A stable key for one positional draw: symbol, seed, stream name, index.

    Stringly-typed on purpose — the key is the whole determinism contract, so it is
    readable in a debugger and independent of ``PYTHONHASHSEED`` (ADR-0012).
    """
    return f"{symbol}:{seed}:{stream}:{index}".encode()


def _trading_days(start: datetime, end: datetime) -> Iterator[datetime]:
    """Weekday timestamps (Mon-Fri) at midnight UTC in ``[start, end]``."""
    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    last = datetime(end.year, end.month, end.day, tzinfo=UTC)
    while day <= last:
        if day.weekday() < 5:  # skip Sat/Sun; holidays aren't modeled
            yield day
        day += timedelta(days=1)


def _calendar_days(start: datetime, end: datetime) -> Iterator[datetime]:
    """Every day at midnight UTC in ``[start, end]`` — a market that never closes.

    The continuous counterpart of :func:`_trading_days` (ADR-0056). No weekday
    filter, and no holiday filter either, because a 24/7 venue has neither.
    """
    day = datetime(start.year, start.month, start.day, tzinfo=UTC)
    last = datetime(end.year, end.month, end.day, tzinfo=UTC)
    while day <= last:
        yield day
        day += _ONE_DAY


def _session_index(day: datetime) -> int:
    """Absolute index of the trading session ``day``, counted from :data:`EPOCH`.

    The position that makes a bar range-independent (ADR-0030): the number of
    weekdays in ``[EPOCH, day)``, computed in closed form rather than by walking, so
    a distant range does not pay for the timestamps before it. :data:`EPOCH` is a
    Monday, so a full week contributes 5. A weekend ``day`` yields the count of
    weekdays before it (weekends emit no bar). Callers clip ``day`` to the epoch.
    """
    weeks, remainder = divmod((day - EPOCH).days, 7)
    return weeks * 5 + min(remainder, 5)


def _calendar_day_index(day: datetime) -> int:
    """Absolute index of ``day`` on a continuous market, counted from :data:`EPOCH`.

    The same role :func:`_session_index` plays for a session market — a bar's
    position, in closed form — on a *different counting function*: every calendar
    day advances the series by one, so no timestamp is skipped and no two share an
    index. ADR-0030's invariant is unchanged and only the counting is per-market,
    which is why the calendar is part of the canonical series' key (ADR-0056).
    """
    return (day - EPOCH).days


def _day_shape(calendar: MarketCalendar) -> tuple[time, timedelta]:
    """The ``(open, length)`` of one trading day on ``calendar`` (ADR-0056).

    Two shapes, and only two. A continuous market's day opens at UTC midnight and
    lasts the full 24 hours (ADR-0053). Anything else is emitted on the nominal
    US-equity session grid, which is what ``minutes_per_day == 390`` means here.

    A calendar that is neither is **refused** rather than quietly given 6.5-hour
    days while annualizing on its own minutes — a 24-hour weekday-only market, say.
    :func:`~trading.calendar.get_calendar` raises rather than falling back to equity
    for the same reason (ADR-0054): a silent equity default *is* the bug. The
    limitation this exposes is real and named in ADR-0056 — ``MarketCalendar``
    carries no opening time, so a non-continuous market cannot be given one here.
    """
    if calendar.is_continuous:
        return _CONTINUOUS_OPEN, _ONE_DAY
    if calendar.minutes_per_day == US_EQUITY.minutes_per_day:
        return _SESSION_OPEN, _SESSION_LENGTH
    raise ValueError(
        f"SyntheticAdapter models two day shapes — a {US_EQUITY.minutes_per_day:.0f}-minute "
        f"session (like {US_EQUITY.name}) and a continuous 24-hour day (like crypto_24_7) — "
        f"and calendar {calendar.name!r} ({calendar.minutes_per_day:.0f} minutes/day, "
        f"{calendar.days_per_year:.0f} days/year) is neither, so its day shape is undefined"
    )


def _day_starts(
    day: datetime, open_time: time, length: timedelta, interval: timedelta
) -> Iterator[datetime]:
    """Bar START times spaced by ``interval`` inside one day's trading window.

    Step from ``open_time`` by ``interval`` while the start is strictly before the
    window's end, so a bar's whole ``[ts, ts + interval)`` span is not required to
    fit — only its start must land inside the window (ADR-0022). On the equity
    session a 1-hour bar therefore runs short at 19:30; on a continuous day the
    interval divides 1440 minutes for every supported cadence, so none does.
    """
    ts = datetime.combine(day.date(), open_time)
    close = ts + length
    while ts < close:
        yield ts
        ts += interval


class SyntheticAdapter:
    """A :class:`~trading.interfaces.DataAdapter` that fabricates GBM bars.

    The bar cadence is fixed at construction by ``frequency`` (default
    :data:`~trading.frequency.DAILY`, ADR-0022), and so is the **market**: the
    frequency carries a :class:`~trading.calendar.MarketCalendar` (ADR-0054), which
    decides both the day shape the generator emits and the year it scales drift and
    volatility by (ADR-0056). Every bar is a pure function of ``(symbol, seed,
    params, frequency, absolute position)``, so a request is a slice of one
    canonical series (ADR-0030) — and since the calendar is part of a
    ``Frequency``'s identity, an equity ``"1d"`` and a 24/7 ``"1d"`` are two
    different canonical series rather than one series read two ways.

    The instance memoizes the cumulative daily walk per symbol. That is a cache of
    pure values — it changes speed, never numbers — but it does make an instance
    stateful, so an adapter is not meant to be shared across threads.
    """

    def __init__(
        self,
        seed: int = 0,
        params: SyntheticParams | None = None,
        *,
        frequency: Frequency = DAILY,
    ) -> None:
        self._seed = seed
        self._params = params or SyntheticParams()
        self._frequency = frequency
        calendar = frequency.calendar
        # Which market: the day shape and, below, the year. Raises on a calendar
        # this generator cannot draw (ADR-0056).
        self._continuous = calendar.is_continuous
        self._day_open, self._day_length = _day_shape(calendar)
        # Bars per trading day at this cadence (1 for daily). A 1-hour bar over a
        # 390-minute session gives 7, the last one running short (ADR-0022); over a
        # continuous 1440-minute day it gives 24, none short.
        self._slots = math.ceil(self._day_length / frequency.delta) if frequency.is_intraday else 1
        # Daily (backbone) drift/vol per trading day, and this cadence's per-bar vol:
        # one day's move is split across its slots, so sigma scales by 1/sqrt(n).
        # The year is the calendar's: 252 for equity — the same float the module
        # constant always was, so equity numbers cannot move — and 365 for 24/7,
        # without which a continuous series would realize 1.2035x its configured vol.
        days_per_year = calendar.days_per_year
        self._mu_day = self._params.annual_drift / days_per_year
        self._sigma_day = self._params.annual_vol / math.sqrt(days_per_year)
        self._sigma_bar = self._sigma_day / math.sqrt(self._slots)
        # symbol -> cumulative daily log return from EPOCH through session i.
        self._daily_cumulative: dict[str, list[float]] = {}
        self._base_prices: dict[str, float] = {}

    # --- positional primitives -------------------------------------------------

    def _key(self, symbol: str, stream: str, index: int) -> bytes:
        return _stream_key(symbol, self._seed, stream, index)

    def _base_price(self, symbol: str) -> float:
        """The symbol's price level at :data:`EPOCH` — its own draw, not the walk's.

        A per-symbol starting price so a multi-symbol universe isn't identical.
        """
        cached = self._base_prices.get(symbol)
        if cached is None:
            draw = _uniforms(self._key(symbol, "base", 0), 1)[0]
            cached = self._params.base_price * (0.5 + draw)
            self._base_prices[symbol] = cached
        return cached

    def _cumulative_through(self, symbol: str, session: int) -> list[float]:
        """Cumulative daily log return from :data:`EPOCH` through ``session``.

        The one place the ``O(bars from the epoch)`` walk lives: element *i* is the
        sum of the first *i + 1* positional daily returns, so the level compounds
        (ADR-0030). Extended in place and reused, so a second request on the same
        adapter pays only for the sessions it adds.

        Returns come :data:`_RETURNS_PER_KEY` at a time — session *i*'s return is
        element ``i % _RETURNS_PER_KEY`` of the block keyed ``i // _RETURNS_PER_KEY``,
        still a pure function of absolute position — because one ``blake2b`` digest
        yields eight words, i.e. four normals, and drawing one at a time would throw
        three quarters of every digest away.
        """
        cumulative = self._daily_cumulative.setdefault(symbol, [])
        while len(cumulative) <= session:
            block, offset = divmod(len(cumulative), _RETURNS_PER_KEY)
            draws = _standard_normals(self._key(symbol, "day", block), _RETURNS_PER_KEY)
            total = cumulative[-1] if cumulative else 0.0
            for draw in draws[offset:]:
                total += self._mu_day + self._sigma_day * draw
                cumulative.append(total)
        return cumulative

    def _session_closes(self, symbol: str, session: int) -> tuple[float, list[float]]:
        """One session's ``(previous close, per-slot closes)`` at this cadence.

        Daily: the session is a single bar, so this is just the walk's two levels.
        Intraday: the slot closes are a Brownian **bridge** from the previous daily
        close to this session's daily close — the slot increments are positional
        draws re-centred so they sum to exactly the session's log return, which is
        the true conditional law of a Gaussian walk given its endpoint. The last
        slot therefore closes exactly on the daily bar's close, and the intraday
        series is consistent with the daily one instead of a second, unrelated walk
        (ADR-0030). The cost of that choice: a session's total move is pinned before
        its path is drawn, so intraday variance is conditional-on-the-day, which is
        also what a real daily bar's relationship to its intraday bars looks like.

        "Session" here means one *trading day* and nothing about a venue's hours:
        this method takes an index and knows no timestamps, so it is identical for
        both markets (ADR-0056). On a continuous market the day is the calendar day
        and the bridge spans all 1440 of its minutes — the daily close it lands on is
        the next UTC midnight, so the grid has no overnight gap.
        """
        cumulative = self._cumulative_through(symbol, session)
        opening = cumulative[session - 1] if session else 0.0
        base = self._base_price(symbol)
        prev_close = base * math.exp(opening)
        if self._slots == 1:
            return prev_close, [base * math.exp(cumulative[session])]

        target = cumulative[session] - opening
        draws = _standard_normals(self._key(symbol, "intra", session), self._slots)
        mean = math.fsum(draws) / self._slots
        level = opening
        closes: list[float] = []
        for draw in draws:
            level += target / self._slots + self._sigma_bar * (draw - mean)
            closes.append(base * math.exp(level))
        return prev_close, closes

    def _bar(
        self, symbol: str, ts: datetime, prev_close: float, close: float, position: int
    ) -> Bar:
        """Wrap one close in an OHLCV bar: open near the previous close, then wicks.

        ``position`` is the bar's absolute index in the canonical series
        (``session * slots + slot``), so its shape draws are the same however the bar
        was requested. One digest covers all of them: two Box-Muller pairs give the
        three shocks and a fifth uniform sets volume. The invariants
        ``low <= min(open, close)`` and ``high >= max(open, close)`` hold by
        construction (rounding is monotone, so it cannot break them), and volume is
        strictly positive.
        """
        draws = _uniforms(self._key(symbol, "shape", position), 5)
        open_shock, high_shock = _box_muller(draws[0], draws[1])
        low_shock, _spare = _box_muller(draws[2], draws[3])
        half_vol = self._sigma_bar * 0.5
        open_ = prev_close * math.exp(half_vol * open_shock)
        high = max(open_, close) * (1.0 + abs(half_vol * high_shock))
        low = min(open_, close) * (1.0 - abs(half_vol * low_shock))
        return Bar(
            symbol=symbol,
            ts=ts,
            open=round(open_, 4),
            high=round(high, 4),
            low=round(low, 4),
            close=round(close, 4),
            volume=int(self._params.base_volume * (0.5 + draws[4])),
        )

    # --- the DataAdapter surface ----------------------------------------------

    def _trading_days_in(self, start: datetime, end: datetime) -> Iterator[datetime]:
        """The trading days of this adapter's market in ``[start, end]``, midnight UTC."""
        if self._continuous:
            return _calendar_days(start, end)
        return _trading_days(start, end)

    def _day_index(self, day: datetime) -> int:
        """``day``'s absolute position from :data:`EPOCH` on this adapter's market."""
        if self._continuous:
            return _calendar_day_index(day)
        return _session_index(day)

    def _bar_starts(self, start: datetime, end: datetime) -> Iterator[datetime]:
        """Bar START timestamps for this adapter's frequency over ``[start, end]``."""
        days = self._trading_days_in(start, end)
        if not self._frequency.is_intraday:
            return days
        interval = self._frequency.delta
        return (
            ts
            for day in days
            for ts in _day_starts(day, self._day_open, self._day_length, interval)
        )

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        adjusted: bool = True,
    ) -> list[Bar]:
        """Return the deterministic GBM series for ``symbol`` in ``[start, end]``.

        A slice of the one canonical series for this ``(symbol, seed, params,
        frequency)``, so overlapping ranges agree on every shared timestamp
        (ADR-0030). ``start`` earlier than :data:`EPOCH` is clipped: bars before the
        epoch do not exist, and no range is ever re-anchored to its own first bar.

        Which timestamps exist is the market's business, not this call's: the
        frequency's calendar fixed both the interval and the day shape at
        construction (ADR-0022/0056), so ``get_bars`` never learns either.

        Synthetic GBM has no corporate actions, so raw == adjusted: ``adjusted`` is
        accepted for :class:`DataAdapter` parity but does not change the numbers,
        which lets the offline paper feed (raw, ADR-0021) and the backtest feed
        (adjusted, ADR-0008) drive the identical series.

        Both bounds must be timezone-aware. The old generator read only their
        date components, so a naive datetime slipped through silently; clipping to
        :data:`EPOCH` compares them, so say what is wrong instead of raising a bare
        ``TypeError`` from the comparison.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError(
                "SyntheticAdapter needs timezone-aware start/end (a Bar's ts always "
                f"is); got start={start!r}, end={end!r}"
            )
        stamps = self._bar_starts(max(start, EPOCH), end)
        bars: list[Bar] = []
        for _day, day_stamps in groupby(stamps, key=lambda ts: ts.date()):
            session_stamps = list(day_stamps)
            session = self._day_index(session_stamps[0])
            prev_close, closes = self._session_closes(symbol, session)
            # A trading day always begins at its open (:func:`_day_starts`), so a
            # timestamp's place in the group *is* its slot within the day. Grouping
            # by UTC date is exact for both shapes: the equity session sits inside
            # one date and a continuous day *is* one (ADR-0056).
            for slot, ts in enumerate(session_stamps):
                previous = closes[slot - 1] if slot else prev_close
                position = session * self._slots + slot
                bars.append(self._bar(symbol, ts, previous, closes[slot], position))
        return bars
