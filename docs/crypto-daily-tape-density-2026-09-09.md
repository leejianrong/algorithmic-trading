# Crypto daily tape-density check — 2026-09-09

> Answers KAN-1078 (EPIC-140): the open question
> `docs/crypto-research-pass-2026-09-02.md` §2 named before scoping KAN-1079 —
> "does Alpaca's *daily* crypto tape have the same holes ADR-0073 measured at
> 5m/1m?" — and nothing else. No code changed behavior; this is a measurement,
> not a decision (see "Why this is not an ADR" below).
>
> **All numbers below were measured directly against the real Alpaca paper
> account on 2026-09-09 by the orchestrating (PM) session, using the real
> `AlpacaAdapter` and `trading.tape_density` functions — not by the session that
> wrote this document or its accompanying script.** This doc records that
> result; it does not re-derive it. The script added alongside it
> (`scripts/crypto_daily_tape_density.py`) exists so a future session can
> re-run the same check for whatever range it actually settles on, since this
> worktree has no Alpaca credentials to run it again itself.

## Headline finding

**No, daily bars do not have the same tape-density problem ADR-0073 found at
5m/1m.** 5m/1m failures were widespread and driven by thin *intraday* order
flow across many symbols (crypto10: 4/10 fail the 0.80 coverage floor at 5m,
10/10 fail at 1m — ADR-0073). At `1d`, bars aggregate over whatever sub-daily
intervals a coin's tape happened to skip within a day, and **7 of 10 crypto10
symbols come back effectively 100% complete** at daily granularity. Two more
(`AAVE/USD`, `AVAX/USD`) *look* incomplete over a window starting before they
were listed on Alpaca, but are **100.1% complete since their own first bar** —
a listing-date artifact, not a gap. The one real structural finding is
**`SOL/USD`**, which has been listed on Alpaca since the same date as
`BTC/USD` and `ETH/USD` yet is missing **417 of 2,076 expected daily bars
(79.9% coverage)** — a genuine, already partially-known hole (see
"Cross-validation" below), not a listing artifact.

**Practical recommendation for KAN-1079:** run as scoped, on `crypto10`, at
`--interval 1d`. But **flag `SOL/USD`'s ~20% daily gap rate explicitly in every
result table it appears in**, or consider dropping it from any table that
reports per-symbol contribution to a strategy's return. A ~20% missing-bar
rate at daily granularity is large enough to materially affect any strategy
whose logic depends on a bar existing at every expected step (a lookback
window, a rebalance-day count, a signal computed over "the last N days") —
`SOL/USD`'s own trailing-return or moving-average calculation silently skips
whatever calendar days it has no bar for, which is not the same computation a
complete tape would produce, and no code in this bench currently detects or
warns about that per-symbol.

## Methodology

Reused `trading.tape_density.expected_bar_count` / `bar_coverage_ratio`
exactly as ADR-0073 built and validated them — the only change is the
`Frequency` passed in (`Frequency.parse("1d", calendar=CRYPTO_24_7)` instead
of `5m`/`1m`). No new arithmetic; this is ADR-0073's own measurement, applied
at a third interval it had not yet been run at.

```python
from datetime import UTC, datetime
from trading.calendar import CRYPTO_24_7
from trading.data.alpaca_adapter import AlpacaAdapter
from trading.frequency import Frequency
from trading.tape_density import bar_coverage_ratio, expected_bar_count
from trading.universe import get_universe

START = datetime(2021, 1, 1, tzinfo=UTC)  # Alpaca's crypto tape start (ADR-0058)
END = datetime(2026, 9, 8, tzinfo=UTC)  # yesterday -- avoid today's forming bar

freq = Frequency.parse("1d", calendar=CRYPTO_24_7)
adapter = AlpacaAdapter(calendar=CRYPTO_24_7)
symbols = get_universe("crypto10")

expected = expected_bar_count(START, END, freq)
for symbol in symbols:
    bars = adapter.get_bars(symbol, START, END)
    coverage = bar_coverage_ratio(bars, START, END, freq)
```

`adjusted` was left at its default; a crypto pair has no splits or dividends
(ADR-0058), so raw and adjusted are the same series and this makes no
difference to the result. This is a **read-only market-data check** — no
orders, no live trading session, no account state touched — so it did not
conflict with any concurrent live work.

## Results — full fixed window (2021-01-01..2026-09-08, 2,076 days)

`expected_bar_count` = 2076.00 bars/symbol on `CRYPTO_24_7`'s 365-day-per-year
daily cadence.

| symbol | bars | coverage | first_ts | last_ts |
|---|---|---|---|---|
| BTC/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| ETH/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| SOL/USD | 1659 | 79.9% | 2021-01-01 | 2026-09-08 |
| LINK/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| LTC/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| BCH/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| DOGE/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| UNI/USD | 2077 | 100.0% | 2021-01-01 | 2026-09-08 |
| AAVE/USD | 1882 | 90.7% | 2021-07-15 | 2026-09-08 |
| AVAX/USD | 1756 | 84.6% | 2021-11-18 | 2026-09-08 |

Mean coverage over the fixed window: **95.5%**. The 2077-vs-2076-expected
off-by-one on the 100% names is an inclusive-boundary rounding artifact
(the window endpoints are both inclusive calendar dates spanning 2,076 *nights*
but 2,077 *calendar days*), not a real extra bar — do not read anything into
it.

## The late-listing-vs-genuine-hole split

`AAVE/USD` and `AVAX/USD`'s apparently-low coverage above is fully explained
by **late listing**, not missing bars. Re-scoring each from its own first bar
(rather than the fixed 2021-01-01 window start) gives:

| symbol | since-listing bars | expected | coverage |
|---|---|---|---|
| AAVE/USD | 1882 | 1881.0 | 100.1% |
| AVAX/USD | 1756 | 1755.0 | 100.1% |
| SOL/USD (control — same start either way) | 1659 | 2076.0 | 79.9% |

AAVE listed on Alpaca 2021-07-15, AVAX 2021-11-18 — both well after the
window's 2021-01-01 start — and since listing, both are **100.1% complete**
(the fractional excess over 100% is the same inclusive-boundary artifact noted
above). `SOL/USD` is the control: it has been listed since the window start,
same as `BTC/USD`/`ETH/USD`, so re-anchoring its window changes nothing —
its coverage is 79.9% either way, which is exactly what makes it a genuine
hole rather than a listing artifact.

This distinction — "not listed yet" vs. "listed and dropped bars" — is now a
**tested behavior**, not just an assertion in this doc's prose: see
`tests/unit/test_crypto_daily_tape_density.py::TestSummarizeCoverage::test_late_listing_is_not_penalized_once_rescored`,
which reproduces the AAVE/AVAX shape on synthetic `Bar`s (listed 50 days into
a 100-day window, complete since) and asserts the fixed-window score reads as
a ~50% hole while the since-listing score is ~100%.

## Cross-validation against `universe.py`'s existing SOL note

`src/trading/universe.py` already carries a comment about `SOL/USD`, written
against an earlier, shorter measurement (ADR-0058, not ADR-0073):

```python
# `core10` behaviour) -- and note SOL is listed from 2021-01-01 but returns 1,634
# bars in a 2,052-day span, i.e. this tape has genuine holes, unlike
# `SyntheticAdapter`, which has none.
...
"SOL/USD": "smart_contract",  # 2021-01-01 (gaps: 1,634 bars of 2,052 days)
```

1,634 / 2,052 = **79.63%**. This session's measurement — a longer, later
window (2,076 days vs. 2,052; ending 2026-09-08 vs. whenever ADR-0058 was
written) — gives **79.9%**, essentially the same rate. Two independent
measurements, taken at different times over different (overlapping) windows,
agree to within 0.3 percentage points: `SOL/USD`'s daily gap rate on Alpaca is
a **stable, persistent property of this symbol's tape**, not noise from one
particular day or window. This is the cross-validation
`docs/crypto-research-pass-2026-09-02.md` §2 asked for, and it holds.

**`universe.py`'s existing comment is accurate and does not need correcting**
— it already states the right rate (within rounding) and the right
conclusion ("this tape has genuine holes"). This document's contribution is
confirming that finding on a longer, more recent window and tying it
explicitly to the daily-vs-intraday question KAN-1078 was scoped to answer,
which the original comment (written for a different purpose, documenting
`crypto10`'s per-symbol inception dates) did not address. No edit was made to
`universe.py` for this reason — see "Why `universe.py` was not touched"
below.

## The other 7 symbols

`BTC/USD`, `ETH/USD`, `LINK/USD`, `LTC/USD`, `BCH/USD`, `DOGE/USD`, and
`UNI/USD` are all effectively 100% complete at daily granularity over the full
5.7-year window. Daily bars aggregating over sub-daily gaps is exactly why:
ADR-0073 measured real intraday holes in several of these same names (e.g.
`LINK/USD` scored 100.3%/79.6% at 5m/1m, `DOGE/USD` and `LTC/USD` failed the
5m floor outright) that simply do not survive being rolled up to one bar per
day.

## Why this is not an ADR

Checked `ls docs/adr | sort -V | tail -3` before writing this: the next
available number is **ADR-0075** (last landed: `0074-walk-forward-and-paper-trial-accounting.md`).
This document is deliberately **not** claiming that number.

Every ADR in this repo records a **decision** — a new default, a new
guardrail, a new config knob, or a reversal of a prior one (ADR-0054's
`MarketCalendar`, ADR-0055's `halt_cooldown_bars` posture, ADR-0060's
`taker_fee_bps`, ADR-0073's own `DEFAULT_MIN_TAPE_DENSITY` floor). This check
changes none of those things: no code was written in `src/trading/`, no
default moved, no new knob was added, and no existing decision was reversed
or amended. It is the same kind of artifact as `docs/crypto-divergence-run.md`
(a run's results, prose plus a table, called out in its own header as "the
answer is in ADR-0061" — i.e., the *decision* lives in the ADR, the *run* is a
plain doc) and as this same measurement's own scoping paragraph in
`docs/crypto-research-pass-2026-09-02.md` §2, which was written as prose with
numbers rather than a numbered decision.

The one place this measurement plausibly *could* motivate a decision — should
`SOL/USD` be dropped from `crypto10`, or should a per-symbol data-quality
caveat become a first-class, enforced thing (a screen alongside ADV/tape-
density, refusing or warning on a symbol below some daily-completeness floor)
— is explicitly **not decided here**. `crypto10`'s exact symbol list is left
unchanged (matching ADR-0073's own precedent: it measured `crypto10` failing
its own tape-density screen at 5m/1m and left the basket as-is because "other
cards may depend on its exact symbols"). If KAN-1079 or a later session
concludes `SOL/USD` should be dropped or gated, that would be the point at
which a decision — and an ADR — gets made. This document's job is narrower:
answer the measurement question KAN-1078 asked, and hand the finding to
whoever makes that call next.

## Why `universe.py` was not touched

The brief for this ticket allowed editing `universe.py`'s SOL docstring
passage *if* this measurement warranted updating or extending it. It does
not: the existing comment already states the correct rate (1,634/2,052 =
79.63%, essentially identical to this session's 79.9%) and the correct
conclusion ("this tape has genuine holes"). Restating the same number with a
longer window's decimals attached would not change what an engineer reading
that file learns from it, and would create two numbers (1,634/2,052 and
2,076-day figures) sitting side by side for no informational gain. The
cross-validation itself — that two independent measurements at different
times agree — is the kind of fact that belongs in a dated results doc (here)
rather than duplicated into a source docstring that is not the place this
bench records measurement history.

## Recommendation for KAN-1079

1. Run at `--interval 1d` on `crypto10` as scoped — the daily tape does not
   carry ADR-0073's intraday tape-density problem, so no additional screening
   or interval change is needed on that basis alone.
2. **Explicitly caveat `SOL/USD` in every result table** KAN-1079 produces —
   its ~20% missing-daily-bar rate is large enough to distort any
   lookback/rebalance-day arithmetic that assumes a bar exists at every
   expected step, and this is now a confirmed, cross-validated, persistent
   property of Alpaca's tape for this symbol rather than a one-off
   measurement artifact.
3. Consider (KAN-1079's call, not decided here) whether to drop `SOL/USD`
   from any *strategy-level* backtest table specifically, versus reporting it
   with the caveat attached — the ADR-0027/0058 precedent in this repo is to
   report a known limitation loudly rather than silently filter it, so the
   default should be "report with the caveat" unless there is a specific
   reason a particular analysis needs a clean tape.
4. `AAVE/USD` and `AVAX/USD` need **no caveat** for this reason — their
   apparent gaps are pure listing-date artifacts, not missing bars, and any
   strategy's warmup/lookback logic naturally handles "this symbol has fewer
   historical bars than others" the same way it already handles `core10`'s
   documented inception-date behavior.
5. Re-run `scripts/crypto_daily_tape_density.py` for whatever exact date
   range KAN-1079 settles on, rather than assuming today's 2021-01-01..
   2026-09-08 window generalizes unchanged — tape density is, per ADR-0073's
   own caveat, noisier day to day than ADV, and a different end date could in
   principle surface a different symbol's gap.
