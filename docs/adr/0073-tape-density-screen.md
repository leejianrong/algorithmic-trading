# ADR-0073: Screen the crypto universe by venue tape density, not market cap

- **Status:** accepted
- **Date:** 2026-08-23
- **Card:** KAN-863 ("Screen the crypto universe by venue tape density, not
  market cap")
- **Builds on:** ADR-0029 (the ADV screen and its no-look-ahead formation
  window — reused verbatim), ADR-0054/0056 (`MarketCalendar` /
  `Frequency.periods_per_year` — reused for "how many bars *should* exist"),
  ADR-0058 (the crypto venue, `AlpacaClient`, the seam-widening pattern
  `cancel_order`/`get_splits` set), ADR-0061 (the crypto fill-cost measurement
  that first surfaced this as a side finding), ADR-0063 (the sibling
  liquidity-tiering screen this module is built parallel to)

## Context

KAN-710/ADR-0061 measured Alpaca's crypto fill cost and, as a side finding,
noticed that `fill_divergence.csv`'s reference price went stale when the
venue's own tape skipped intervals — and that the skip rate varied wildly by
coin, not by coin size: `LINK/USD` printed 100.3% of its possible 5-minute
bars over a day while `ETH/USD`, the second-largest cryptoasset by market
cap, printed only 47.6%. KAN-863 asks the question that finding raises
directly: if a crypto universe is built by picking coins that seem liquid
globally (market cap, exchange volume elsewhere), does it actually reflect
what Alpaca's own tape can support?

Nothing in the bench measures this today. `liquidity.py`'s ADV screen
(ADR-0029) asks "did enough dollars trade" — a *global* depth question, and
one `AlpacaAdapter.get_bars` cannot even answer well for crypto, since
`Bar.volume` truncates fractional coin counts to an integer (ADR-0058's known
gap). Picking a crypto universe by market cap, as `crypto10` implicitly did
(ADR-0024), assumes global liquidity and this venue's order flow move
together. This ADR measures that assumption directly and finds it false.

## What was measured

All measurements below are real, read-only calls against the live Alpaca
paper account (`RealAlpacaClient(asset_class="crypto")`; crypto market data
needs no credentials at all, per ADR-0058) — no order was placed, nothing in
the account was mutated. Methodology: fetch real bars for `[window_start,
window_end]` at a given interval and divide the actual bar count by the
*expected* count for a continuous market over that span
(`MarketCalendar.periods_per_year(interval) × window_years`, ADR-0054/0056 —
reused rather than a fourth hand-rolled 24/7 day-shape calculation).

**Reproducing the ticket's own cited numbers, exactly.** The ticket quoted
figures "reproduced independently on 2026-08-15 bars." Fetching the same ten
symbols over the same calendar day (`2026-08-15T00:00Z .. 2026-08-16T00:00Z`)
reproduced every one of them to the decimal:

| Symbol | 5m coverage | 1m coverage |
|---|---|---|
| BTC/USD | 98.6% | 63.9% |
| ETH/USD | 47.6% | 12.8% |
| LINK/USD | 100.3% | 79.6% |
| SOL/USD | 58.3% | 17.4% |
| DOGE/USD | 54.5% | 15.3% |
| AAVE/USD | 84.0% | 34.9% |
| PEPE/USD | 77.4% | 44.0% |
| BONK/USD | 95.5% | 61.2% |
| WIF/USD | 91.0% | 39.9% |
| SUSHI/USD | 86.1% | 42.2% |

(LINK's 5m figure exceeding 100% is a real artifact, not a bug: Alpaca
occasionally emits more than one bar inside a nominal 5-minute slot near a
volatile print; it is one extra bar out of 289, and it is on the dense side
of the distribution either way.)

**Enumerating the venue's real listing, not a hand-guessed one.** `1m` and
`5m` coverage against today's date (2026-08-23, `now-24h`) came back close to
100% for every symbol in the table above — a different day than the ticket's,
markedly *more* active. That single observation is itself a finding, held for
the "Point-in-time, noisier than ADV" note below: tape density is **not** a
fixed property of a coin, it moves day to day with real order flow, so a
screen built on one day's numbers is a snapshot, exactly like ADR-0028's
broker-verification snapshot.

`client.list_assets()` (this ADR's seam widening — see Decision) enumerated
**73** total crypto assets, of which **36** are `/USD`-quoted. Excluding the
four pegged/stablecoin pairs already excluded from `crypto10` for an
unrelated reason (`USDC/USD`, `USDT/USD`, `USDG/USD`, `PAXG/USD` — ADR-0058's
module docstring: "a pegged asset has no trend or relative strength to
rank") leaves **32** non-stablecoin `/USD` candidates — matching the ticket's
own "~32" figure exactly, which is itself evidence the candidate-set
definition (real listing, minus stablecoins, USD-quoted) is the one the
ticket had in mind rather than a coincidence of two independently guessed
numbers.

Screening all 32 over the ticket's own 2026-08-15 window:

**5-minute bars** (default floor 0.80 — see Decision for why): **19 of 32**
cleared it (LINK 100.3%, BTC 98.6%, UNI 97.6%, CRV 95.8%, BONK 95.5%, LDO
93.8%, HYPE 93.4%, ARB 91.3%, BAT 91.0%, DOT 91.0%, WIF 91.0%, SHIB 90.6%, ADA
89.6%, AVAX 88.9%, XTZ 87.5%, SUSHI 86.1%, FIL 85.1%, ONDO 84.4%, AAVE 84.0%).
**13 dropped** (BCH 78.5%, GRT 78.5%, PEPE 77.4%, POL 76.0%, TRUMP 74.3%, LTC
72.9%, YFI 72.6%, SKY 70.8%, XRP 67.7%, RENDER 64.2%, SOL 58.3%, DOGE 54.5%,
ETH 47.6%).

**1-minute bars**, same floor, same day: **0 of 32** cleared it. The best
candidate, LINK, measured 79.6% — a hair under the 0.80 floor. This is the
ticket's second consequence made concrete rather than argued: fine-interval
crypto is not viable on this venue at all, at least on a day like
2026-08-15.

**Daily bars are unaffected**, confirming the ticket's third claim directly:
five symbols spanning the coverage extremes above (`ETH/USD`, `DOGE/USD`,
`SOL/USD`, `BTC/USD`, `LINK/USD`) all measured **exactly 100.0%** daily
coverage over a trailing 30-day window. A daily bar aggregates the whole day,
so a missing 5-minute print inside it is invisible at that cadence.

**A finding about `crypto10` itself.** Screening `crypto10`'s own ten symbols
(`BTC/USD, ETH/USD, SOL/USD, LINK/USD, LTC/USD, BCH/USD, DOGE/USD, UNI/USD,
AAVE/USD, AVAX/USD`) at the 0.80 floor on 2026-08-15: at 5m, **4 of 10** fail
(`ETH` 47.6%, `SOL` 58.3%, `DOGE` 54.5%, `LTC` 72.9%); at 1m, **all 10** fail
(the best, `LINK`, is 79.6%, still under the floor). `crypto10` was hand-picked
in ADR-0058 for exactly the reason this ADR argues against — recognisable coin
names and market presence, not measured venue order flow — and this is the
predicted consequence, observed. **This ADR does not change `crypto10`'s
symbol list.** The basket ships today, other cards and every existing
backtest may depend on its exact ten symbols for reproducibility (ADR-0024's
own convention: baskets are hand-curated and changed deliberately, not
silently), and the ticket explicitly asked to prefer adding the screen as a
tool over silently changing a basket. Flagged here as a finding; a follow-up
card should decide whether `crypto10` needs a tape-density-aware sibling or a
documented caveat (**recommended, not filed as a Pandan card by this agent —
see the PR description**).

## Decision

### A sibling screen, not a repurposed one

`trading.tape_density` (`classify_tape_density`-shaped, following
`liquidity.py`'s exact conventions) adds `expected_bar_count`,
`bar_coverage_ratio`, `TapeDensityVerdict`/`TapeDensityScreen` (mirroring
`LiquidityVerdict`/`LiquidityScreen` field-for-field, including
`.unverified`/`.kept`/`.dropped`/`.describe()`), and `screen_by_tape_density`.
It cannot be folded into `screen_by_adv`: ADV divides dollars by bar count, so
a missing bar there just shrinks a denominator and a thin-but-present tape
still produces a number; tape density asks "was there a bar to sample *at
all*" — a structural completeness question a size-based average cannot
express. The two disagree in practice: `BTC/USD` is the venue's deepest
market by dollar volume and still misses more than a third of its 1-minute
bars (measured: 63.9%).

**No look-ahead, reused rather than re-derived.** `screen_by_tape_density`
calls `trading.liquidity.formation_window` directly — the exact function
`screen_by_adv` uses — so the same no-look-ahead guarantee (ADR-0001, ADR-0029)
applies with no second implementation to keep in sync (ADR-0035's reuse rule,
now cited for roughly the sixth time in this repo's history).

**Expected bar count is derived from `MarketCalendar`, not hand-rolled.**
`expected_bar_count(window_start, window_end, freq)` computes
`freq.calendar.periods_per_year(freq.delta) × (window_span /
timedelta(days=freq.calendar.days_per_year))` — reusing the exact
`periods_per_year` arithmetic ADR-0054 built and ADR-0056 confirmed against
generated data, rather than re-deriving "a continuous market has 1440
minutes × 365 days" a third time in this codebase. It **requires a continuous
calendar** (`freq.calendar.is_continuous`) and raises otherwise: a session
market's expected bar count also depends on *which hours* are open, which
`periods_per_year` alone does not encode, so silently answering that question
for `us_equity` would be exactly the wrong-market arithmetic ADR-0054 exists
to prevent. The `Frequency` — not a bare `timedelta` — is the input for the
same reason: a caller cannot pass a `"5m"` interval without also carrying the
calendar that says what a "possible bar" means on that market.

### Seam widening: `AlpacaClient.list_assets()`, not a hand-curated candidate list

The ticket posed this as a real choice, and it was resolved in favor of
enumerating the venue's actual listing. A hand-curated "~32 coins" list would
have needed to be *right about which ~32 exist today* to be a valid screening
input, and getting that wrong (an outdated list, a coin Alpaca delisted, one
it added since) would silently narrow or bias the candidate set before the
tape-density measurement ever runs — the exact kind of unmeasured assumption
this ticket exists to remove. `RealAlpacaClient._crypto_symbol_map()`
(ADR-0058) already builds this from `get_all_assets`, but only as a private
helper for position-symbol canonicalization; there was no public way to ask
"what does this venue list at all."

`list_assets()` is the **eighth call on the seam**, following the exact
widening pattern `cancel_order` (ADR-0036, "the sixth call... the widening
ADR-0017 anticipated") and `get_splits` (ADR-0045, "the seventh call") set:
`Protocol` method + `FakeAlpacaClient` implementation + `RealAlpacaClient`
implementation, scoped to the client's own `asset_class` (a construction
property, exactly as `feed` and the interval already are) rather than a new
per-call argument. `_crypto_symbol_map` now derives from `list_assets()`
instead of issuing its own parallel `get_all_assets` request — one fewer
place to keep the SDK response-shape handling in sync, and `test_alpaca_crypto.py`'s
existing `test_the_protocol_gained_no_crypto_specific_call` test is updated
(not weakened) to record that this widening is unrelated to crypto itself:
the seam gained no method *because* of the venue split (that claim still
holds for the original seven calls); `list_assets` exists because a
tape-density screen needed to enumerate a listing, which is a different
motivation from anything ADR-0058 required.

`FakeAlpacaClient.list_assets()` returns exactly what a test registered via
`assets=`/`set_asset` — it has no notion of "everything a venue lists" beyond
what was declared, unlike `get_asset`, which invents a fully-usable default
for an unscripted symbol. That asymmetry is deliberate: there is no wrong
answer to guess at for an enumeration nobody scripted, so an unscripted fake
enumerates as empty rather than fabricating a listing.

### The floor: 0.80, placed in a real measured gap

`DEFAULT_MIN_TAPE_DENSITY = 0.80`, chosen the way ADR-0063's
`DEFAULT_TIER_ADV_FLOOR` was: from a **gap in a real measurement**, not a
round number picked in advance. Screening the 32 real candidates at 5m on
2026-08-15 produced a cluster of 19 symbols at or above 84.0% and a second,
clearly worse cluster starting at 78.5% — 0.80 sits in that ~5.5-point gap.
The same floor applied to 1m data keeps **zero** of the 32 candidates (the
best, LINK, measured 79.6%) — which is not a flaw in the floor's placement,
it *is* the ticket's second finding, made mechanically visible rather than
argued in prose.

`DEFAULT_TAPE_DENSITY_FORMATION_DAYS = 1` — one calendar day, shorter than
ADV's 90-day default. Two reasons, both explicit in the module docstring
rather than left implicit: (1) it matches the exact single-day methodology
this default was validated against, so the "0.80 sits in a measured gap"
claim above is reproducible from the module's own default rather than from a
different window than what was measured; (2) unlike ADV, which wants a
quarter to average out an earnings-day spike, a week of 1-minute bars for 32
symbols is already ~322k rows to fetch — a real cost this ADR does not want
to impose by default. The cost of "short" is stated plainly, not hidden: tape
density is **noisier day to day than ADV**, measured directly — `BTC/USD` read
98.6% on 2026-08-15 and 100.0% eight days later; `ETH/USD` read 47.6% on
2026-08-15 against 89.6% averaged over the trailing week ending 2026-08-23. A
single-day formation window can therefore read more pessimistically or more
optimistically than a symbol's typical behavior on this venue. A caller who
wants a smoother read passes a larger `formation_days`; this is documented as
a real, unresolved trade-off (a "known gap", not a solved problem), exactly
the way ADR-0029 documents its own formation window as point-in-time rather
than rolling.

### CLI wiring: `backtest --min-tape-density`, additive, off by default

Mirrors `--min-adv` exactly: `--min-tape-density FLOAT` (default `None`, off)
and `--tape-density-window DAYS` (default `DEFAULT_TAPE_DENSITY_FORMATION_DAYS`).
`_apply_tape_density_screen` mirrors `_apply_liquidity_screen` line for line —
prints `screen.describe()`, exits 2 with a clear message if the screen empties
the universe rather than silently running nothing. A new
`_check_tape_density_options` gate (mirroring `_check_symbol_shapes`'s exit-2
shape) refuses `--min-tape-density` on a market whose calendar is not
continuous, **before any fetch**, for the same reason `expected_bar_count`
raises: a session market's expected bar count is not what this screen
computes, and silently answering it wrong (or crashing mid-fetch) would be
worse than refusing up front. A run without the flag prints exactly the bytes
it always did — no existing test's output changes.

Deliberately **not** wired into `crypto10`'s definition, `sweep`, or `paper` —
same reasoning ADR-0063 gave for its own tiering flag being `backtest`-only:
the minimal, additive shape a real measurement justifies, not a
larger refactor.

## Alternatives considered

**Fold tape density into `screen_by_adv`.** Rejected: they measure different
things (a count vs. a size-weighted average) and conflating them would hide
exactly the disagreement this ADR measured (`BTC/USD` deepest by dollars,
badly under-sampled at 1m).

**A hand-curated ~32-coin candidate list**, matching how `crypto10` itself
was built. Rejected for the reason given above: the whole point of this card
is to screen by *measured* venue behavior rather than a human's guess, and a
hand-guessed candidate list is exactly the kind of unmeasured assumption a
market-cap-picked universe already represents — using one to build the
screening input would undermine the screen's own premise.

**Silently swap `crypto10`'s low-density coins for denser ones.** Rejected
per the ticket's explicit guidance: other cards/backtests may depend on
`crypto10`'s exact ten symbols for reproducibility, and a basket's contents
should change deliberately (as ADR-0024/ADR-0058 changed it, each time with
its own ADR) rather than as a side effect of an unrelated screening feature.
Recorded as a finding and a recommended follow-up instead.

## Consequences

### Equity, and every existing crypto invocation, is unaffected

`--min-tape-density` defaults to `None`; without it, `backtest` prints
exactly what it always did. `screen_by_tape_density` is a new, opt-in library
function with no caller inside `cli.py`'s existing paths. `list_assets()` is
a new `AlpacaClient` method with a default (empty) fake implementation; no
existing production code calls it except the refactored
`_crypto_symbol_map`, which is asserted to return the identical map (a live
test, `test_list_assets_agrees_with_the_symbol_map`, checks this against the
real venue).

### What this does and does not settle

Like ADR-0052/0061/0063 before it, this is a **measurement and a mechanism**,
not a final universe. It establishes that tape density is real, venue-specific,
and disagrees sharply with market cap — and it gives an operator a tool to act
on that. It does not say which 19 (or fewer) coins *should* replace `crypto10`,
does not smooth day-to-day noise beyond what a longer `formation_days` buys,
and does not touch the ADV/volume-truncation gaps ADR-0058 already recorded
(`Bar.volume: int` truncates fractional crypto volume, so the ADV screen
remains unusable on this tape for an unrelated reason).

### Known gaps

- **`crypto10` is unchanged**, despite 4/10 (5m) or 10/10 (1m) of its own
  symbols failing the default floor on the day measured. Recorded as a
  finding for a human to act on, not silently fixed — see the PR description
  for the recommended follow-up.
- **`paper` and `sweep` have no `--min-tape-density`.** Only `backtest` is
  wired, matching ADR-0063's own gap list for its sibling flag.
- **`result.json` does not record the screen or its parameters.** The same
  gap ADR-0063 left for its own tiering flag.
- **One calendar day is a noisy sample.** Day-to-day coverage swings of 40+
  percentage points were measured directly (`ETH/USD`: 47.6% vs. 89.6% eight
  days apart). A caller who needs a stable universe across runs should pass a
  larger `formation_days` and accept the larger fetch, or re-screen before
  each real allocation decision — this module does not average across days by
  default.
- **Point-in-time, not rolling**, the same limit ADR-0029 already documents
  for the ADV screen: a symbol whose tape thins out mid-run stays in the
  universe.
- **The reference-staleness question ADR-0061 originally raised is still
  open.** This ADR answers "which symbols are dense enough to trust," not
  "how much does staleness widen the divergence error bar" (KAN-863's other
  half, per ADR-0061's closing note) — that remains for a future card that
  measures divergence quality directly rather than venue tape coverage.
