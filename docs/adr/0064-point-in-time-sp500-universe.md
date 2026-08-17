# ADR-0064: A free-data point-in-time S&P 500 universe — the selection half fixed, the price half measured

- Status: Accepted
- Date: 2026-08-18
- Deciders: strategy developer (project owner)
- Card: KAN-631 ("Point-in-time S&P 500 universe on free data"), rescoped
  2026-08-16 after vendor research ruled out a paid data source. Amends
  ADR-0027 (survivorship bias) rather than closing it.

## Context

ADR-0027 accepted survivorship bias as a known, unfixed limitation: `blue20` is
today's mega-caps, chosen with 2026 hindsight, and yfinance serves price history
only for currently-listed tickers. It named the real fix — "a point-in-time,
survivorship-bias-free constituent database" — and left it for a future slice,
deliberately not scheduled.

KAN-631 originally asked for that database via a paid vendor. The owner decided
against paying for it. The rescoped card asks a narrower, answerable question:
**how much of this can free data actually fix, and what precisely remains
broken?** — for the S&P 500 specifically (not Russell 2000 or S&P 1500: no free
vendor publishes point-in-time membership for those, and this ADR does not
attempt to approximate it).

The rescoped card also cited a secondhand measurement of a *different*, thinner
dataset: 408 change-rows breaking down 1970s=2, 1980s=0, 1990s=8, 2000s=43,
2010s=218 against a real rate of ~20-25 changes/year, concluding PIT membership
is usable only from about 2010. It explicitly instructed: verify this against
whatever you actually use, and report what you measure instead if it differs.

## What is fixable, and what is not

Index **membership** (who was in the S&P 500 on a date) is published,
reconstructible history — Wikipedia's "List of S&P 500 companies" carries a
changes section, and community-maintained derivatives of it are free and MIT
licensed. Index **price history for a name that has since delisted** is not
free: yfinance (and every adapter this bench has) serves only currently-listed
tickers, full stop. So the fix available here is strictly partial:

- **Fixed:** the *selection* mechanism. A backtest universe can now be built
  from what was actually in the index on a historical date, not from a list
  assembled today with full knowledge of who won.
- **Not fixed, and not fixable from this fixture alone:** a delisted
  constituent's price history. The saving grace, stated in ADR-0027 and
  repeated here because it is the reason this is worth doing at all: S&P 500
  removals are overwhelmingly acquisitions and index reshuffles, not
  bankruptcies, and an acquired firm's ticker usually keeps its price history
  on yfinance right up to the acquisition (it stops *listing*, it does not
  retroactively vanish) — so the residual gap is measured here to be real, but
  smaller than the small-cap case, and quantified in the Measurement section
  below rather than merely asserted.

## Decision

### 1. A committed, free-data PIT membership fixture and reconstruction module

`tests/fixtures/sp500_membership/sp500_changes.csv` (694 rows, ~17 KB): one row
per calendar date on which S&P 500 membership actually changed, holding only
the tickers added/removed that date (a **compressed changes table**, not a
daily snapshot dump). Sourced once, by hand, via
`scripts/refresh_sp500_membership.py` — network, manual, never imported by
`src/trading` and never run by a test, per ADR-0040 — from
[`fja05680/sp500`](https://github.com/fja05680/sp500) (MIT licensed), a
community-maintained dataset built from Wikipedia's changes section plus
manual research filling the gaps that page alone leaves (its own README is
explicit that the Wikipedia table is not sufficient by itself). The pulled
file spans **1996-01-02..2026-06-30** (fetched 2026-08-17).

`src/trading/data/sp500_membership.py` reconstructs membership from that
fixture: `load_changes` parses it, `PointInTimeSP500` precomputes a cumulative
snapshot after each change and answers `members_as_of(date)` by bisection, and
a module-level `members_as_of()` is the one-shot convenience. `universe.py`
gets one additive documentation update (its caveat 2) cross-referencing this
module; `Basket`/`BASKETS`/`get_universe`/`get_sector_map`/`validate_universe`
are unchanged, and `blue20`/`core10`/`crypto10` are unaffected — they remain
exactly what ADR-0027/0024 already said they are.

### 2. Measured coverage: usable from 1996, not merely from ~2010

The card's cited secondhand dataset badly under-covers the 2000s. This fixture
does not, measured directly against the committed file:

| Decade | Change-dates/year (range) |
|---|---|
| 1996-1999 | 17-39 |
| 2000s | 10-42 |
| 2010s | 14-32 |
| 2020s | 12-19 |

Every decade from 1996 on shows 10-42 change-dates/year — in the right
ballpark for a real ~20-25/year turnover rate throughout, not concentrated
after 2010. Three independent spot checks against well-known corporate
history all land on the exact date: **TSLA added 2020-12-21**, the
**FB→META ticker change 2022-06-09**, and **GM re-added 2013-06-07** (its
post-bankruptcy re-IPO, correctly distinct from the pre-2009 GM that was
removed in the crash). Membership size stays within 487-507 throughout, per
the upstream README's own accounting.

On that basis, this ADR documents the usable floor as **1996-01-02**, a
stronger claim than the card anticipated, because the source actually used
measures differently — exactly what the card asked for ("if your source's
numbers differ, use what you measure and say so"). One caveat carried forward
rather than smoothed over, from the upstream README itself: the first ~5 years
(1996-2000) may be missing a handful of names the maintainer could not
independently verify (487 constituents in the first row vs. today's
~503-507); the count reaches 494+ by 2001-01-16 and never falls below that
again. Treat 1996-2000 as *slightly* less complete, not as unusable.

### 3. Measurement: `cross_sectional` on a real PIT universe vs. today's

Strategy `cross_sectional` (defaults: `lookback=120`, `top_k=8`, `weight=0.9`,
`rebalance_days=21`), `--source yfinance`, `--no-guardrails` (this strategy's
defaults sit safely under the position cap regardless — `weight/top_k` ≈
11.25% against the 25% default — so guardrails would not bind either way; this
just removes them as a variable rather than changing the result), 2007-01-01
to 2012-01-01 (five years spanning the 2008 financial crisis — the era most
likely to expose real delistings), default $1,000 cash and cost model.

Two universes, each N=50, drawn the same way for a fair comparison — sort the
full membership list, then `random.Random(seed).sample(..., 50)`:

- **"Today"** (the survivorship-biased proxy this repo already ships, via
  `blue20`-style hindsight curation): today's S&P 500 membership (the
  fixture's last date, 2026-06-30).
- **"PIT"**: S&P 500 membership as of 2007-01-01, from this module.

Run twice, with two different seeds, because a single 50-name draw turned out
to be far noisier than expected (see below) and one data point would have
misrepresented that.

| Seed | Universe | Absent/50 | Traded | Total return | Annualized | Sharpe | Max DD |
|---|---|---|---|---|---|---|---|
| 20260817 | Today | 9 (18%) | 41 | +15.17% | +2.87% | 0.24 | 45.07% |
| 20260817 | PIT-2007 | 24 (48%) | 26 | +14.90% | +2.82% | 0.24 | 37.02% |
| 20260818 | Today | 4 (8%) | 46 | -5.82% | -1.19% | 0.07 | 49.30% |
| 20260818 | PIT-2007 | 17 (34%) | 33 | +36.15% | +6.37% | 0.38 | 36.01% |

**The return/Sharpe difference is not stable across the two draws, and that
instability is itself the honest finding.** Seed 20260817 shows the direction
ADR-0027 expects, but tiny (PIT total return 0.27pp *below* Today's — well
inside noise). Seed 20260818 shows a *much larger* difference in the
*opposite* direction (PIT total return 41.97pp *above* Today's). At N=50 over
one five-year window, single-name idiosyncratic outcomes swamp whatever
systematic survivorship effect exists — averaging the two draws into one
headline number would manufacture false precision of exactly the kind
ADR-0027 already refused to do ("we deliberately do not cite a precise
number... treat the magnitude as an order-of-magnitude expectation, not a
correction factor"). This measurement now has concrete evidence *for* that
caution rather than merely stating it. A tighter estimate would need many more
independent draws (or a wider universe) than this card's scope covers; that
is future work, not claimed here.

**What is stable across both draws, and is the more important number: the
absence rate.** Today's universe lost 9% and 18% (mean 13%) of its sampled
names to "no bars in range" — almost entirely companies that simply had not
IPO'd yet by 2007 (ARES, HOOD, CRWD, APP, KEYS, WDAY, CEG, WDAY again in the
second draw — all real, if the wrong kind of absence). The PIT-2007 universe
lost 48% and 34% (mean 41%) — over three times the rate, and for the reason
ADR-0027 named: acquired, renamed, or bankrupt constituents (ABKFQ/Ambac,
BMET/Biomet, BOL/Bausch & Lomb, BRCM/old Broadcom, CA/CA Technologies,
NOVL/Novell, SHLD/Sears, WFM/Whole Foods, HNZ/Heinz, CTX/Centex, and others)
that yfinance simply does not serve. **This is the residual bias ADR-0027's
"mechanism 2" described, now measured rather than assumed**: on this sample,
fixing the *selection* mechanism recovers roughly 60% of a true PIT
universe's tradeable names on free data, not all of it — a real fix, with a
real, non-trivial hole still in it.

## Alternatives considered

| Option | Why not |
|---|---|
| Buy a survivorship-free vendor dataset (the card's original ask) | Explicitly rejected by the owner during 2026-08-16 rescoping. Out of scope by decision, not by oversight. |
| Trust the card's cited secondhand dataset and its ~2010 floor | The instruction was to verify, not inherit. This fixture measures materially better coverage back to 1996; using a worse number when a better one is verified would be dishonest in the opposite direction. |
| Report one measurement (seed 20260817 only) | It happened to show the "expected" small, correctly-signed effect. Running a second seed revealed that expectation does not hold at this sample size, and reporting only the first would have been cherry-picking a result that confirmed the prior. |
| Average the two seeds into one headline number | Manufactures false precision from two wildly disagreeing draws — exactly what ADR-0027 already refused to do with a "fixed haircut". |
| Extend the fixture/module to Russell 2000 or S&P 1500 | Explicitly out of scope: no free vendor publishes point-in-time membership for either, and approximating it would be worse than not building it. |
| Reconstruct historical prices for delisted names ourselves | Would require a paid data source or manual per-name archaeology — the exact cost this rescoping avoided. Left as the named, unfixed gap. |
| Wire PIT membership into a new `@basket`-style CLI sigil now | Not asked for by the card, and a static basket name is the wrong shape for a date-parameterized query. The library module is the deliverable; a CLI surface is a future, separate decision if wanted. |

## Consequences

- A backtest can now build a **real, historically-accurate S&P 500 candidate
  set** for any date back to 1996-01-02 — `trading.data.sp500_membership`,
  offline, no network at call time.
- The selection half of ADR-0027's bias is addressed for the S&P 500
  specifically; `blue20`/`core10`/`crypto10` and everything else ADR-0027
  said about them is **unchanged** — this does not "fix survivorship bias" in
  general, only for the one index this fixture covers.
- The residual price-data gap (delisted names unreachable from yfinance) is
  now **measured**, not merely asserted: roughly a third to a half of a true
  2007 S&P 500 sample is untradeable on free data today, versus under a fifth
  for today's membership sampled the same way. A PIT backtest on free data is
  a real improvement over hindsight curation, but it is still trading a
  reduced, partially-survivorship-affected universe — report it as such.
  Recomputing on more, larger, or different-era samples would sharpen this
  number; it has not been done here.
- The return/Sharpe comparison is **not** a usable correction factor — it
  disagreed in sign and by an order of magnitude between two runs. Do not cite
  "PIT costs/gains N% annualized" from this ADR; cite the absence-rate finding
  instead, and treat any single-run return comparison (on this or any other
  universe) as one noisy draw.
- `scripts/refresh_sp500_membership.py` exists so the fixture can be updated
  later without repeating this research; a refresh should re-run the three
  spot checks in its own docstring before the new file is committed.
- Nothing in `src/trading/sweep.py`, `metrics.py`, `config.py`, `liquidity.py`,
  `cli.py`, or `docs/research-playbook.md` changed — this card stayed
  disjoint from concurrent lanes (KAN-620, KAN-862) by construction.

## See also

- `docs/adr/0027-survivorship-bias.md` — amended by this ADR (see its own
  "Amendment (2026-08-18)" section) rather than closed; everything it says
  about `blue20`/`core10`/`crypto10` and the general mechanism still holds.
- `src/trading/data/sp500_membership.py` — module docstring carries the same
  coverage/measurement detail as this ADR, kept in sync deliberately.
- `scripts/refresh_sp500_membership.py` — the manual, occasional refresh path.
