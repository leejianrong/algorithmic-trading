# ADR-0054: Annualization is a property of the market, not a module constant

- Status: Accepted
- Date: 2026-08-11
- Deciders: strategy developer (project owner)
- Tickets: KAN-705 (EPIC-87, "Crypto: a 24/7 market")

## Context

`frequency.py` has carried the US-equity cash session in its arithmetic since
ADR-0022:

```python
TRADING_DAYS_PER_YEAR = 252.0
REGULAR_SESSION_MINUTES = 390.0
...
return TRADING_DAYS_PER_YEAR * (REGULAR_SESSION_MINUTES / minutes)
```

That is correct for the only market this bench has ever traded, and ADR-0022 said
so explicitly ("an explicit modeling assumption … fine for annualizing a synthetic
offline series"). It is wrong for every other market, and the epic that follows this
card adds one: a 24/7 crypto venue trades **365 days x 1440 minutes**, not 252 x 390.

`periods_per_year` is the single annualization knob for the whole reporting stack —
Sharpe, Sortino, Calmar, annualized return, turnover, return per unit exposure,
alpha, the information ratio, and every ADR-0039 significance figure computed from
them. One number, wrong by a constant factor, moves all of them at once.

**The card's arithmetic, verified rather than quoted.** The equity factor at 5m is
`252 * (390/5) = 19,656`; the 24/7 factor is `365 * (1440/5) = 105,120`. The ratio
is **5.3480x**, so the Sharpe (which scales by `√periods_per_year`) is out by
**2.3126x**. Daily is `365/252 = 1.4484x`, i.e. `√` = **1.2035x**. The ratio is
identical at every sub-daily interval — it is `(365 x 1440) / (252 x 390)` with the
interval cancelling — so 1h, 30m, 5m and 1m are all 5.3480x / 2.3126x out. Both of
the card's figures hold.

**The direction, stated precisely, because the card's framing and its own text
disagree.** Annualizing a 24/7 return series on the equity calendar uses a
*smaller* factor, so the reported figure is **smaller** than the truth. On a
profitable strategy that **understates** — the card's own wording, and the
conservative direction. What actually flatters is a **losing** strategy: measured
on a real 5m synthetic run (seed 5, AAPL+MSFT, 2021-06, `sma_crossover`, 1,794
equity points, total **-3.73%**), the same return series scores **Sharpe -8.34** on
252 x 390 and **-19.28** on 365 x 1440, and annualized return **-34.05%** against
**-89.21%**. A loss reported as less than a third as bad on the risk-adjusted
number is exactly the kind of flattery this bench exists to refuse. Daily, the same
run family: **-0.2608** vs **-0.3139** (16.9% of the correct magnitude).

Worse than either direction on its own: total return and max drawdown do **not**
scale with `periods_per_year`, so a mis-annualized run reports an honest drawdown
next to a Sharpe and a Calmar computed on a different market's year. The figures
stop being consistent with each other, which is harder to spot than a uniform bias.

This card is deliberately sequenced **before** the crypto data adapter exists, so
no crypto number is ever produced under the equity calendar in the first place.

## Decision

**A new `MarketCalendar` value type owns the two numbers, in a new module.**
`src/trading/calendar.py` holds a frozen, validated `MarketCalendar(name,
days_per_year, minutes_per_day)` plus two named instances and a small registry:

- `US_EQUITY = MarketCalendar("us_equity", 252.0, 390.0)` — the former module
  constants, exactly.
- `CRYPTO_24_7 = MarketCalendar("crypto_24_7", 365.0, 1440.0)`.
- `CALENDARS` / `get_calendar(name)`, case- and whitespace-insensitive, raising
  `ValueError` naming the known calendars. **An unknown name never falls back to
  the equity calendar**: silently annualizing a 24/7 market on 252 x 390 is the
  defect being removed, and a fallback would reintroduce it as a default.

It is a **new file on purpose**. This slice landed as one of three parallel lanes in
the same epic, all of which need to say "equity vs crypto"; a new module has zero
collision surface, and it makes this the single market-calendar vocabulary rather
than three private enums.

**Two derivations, one rule.** `MarketCalendar.periods_per_year(interval)`:

- **sub-daily** — `days_per_year * (minutes_per_day / interval_minutes)`. The
  expression order is preserved from the old function so the equity result is
  bit-for-bit identical, and a comment says so.
- **one day or longer** — `days_per_year / days_per_bar`. A daily bar covers a whole
  trading day however short the session, which is why equity daily is exactly
  `252.0` and *not* `252 * (390/1440) = 68.25`.

The two derivations coincide on a continuous market (365 x 1440 one-minute bars =
525,600 = 365 daily bars x 1440) and deliberately do not on a session market. Both
properties are pinned by tests, in both directions.

**`Frequency` carries its calendar.** A fourth field, `calendar: MarketCalendar =
US_EQUITY`, defaulted so every existing construction still works. Two consequences,
both wanted: a frequency can say which market's year produced its factor, and a
`"5m"` on a 24/7 market is **not equal** to a `"5m"` on the equity calendar, so the
two cannot be conflated by a dict key, a set, or an `==`.

**The crypto path is a keyword, never a new positional.** `Frequency.parse(label,
*, calendar=US_EQUITY)` keeps its single-argument signature and its exact equity
return value, so **`cli.py` needed no change at all** — verified by a test that
introspects the signature and asserts `label` is the only required positional. The
CLI surface that *selects* a market is a later integration card; adding a flag here
would have collided with two sibling lanes. `frequencies_for(calendar)` gives the
whole standard set on any market, and per-calendar registries are memoized (a
`Frequency` is immutable and building one is pure arithmetic).

**`TRADING_DAYS_PER_YEAR` and `REGULAR_SESSION_MINUTES` stay**, now as views onto
`US_EQUITY`. They are imported by `report.py` (the `result.json` frequency
fallback) and `data/synthetic.py` (its weekday-walk GBM scaling), and by an existing
test. Keeping the names is what makes this slice additive: **no file outside
`frequency.py`, the new `calendar.py`, the new test module and this ADR was
touched.**

### The annualization factors, in full

| interval | equity (252 x 390) | crypto 24/7 (365 x 1440) | factor ratio | Sharpe ratio |
|---|---|---|---|---|
| 1d | 252 | 365 | 1.4484x | 1.2035x |
| 1h | 1,638 | 8,760 | 5.3480x | 2.3126x |
| 30m | 3,276 | 17,520 | 5.3480x | 2.3126x |
| 5m | 19,656 | 105,120 | 5.3480x | 2.3126x |
| 1m | 98,280 | 525,600 | 5.3480x | 2.3126x |

Every equity column entry is written as a literal in `tests/unit/test_calendar.py`
rather than derived from `US_EQUITY`, so a change to the calendar cannot quietly
redefine the number this bench has always reported.

### Both calendars are nominal, and say so

No holidays, no half-days, no exchange maintenance window, and 365 rather than
365.25 (a 0.07% understatement of a crypto year, three orders of magnitude below
the 5.35x error this removes). A real trading calendar is **KAN-687** and needs a
provider dependency; this module is the seam it slots behind. `days_per_year` is
capped at 366 and `minutes_per_day` at 1440 so a nonsense calendar is rejected at
construction rather than producing a plausible-looking factor.

## What was measured

- **Equity is byte-identical**, which the card called the test that matters. Three
  runs against `main` @ `cfb4d85`, hashing the artifacts (stdout embeds the `--out`
  path): daily `backtest` (`equity_curve.csv`
  `220e0bb8…`, `result.json` `ff6d6098…`), **5m** `backtest` (`4ba021e1…`,
  `cb9d6f42…`) and `paper --once` (`9608600b…`, `3a8fc771…`) all match, and the
  paper `--out` directory is `diff -r` identical across all four artifacts. The 5m
  run is the one that would move first: `result.json` carries a `metrics` block, so
  any drift in the factor shows up in those bytes.
- **The fast gate passes with no existing test or golden modified** — 1,097 passed,
  2 skipped (optional extras), and `make test-integration` (offline) green.
- **The guard was watched failing.** Reverting `MarketCalendar.periods_per_year` to
  the hard-coded equity constants (the exact regression this card exists to prevent)
  turns **16** tests red, **all 16 in `tests/unit/test_calendar.py`** and not one
  anywhere else in the 1,097-test suite: the five crypto factors twice over, the
  crypto daily identity, the continuous-market consistency check, both crypto Sharpe
  consequences, `frequencies_for`, and the multi-day crypto arithmetic. That the
  *rest* of the suite stays green under the revert is the finding — nothing this
  bench already had could see the defect. The equity tests in the new module stay
  green too, by design: they pin the number, and only the crypto ones can pin where
  the number comes from.
- **No 24/7 bars were needed, and none exist.** Confirmed against the code:
  `SyntheticAdapter` emits weekday-only bars stamped inside a 13:30-20:00 UTC
  session (a 5m fetch across 2021-06-03..08 returns 312 bars = 4 weekdays x 78,
  the weekend absent). This card is arithmetic on a return series, so its tests
  build `EquityPoint` curves by hand and never ask an adapter for anything. A 24/7
  synthetic mode is a later card in this epic; ADR-0040's lesson applies to it, not
  here.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Add a `days_per_year` / `minutes_per_day` argument to `_intraday_periods_per_year` | The numbers travel together and are meaningless apart; two loose floats at every call site is exactly how the equity assumption spread in the first place. |
| Put the calendar fields on `Frequency` directly (no new type) | Then a "market" cannot be named, registered, compared, or passed to code that has nothing to do with bar cadence — and three parallel lanes needed a shared word for it. |
| An `enum Market { EQUITY, CRYPTO }` | An enum carries a name and no arithmetic, so every consumer would re-derive the factors from the enum — the constants scattered again, one layer up. A value object with the numbers on it has one implementation. |
| Make `calendar` a required argument to `Frequency.parse` | Forces `cli.py` to change, which this batch reserved, and turns a safe default into a chance to pass the wrong market. Keyword-only with an equity default keeps the existing call byte-identical. |
| Derive daily as `days_per_year * (minutes_per_day / 1440)` for one rule instead of two | Gives equity daily `68.25`, silently rewriting every historical figure. A daily bar is one session's worth, not 390 minutes of a 1440-minute day. |
| A CLI `--market` flag now | `cli.py` is owned by the integration card in this epic; three lanes cannot collide on it. The library seam ships first, the flag follows. |
| Use 365.25 days for crypto | Leap-year precision (0.07%) on top of a nominal calendar with no maintenance windows is false precision. Recorded here instead. |
| Fix the remaining 252s in `risk.py` / `data/recent_window.py` in this slice | Both are other lanes' files in this parallel batch. Named below so a later card can finish the job. |
| Fall back to `US_EQUITY` on an unknown calendar name | The silent-equity-default *is* the bug. `get_calendar` raises and names what it knows. |

## Consequences

- Annualization is now a property of a named market. `Frequency.parse("5m",
  calendar=CRYPTO_24_7).periods_per_year == 105_120`, and the equity call is
  untouched.
- Every existing caller — `cli.py`, `metrics.py`, `report.py`, `sweep.py`,
  `data/synthetic.py` — compiles and behaves exactly as before. `metrics.py` needed
  **no change**: it already threads `periods_per_year` as a plain float through
  every entry point, which is what made this slice small.
- **The 252 assumption is not fully joined up.** Three sites still carry it, none of
  them mine in this batch, all of them listed for a follow-up card:
  - `src/trading/risk.py` — `_TRADING_DAYS = 252`, the vol-target realized-vol
    annualization (ADR-0015). A 24/7 series annualized on 252 understates realized
    vol, so the effective gross cap would be scaled by a wrong number.
  - `src/trading/data/recent_window.py` — `RTH_SESSION` / `CALENDAR_DAYS_PER_SESSION`,
    the bounded-fetch-window sizing (ADR-0047). Those convert a 6.5-hour session
    inside a 24-hour day; on a 24/7 source they over-fetch by ~3.7x, which errs in
    the safe direction (ADR-0047 chose to err wide) but is the same assumption.
  - `src/trading/data/synthetic.py` — scales its GBM by `TRADING_DAYS_PER_YEAR`,
    which is *correct* for the weekday-only series it actually emits, and will need
    the calendar the day it grows a 24/7 mode.
- **`result.json` still carries a bare interval label with no market.**
  `report._resolve_periods_per_year(frequency, override)` parses `"5m"` on the
  equity calendar when no explicit `periods_per_year` is passed, and that value
  feeds the benchmark-relative block (ADR-0037). A crypto run must therefore pass
  `periods_per_year=` explicitly at the `write_result_json` call site — which lives
  in `cli.py`, i.e. in the integration card's scope. Left alone deliberately:
  changing the fallback would either alter equity bytes or require a schema field,
  and both belong with the flag that selects a market. Recorded so it is not
  discovered by a wrong number.
- `Frequency` equality now includes the calendar. Two frequencies with the same
  label on different markets are unequal — intended, and the reason a mixed-market
  comparison cannot pass silently — but any code that treated a label as an
  identity should compare `label`, not the whole value.
- Forecloses nothing. A real exchange calendar (KAN-687), a partial-week venue, or
  a futures session all fit the same value type; a calendar with holidays would add
  a method rather than change these two numbers.
