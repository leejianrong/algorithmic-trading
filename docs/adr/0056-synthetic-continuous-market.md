# ADR-0056: The synthetic generator emits a 24/7 market, chosen by its calendar

- Status: Accepted
- Date: 2026-08-11
- Deciders: strategy developer (project owner)

## Context

EPIC-87 was scoped on the belief that `SyntheticAdapter` could already produce
continuous bars, so "all three Phase 1 cards are testable with NO network and NO
crypto data". KAN-830 checked that by running it, and it is false. Measured on
`main` @ `cfb4d85`, unchanged at `a157123`:

```
get_bars("BTC", 2021-01-01 .. 2021-01-15) at 1d -> 11 bars, weekdays only
get_bars("BTC", 2021-01-04 .. 2021-01-04) at 1h ->  7 bars, 13:30..19:30 UTC
```

`_session_index` counts weekdays from `EPOCH` and a weekend emits nothing;
`_intraday_starts` fills the nominal 13:30–20:00 UTC regular equity session
(`_SESSION_OPEN` / `_SESSION_CLOSE`, `_SESSION_LENGTH = 6.5h`).

The sharper measurement is the third one. ADR-0054 gave `Frequency` a
`MarketCalendar`, so a caller can already say `Frequency.parse("1d",
calendar=CRYPTO_24_7)` — and handing that to the adapter still returned **11
weekday bars**. The calendar reached the generator and was ignored: the
annualization basis was per-market while the data it annualized was not.

Why this blocks rather than decorates. A crypto adapter and broker (KAN-708) need an
offline stand-in, because the required `integration` job may not leave the machine
(ADR-0040). And this is the exact shape of ADR-0040's own lesson, which has now been
sighted three times (ADR-0047, ADR-0053): a stand-in that is *differently shaped*
from the provider makes a regression test pass whether or not the bug exists.
`SyntheticAdapter` clips a `datetime.min` start and `FakeAdapter` filters any range,
and that pair hid ADR-0047 for months.

## Decision

### The market is the frequency's calendar. There is no second switch

`SyntheticAdapter` reads `frequency.calendar` (ADR-0054) and nothing else. No new
constructor parameter, no `market=` argument, and — per ADR-0022 — nothing reaches
`get_bars`, whose signature is pinned by an introspection test.

This is the load-bearing choice, and it is about what cannot be expressed rather
than about ergonomics. A separate `market=` flag would allow **24/7 bars annualized
on 252 days**, which is precisely the defect ADR-0054 exists to remove, sitting one
mismatched keyword away. Deriving the shape from the calendar that already sets
`periods_per_year` makes that combination unrepresentable.

Two further properties fall out for free rather than being arranged. ADR-0030 keys
the canonical series on `(symbol, seed, params, frequency)`, and since ADR-0054 the
calendar is part of a `Frequency`'s identity — so an equity `"1d"` and a 24/7 `"1d"`
are two distinct canonical series that cannot be conflated by a dict key, and
`Frequency.parse`'s calendar argument is keyword-only with an equity default, so
**equity remains the default** and every existing caller is untouched.

### Exactly two day shapes, and a third is refused

| calendar | days emitted | day window | slots at 1h |
|---|---|---|---|
| `US_EQUITY` (252 x 390) | Mon–Fri | 13:30–20:00 UTC | 7 (last one short) |
| `CRYPTO_24_7` (365 x 1440) | every calendar day | 00:00–24:00 UTC | 24 |

`_day_shape(calendar)` returns `(open, length)`: continuous markets get UTC midnight
and 24 hours, `minutes_per_day == 390` gets the equity session, and **anything else
raises** at construction.

Refusing is not fussiness. `MarketCalendar` carries no *opening time*, so a
non-continuous market cannot be given one here — a hypothetical 24-hour weekday-only
venue (1440 x 252, FX-shaped) would otherwise silently receive 6.5-hour days while
annualizing on 1440 minutes, and worse, a 1440-minute window opened at 13:30 spills
across the UTC date boundary that `get_bars` groups by, which would corrupt the slot
arithmetic rather than merely mis-shape the day. `get_calendar` raises rather than
falling back to equity for the same reason (ADR-0054): a silent equity default *is*
the bug.

### A continuous day is stamped from UTC midnight — inherited deliberately, not silently

ADR-0053 chose UTC midnight as the instant a 24/7 daily bar closes, on the grounds
that it is the only anchor with no owner and the one under which the session and
rolling-day completeness rules coincide. This ADR adopts the same anchor for
*generation*, and the reason is stronger here than convention: the daily bar and the
intraday grid must share an anchor, or the bridge's last bar would not land on the
daily close. A daily bar START-stamped at UTC midnight covers `[00:00, 24:00)`, so
the intraday grid steps from 00:00 and the last bar's window ends exactly where the
next daily bar begins — verified at 1h (`23:00 + 1h == next 00:00`).

Had ADR-0053 picked another anchor, this generator would have had to follow it rather
than the reverse. Stating that is the point: the convention is one decision used
twice, not two decisions that happen to agree.

### The GBM scaling follows the calendar, and the correction is measured

The per-day sigma is `annual_vol / sqrt(days_per_year)`. The generator divided by
`frequency.TRADING_DAYS_PER_YEAR` (252); it now divides by
`frequency.calendar.days_per_year`. For `US_EQUITY` those are **the same float** —
`TRADING_DAYS_PER_YEAR` has been a view onto `US_EQUITY.days_per_year` since
ADR-0054 — which is why the equity series cannot move, structurally rather than by
luck.

Left at 252 on a 365-bar year, a continuous series would realize
`sqrt(365/252) = 1.2035x` the volatility it was configured with. That was not
reasoned about and left there; it was measured both ways on the same seed and span
(7,305 daily bars, 2000–2020, `annual_vol = 0.60`, `annual_drift = 0.30`):

| divisor | per-step sd | annualized vol (on 365) | annualized drift |
|---|---|---|---|
| 252 (the defect) | 0.037118 | **0.7091** | +0.4106 |
| 365 (shipped) | 0.030841 | **0.5892** | +0.2801 |

The ratio of the two per-step sigmas is **1.2035**, i.e. exactly `sqrt(365/252)`, and
the drift error is linear rather than square-root (`365/252 = 1.4484x`). The shipped
figure sits **1.8% below** the configured 0.60, about two standard errors of a sample
stdev at this size (`1/sqrt(2n) = 0.83%`) — and the *equity* series measures 0.5899
for the same configuration, so both markets realize the same vol on their own year.
Reproduced at three volatilities: configured 0.20 / 0.60 / 0.80 realize 0.1964 /
0.5892 / 0.7856 correctly, against 0.2364 / 0.7091 / 0.9455 under the old divisor.

Sub-daily agrees, which the bridge does not guarantee a priori: one year of 24/7 bars
at `annual_vol = 0.60` annualizes to **0.6124** at 1h, **0.6023** at 30m and
**0.5991** at 5m. The residual is the bridge's conditional-on-the-day variance
(ADR-0030), not the scaling.

### The position is a calendar-day count — and range independence does not check that

`_calendar_day_index(day) = (day - EPOCH).days` replaces `_session_index`'s weekday
arithmetic on a continuous market. ADR-0030's invariant is untouched — a bar is a
pure function of its absolute position, in closed form, so overlapping ranges agree —
but the *counting function* is per-market, which is the second reason two calendars
must be two canonical series.

Proving the guard turned up something worth recording. Reverting the index to
`_session_index` while leaving everything else correct turned **only the three
volatility tests** red: every range-independence test still passed, because a
*wrong-but-pure* position function is still a pure function of the timestamp.
**Range independence constrains purity, not injectivity.** Under the weekday index a
Saturday and a Sunday share the preceding Friday's position and come back as
byte-identical bars, and nothing in the suite said so. A test now pins injectivity
directly (212 consecutive days, all distinct), which takes that revert from 3 red
to 4.

## Alternatives considered

| Option | Why not |
|--------|---------|
| A separate `market=` / `continuous=True` constructor flag | Two switches that can disagree, and the disagreement is exactly ADR-0054's defect: 24/7 bars annualized on 252 days. The calendar already had to be right for the metrics; making it also decide the shape removes the possibility rather than documenting it. |
| Derive the shape from `days_per_year >= 365` alone | Ignores `minutes_per_day`, so a 365-day market with a 6.5-hour session (nothing real, but constructible) would get 24-hour days. `is_continuous` already means both, and it is the calendar's own predicate. |
| Give the non-continuous shape `calendar.minutes_per_day` as its session length | Tempting generality, and unsafe: `MarketCalendar` has no opening time, so a 1440-minute window would open at 13:30 and spill past the UTC date `get_bars` groups by, silently corrupting the slot arithmetic. Refusing an unmodellable calendar is the honest version. |
| Fall back to the equity shape for an unknown calendar | A silent equity default is the bug ADR-0054 removed from `get_calendar`. Re-introducing it one module over would be the same mistake with a different owner. |
| Anchor the continuous day at 00:00 ET, or at the venue's "trading day" | Rejected for ADR-0053's reasons (a market with no venue timezone borrowing one exchange's local calendar, plus DST), and additionally here: the daily bar and its intraday grid would need the same non-UTC anchor, spreading the choice further. |
| A continuous walk from the epoch at the bar cadence, no daily backbone | ADR-0030 already rejected this for the session market (~3.1M steps for a 1-minute request); on a 24/7 calendar it is *worse* by 24/6.5 x 365/252, and it would give up the cross-frequency agreement that makes 1h/5m/1d one series. |
| Model an inception date, maintenance windows, or fat tails | Each is a real difference from a crypto venue, and each is a modelling decision this card cannot make offline with no venue to check against. Named as limitations and pinned by tests instead of half-built. |
| Skip the generator; write explicit 24/7 fixtures per test (what ADR-0053 did) | Right for 27 completeness tests, wrong as the epic's foundation: KAN-708 needs a multi-symbol, multi-year, range-independent series a backtest can actually run on. This ADR is what lets a *later* test choose the cheaper tool knowingly. |

## Consequences

- **Buys.** An offline continuous series at 1d/1h/30m/5m/1m that the whole stack can
  run on with no network and no crypto credentials, so KAN-708's adapter and broker
  have a fixture and the required `integration` job stays offline (ADR-0040). Range
  independence holds on the new counting at both cadences (a 1d sub-range equals the
  tail of its parent over 400+ bars; a 100-bar interior window and a weekend-to-weekend
  9-bar window are true slices; a 1h sub-range matches over 168 bars). The
  cross-frequency agreement survives the longer day: 1h, 30m and 5m all close on the
  daily bar at every one of 10 days, and 1m matches over a 1,440-bar day.
- **The equity series does not move**, verified by hash rather than asserted. Against
  the `cfb4d85` baselines (still current on `a157123`): daily backtest
  `equity_curve.csv` `220e0bb8…`, `result.json` `ff6d6098…`; 5m backtest `4ba021e1…` /
  `cb9d6f42…`; `paper --once` `9608600b…` / `3a8fc778…` / `daa33064…` — all eight
  unchanged. No existing test or golden was modified; the 26 tests in
  `test_synthetic.py` pass untouched, including its two pinned exact bars.
- **Costs.** The generator now has a branch on market in three places
  (`_trading_days_in`, `_day_index`, `_day_shape`) where it had none — the price of
  two shapes in one adapter, taken instead of a second adapter class that would have
  duplicated the walk, the bridge and the draw plumbing. A continuous walk is ~1.45x
  the epoch steps of an equity one for the same date (365 vs 252 per year), so a 2020s
  request costs ~13,000 steps rather than ~9,000; still milliseconds, memoized per
  instance. And a 1m continuous day is 1,440 bars against the session's 390, so a
  careless wide 1m request is ~3.7x the bars it used to be — the ADR-0047 bound is what
  keeps that off the paper path.
- **Unverified against a real venue, and deliberately so.** There are no crypto
  credentials here and this lane made no network call, so **nothing below was observed
  against a crypto provider** — it is arithmetic and a generator:
  - Whether a real 24/7 provider stamps its daily bars at UTC midnight. ADR-0053
    chose the convention and noted the same gap; this generator now *embodies* it, so
    a provider that stamps at 08:00 UTC would disagree with our fixture about what a
    "day" is. Not detected at runtime.
  - Whether a crypto endpoint answers an absurdly early start with an empty response
    (as Alpaca's equity endpoint does, ADR-0047) or an error. **This adapter clips to
    `EPOCH` in both modes**, inherited unchanged from ADR-0030 because the paper feed
    depends on it — so it is *more forgiving than a provider may be*, and it must not
    be used to test bounded-window behaviour. Pinned by a test that asserts the
    clipping so the limitation is visible rather than discovered.
  - Whether real 24/7 markets have gapless bars. Ours do, by construction: no
    inception date (every symbol has 1990 bars), no maintenance window, no missing
    bars, no partial days. All three pinned as characterizations.
- **Still GBM, and the caveat is sharper here than in ADR-0012.** A 20% down day is a
  ~6.4-sigma event at 60% annual vol, so this series essentially never produces one,
  while real crypto does — asserted as a test, in the direction that fails if the model
  ever grows fat tails without the docs following. ADR-0055 made the same point from
  the risk side. This validates plumbing; it judges nothing.
- **Reverting the fix, per hunk:** the GBM divisor turns **3** red; the calendar-day
  iterator **19**; the calendar-day index **4** (3 before the injectivity test that
  this exercise exposed as missing); the continuous day shape **14**. 45 new fast
  tests total.
- **Not wired to anything.** Like the rest of EPIC-87 phase 1, this is a library seam:
  no CLI flag selects a market (that is KAN-835's lane), `cli.py` and `report.py` are
  untouched, and a `trading backtest --source synthetic` run still gets the equity
  calendar. The mode is reachable from Python as
  `SyntheticAdapter(frequency=Frequency.parse("1d", calendar=CRYPTO_24_7))`.
- **Open, and now cheaper to close.** `data/recent_window.py`'s `fetch_span` still
  sizes its window on the equity calendar (ADR-0053 assessed it: 5.79x / 21.39x too
  wide for 24/7, the safe direction) — a continuous synthetic series is now the fixture
  that could measure the alternative. `risk.py`'s `_TRADING_DAYS = 252` in the
  vol-target path remains equity-only (ADR-0055's list). And nothing yet generates a
  *correlated* multi-symbol universe in either mode, which a cross-sectional crypto
  strategy would want.
