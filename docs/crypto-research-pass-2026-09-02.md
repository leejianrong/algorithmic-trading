# Crypto research pass — scoping (2026-09-02)

> This is a **pre-registration/scoping document, not a research run.** No backtest,
> sweep, or paper command was executed to produce it — everything below comes from
> reading `src/trading/` and the ADRs already cited in `CLAUDE.md`. Its job is the
> same one [`docs/deployment-decision-2026-09-01.md`](deployment-decision-2026-09-01.md)
> did for KAN-642 on equities: answer every question in
> [`docs/research-playbook.md`](research-playbook.md) §1-2 *before* a future session
> touches any data, so that session executes a decided plan instead of making these
> calls with a result already in view. Answers KAN-1077 (EPIC-140, "Crypto strategy
> research (KAN-642 for crypto)").
>
> **This is not the same question as
> [`docs/crypto-divergence-run.md`](crypto-divergence-run.md).** That page (and
> ADR-0061) already answered "is the modelled 5 bps slippage / 22 bps fee real on
> crypto" — a fill-cost measurement question, done. This document is about whether
> any of the five candidate *strategies* has a real edge on crypto data at all — a
> strategy-research question, not yet attempted. A future execution of this pass
> will *use* the already-measured cost numbers (§3 below), not re-measure them.

## 1. Data source — `--source alpaca` is mandatory

`yfinance` has no crypto coverage at all — `YFinanceAdapter` is an equity-only
adapter and was never extended for crypto pairs. `SyntheticAdapter` can *generate*
a continuous 24/7 series (ADR-0056) and is exactly what earlier crypto-groundwork
ADRs (0053-0057) used to test the calendar/completeness/risk-posture plumbing
offline — but its own docstring says it plainly: "still GBM (a 20% down day is
~6.4σ at 60% vol, where real crypto has them)", "no inception date, no maintenance
window", and it is explicitly flagged as "more forgiving than a provider may be"
(ADR-0056, ADR-0058). A strategy backtest is exactly the case this warning is
aimed at: a mean-reversion or trend strategy tuned against a GBM series with no
fat tails, no liquidation cascades, and no real tape holes would tell us nothing
about whether the edge survives contact with actual crypto price action. **A
future execution of this pass must use `--source alpaca`, real historical data,
for every step from the cheap kill test onward.** `--source synthetic` may still
be useful for a pure plumbing smoke test (does the command even run under
`--market crypto`) before spending real fetches, but no number from it should
appear in any hypothesis-confirming table.

**`AlpacaAdapter` has no bar-level cache — verified by reading the source.**
`src/trading/data/alpaca_adapter.py` carries exactly one cache,
`self._split_cache: dict[tuple[str, date, date], list[SplitEvent] | None]`
(line 151), used only by the adjusted-series split-verification guard (ADR-0045)
— which is irrelevant to crypto, since crypto has no splits or dividends
(ADR-0058). There is no `self._bar_cache`, no read-through CSV cache, nothing
analogous to what `YFinanceAdapter` has. By contrast,
`src/trading/data/yfinance_adapter.py`'s whole first module-docstring section is
about its "read-through cache + injectable fetcher" — every `(symbol, start, end)`
window is written to and read from a CSV file under a `cache_dir` (conventionally
`.cache/data`), so a sweep that requests the same range repeatedly (every combo in
a grid, every fold's IS re-fetch) pays the network cost **once**.

**Consequence: every sweep trial in a future execution of this pass re-hits the
live Alpaca venue.** The equity KAN-642 pass explicitly relied on the yfinance
cache being warm ("`.cache/data` warmed once per universe/range so sweep combos
are compute-bound, not network-bound" — `deployment-decision-2026-09-01.md`) and
still hit real trouble: `cross_sectional`'s walk-forward folds each requested a
**fresh, uncached** ~500-symbol batch (because a fold's IS/OOS sub-range is a
range the cache had not seen before) and that was the single biggest driver of
the resource exhaustion that killed that candidate's OOS step. A crypto pass has
**no cache at all**, so *every* grid combo, *every* fold, *every* confirm run is a
fresh round-trip to Alpaca for the whole universe — not just the ranges a cache
hasn't warmed yet, all of them, every time. Budget accordingly:

- **Narrower grids than the equity pass used.** The equity pass swept up to 16
  combos per candidate; a crypto pass sweeping the same grid sizes multiplies the
  network cost by the grid size with no caching amortization. A future execution
  should start with the smallest defensible grid per candidate (playbook step 3's
  spirit, applied to sweep sizing too) and widen only if step 3's cheap kill test
  survives.
- **Fewer folds than the equity pass's default of 3-4.** Each walk-forward fold
  issues its own IS+OOS fetch for the whole universe (`crypto10` is only 10
  symbols vs. `@sp500`'s ~500, which helps, but every fold is still a full
  re-fetch with zero cache reuse). 2-3 folds is a more defensible starting point
  than the equity pass's default.
- **Rate-limit risk is real and unforced.** Alpaca's data API has request-rate
  limits; a sweep with no cache issuing dozens of trial-level fetches for a
  10-symbol universe over a multi-year daily range is squarely the kind of
  request volume that can trip a limit mid-sweep, and — per ADR-0040's repeated
  lesson (cited six times in `CLAUDE.md` already) — a provider refusal that looks
  like a data regression is exactly the failure this bench keeps re-discovering
  in new subsystems. A future session should watch for and distinguish a rate
  limit (retry-able) from a genuine data absence (not retry-able) rather than
  assuming either.
- **No `--folds` OOS repetition without cost.** Unlike the equity pass, which
  could re-run a fold cheaply once the range was cached, every retry of a killed
  crypto sweep pays full price again. A future session should size the *first*
  attempt conservatively rather than planning to iterate the grid size up.

## 2. Universe — `crypto10` at `--interval 1d`, daily tape holes unmeasured

`crypto10` (`src/trading/universe.py`) is the only real curated crypto basket:
10 USD-quoted Alpaca pairs (`BTC/USD`, `ETH/USD`, `SOL/USD`, `LINK/USD`,
`LTC/USD`, `BCH/USD`, `DOGE/USD`, `UNI/USD`, `AAVE/USD`, `AVAX/USD`). No second
crypto basket exists, and no PIT crypto membership tool exists either (relevant
to §4's `cross_sectional` discussion below).

**Tape-density screening intraday is already measured, and cited rather than
re-derived (ADR-0073).** `screen_by_tape_density` measured `crypto10`'s own ten
symbols against Alpaca's real tape on 2026-08-15: **4/10 fail the screen at 5m**
(ETH, SOL, DOGE, LTC — the default 0.80 coverage floor) and **10/10 fail at 1m**.
The module's own docstring states the headline finding independent of this basket:
"fine-interval crypto is not viable on this venue" — ETH, a top-3 coin by market
cap, printed only 47.6% of possible 5m bars and 12.8% of possible 1m bars over a
full day on Alpaca's own tape, while LINK printed effectively all of them. This
number does not need re-measuring for this scoping document; it is settled.

**Decision: run this pass at `--interval 1d`, not intraday.** Two paths were
available and this document picks the first, for a reason that has nothing to do
with convenience:

1. **Run all five candidates at `--interval 1d` on the full `crypto10` universe.**
   Daily bars aggregate over whatever intervals a coin's tape happened to skip
   within a day — if BTC traded on Alpaca at some point during a UTC day, the
   daily close reflects that trade whether or not every 5-minute slot inside the
   day had one. This sidesteps the intraday tape-density problem entirely rather
   than screening around it, and it is also the exact interval the equity
   KAN-642 pass itself used (`docs/deployment-decision-2026-09-01.md`'s frozen
   table: every candidate at daily bars, 2008-2023). Using the same interval
   makes any eventual cross-market comparison ("does this strategy family work
   on crypto the way it does on equities") an apples-to-apples methodological
   choice, not an artifact of one pass using daily bars and the other using 5m.
2. **Screen `crypto10` down to the ~6 symbols that pass the 5m tape-density
   floor and run intraday.** Rejected for this pass: `cross_sectional` (§4)
   already has weak statistical power on a 10-name universe; 6 names is worse
   still, and running the *other four* candidates on a screened-down universe
   too (for consistency) would mean the whole pass' equity-vs-crypto comparison
   moves on two axes at once (fewer symbols *and* a different interval) rather
   than one.

Daily was chosen, not left as the only option — a future session could still run
an intraday variant on the tape-density-screened subset as a *separate*, smaller
follow-up once daily results exist, but that is out of scope for this pass's
first execution.

**Open, unmeasured question, stated explicitly: does Alpaca's *daily* crypto tape
have the same holes ADR-0073 measured at 5m/1m?** Nobody has checked. ADR-0073's
own numbers are 5m and 1m only; `universe.py`'s comment on SOL notes "this tape
has genuine holes... 1,634 bars of 2,052 days" for daily-frequency historical
data specifically (a different, earlier measurement — ADR-0058, not ADR-0073),
which is itself evidence that daily crypto data on this venue is **not**
guaranteed complete just because it aggregates over sub-daily gaps. A day with
zero trades printed at all would still be a missing daily bar, and a strategy's
lookback/rebalance-day arithmetic assumes a bar exists at every expected step
the way it does on an equity session with weekends already excluded by the
calendar. **Recommended first cheap step if this pass is ever executed:** before
running any strategy, pull `crypto10`'s daily bars over the intended range via
`--source alpaca` and directly measure bar-count coverage against
`calendar.CRYPTO_24_7`'s expected 365 bars/year (the same arithmetic
`tape_density.expected_bar_count` already does, just applied at `1d` instead of
`5m`/`1m` — no new code needed, this is a measurement, not a feature). This is
cheap: 10 symbols, one daily fetch each, no strategy execution. Trusting any
daily crypto Sharpe from this pass without first running that check is exactly
the "offline stand-in more forgiving than the provider" trap ADR-0040/0056/0058
keep re-finding in new subsystems — except here the risk is a *provider* gap
hiding inside real data, not a synthetic stand-in.

**`crypto10`'s survivorship bias applies here, undiminished.** `universe.py`'s
own docstring (caveat 4) calls this "the worst of the three" baskets: the tokens
are 2026's survivors of a much higher-failure-rate market, the venue itself is a
survivor filter (Alpaca delists losers, so "what Alpaca lists today" is already
curated), and Alpaca's crypto tape starts 2021-01-01 with no way to reconstruct
delisted pairs even manually. Nothing in this scoping document changes that. Any
result from a future execution of this pass is an upper bound on an upper bound,
exactly as ADR-0058/0073 already say, and should be reported with that caveat
attached every time a number is quoted — the same discipline the equity pass's
verdict document applied to `blue20`.

## 3. Cost model — proceed on the shared 5.0 bps default, caveat stated loudly

`CostConfig.crypto()` (`src/trading/config.py`) has two terms, and they are on
very different evidentiary footing:

- **`taker_fee_bps` is well-measured.** ADR-0060 sourced Alpaca's published
  tier-1 taker rate (25 bps) from the fee schedule and ADR-0061 independently
  re-derived it from real position-delta arithmetic on a live session — **exact
  agreement, 22.0000 bps identically across eight different pairs**, confirming
  both that the fee is real and that it is not per-pair. This term needs no
  further work for this pass; use `CostConfig.crypto()`'s default (or
  `--taker-fee-bps` if the account's actual tier is known to differ, per
  `docs/crypto-divergence-run.md`'s reconciliation script).
- **`slippage_bps` is NOT well-measured, and stays at the shared 5.0 bps default
  regardless.** ADR-0061 measured **+13.02 bps mean** on crypto (optimistic —
  the model overstates the real cost) against equities' **+0.51 bps** (ADR-0052,
  conservative — the opposite sign) on only **11 paired fills**, below
  `MIN_PAIRED_FILLS = 30`, and — the sharper problem — **8 of those 11 fills
  shared one market instant** (a single strategy warmup burst), so the effective
  independent sample is closer to 4 than 11. The finding itself (equities
  conservative, crypto optimistic, same constant, opposite signs) is real and
  already recorded; the crypto *level* is not yet a measurement by this bench's
  own stated bar.

**Decision: proceed on `CostConfig.crypto()`'s shared 5.0 bps slippage default,
with the ADR-0061 caveat repeated loudly in any write-up this pass produces —
this pass does not wait for a ≥30-independent-paired-fill crypto divergence run
before starting.** Two grounds, both explicit rather than assumed:

1. **Precedent.** The equity KAN-642 pass itself proceeded on the equity cost
   defaults while they were still, at the time, provisional in exactly this
   sense — ADR-0052's own 60 paired fills were called "one afternoon, one venue,
   twenty mega-caps" and `slippage_bps` was explicitly *not* re-tuned to the
   measured 0.51 bps mean, on the reasoning that the measurement was "the same
   order as the reference-price noise floor" and that paper fills are simulated
   rather than routed. KAN-642 did not wait for a bulletproof cost model before
   researching strategies against the provisional one; this pass follows the
   same precedent on the crypto side, where the provisional-ness is more severe
   (11 fills, not 30, and known non-independence) but the category of decision
   is the same.
2. **CLAUDE.md's own domain invariant is about guardrails, not cost research
   gating.** "Calibrate a guardrail; never widen it until nothing trips" (the
   halt-recovery invariant, ADR-0055) is a rule about *risk limits* — the thing
   that stands between a strategy and account-destroying losses. It is not a
   rule that says "never research a strategy until every cost input has 30
   independent samples." A strategy backtest with a slightly-wrong cost
   assumption produces a slightly-wrong Sharpe, which the playbook's own cost-
   sensitivity step (§6, `--slippage-sweep`, ADR-0069, already built) is
   designed to catch — run it, and if the candidate's edge only survives inside
   the gap between the two measured directions (below +0.51, above +13.02), that
   is itself the finding to report, not a reason the pass could not run.

**What "stated loudly and repeatedly" means concretely for a future write-up:**
every table reporting a crypto Sharpe/return number should carry a footnote
naming the ADR-0061 direction-and-magnitude caveat, the same way the equity
pass's every table implicitly inherited ADR-0027's survivorship caveat from
`CLAUDE.md`'s framing — not a single disclaimer paragraph at the top that the
rest of the document can be read past.

## 4. Strategy candidates and their crypto-specific hypotheses

Mechanical applicability (does the strategy's code run against `crypto10` under
`--market crypto`) is separate from hypothesis validity (does the *reason it
might work* still make sense on this market), and the two questions get
different answers for at least two of the five candidates.

### `sma_crossover` and `momentum` — mechanically fine; the equity mechanism needs replacing, not reusing

Both are single-symbol, transition-driven, long-or-flat strategies
(`strategies/sma_crossover.py`, `strategies/momentum.py`) with no cross-sectional
ranking and no asset-class assumptions baked into the code — they run against
any `dict[str, Bar]` the engine hands them. Nothing in either file references a
market session, a settlement convention, or an equity-specific data field.
Mechanically, both drop into `--market crypto --symbols @crypto10 --source
alpaca` with no code change, exactly as ADR-0058 already demonstrated for
`sma_crossover` (the ADR-0061 divergence run *was* an sma_crossover/crypto10
session).

The **hypothesis** is where reuse breaks down. The equity pre-registration
(`deployment-decision-2026-09-01.md` §1-2) is explicit: "large, index-heavy
holders (funds tracking benchmarks, pension rebalancers) cannot instantly
reprice a mega-cap... flows... throttled by market-impact-aware execution
schedules," with disposition-effect retail sellers as the counterparty. That
story assumes an institutional, benchmark-driven investor base and an execution
practice (multi-day impact-aware order slicing) that is a fact about *equity
market structure*, not about trend persistence in general. Nothing about crypto
markets today obviously has that same institutional-flow-throttling mechanism
at the same scale — pension funds and index trackers are not (yet) the dominant
holder base for `crypto10`'s ten pairs.

A crypto-native mechanism needs its own story, not a copy-paste of the equity
paragraph. Two candidate framings, offered as drafts a future session should
pick between (or write a better one) rather than as settled claims:

- **Retail narrative-chasing.** Crypto price action is disproportionately driven
  by retail attention cycles — a coin trends on social media, retail capital
  flows in over days to weeks as the narrative spreads, and a moving-average or
  trailing-return signal is a lagging proxy for "the narrative has actually
  taken hold" rather than an early entry into it. The counterparty would be
  retail traders who buy the initial spike and sell too early on the first
  pullback (the crypto analogue of the equity disposition effect, arguably
  *stronger* here given crypto retail's documented tendency toward short holding
  periods and panic-selling on volatility).
- **Algorithmic momentum funds now active in crypto.** A newer, more
  institutional framing: CTA-style and crypto-native quant funds increasingly
  run trend-following books on major pairs, and if enough capital runs the same
  signal at similar horizons, the resulting flow *is* the persistence — a
  self-reinforcing, not fundamentally-driven, mechanism. The counterparty here
  is less clear (this is close to "the mechanism is other algos, and the
  counterparty is late/discretionary traders who buy the top of a
  momentum-driven move") and is honestly the weaker of the two stories.

Neither of these has been checked against any actual data or literature by this
scoping pass — they are draft mechanism candidates for whoever executes the pass
to adopt, discard, or replace before writing the actual `--hypothesis` string.

### `mean_reversion` — a genuinely stronger crypto story, not a re-test of a known failure

`mean_reversion` (`strategies/mean_reversion.py`) is mechanically identical in
shape to the two above — single-symbol, RSI-threshold-driven, long-or-flat — and
runs against `crypto10` with no code change.

On equities this candidate **failed twice** (ADR-0071's original measurement and
KAN-642's wider-universe replication: -237pp vs. SPY, paired win rate 47.7%,
deflated significance never durably above 0.95). It would be a mistake to treat
that as a prior against running it on crypto, for a specific, mechanism-level
reason this section exists to make explicit rather than gloss over.

The equity hypothesis was "short-horizon liquidity provision... a sharp,
single-name drawdown in a mega-cap is disproportionately likely to be a forced
or panic sale (a margin call, an index-fund rebalance, an overreaction to
noise)." On equities that mechanism is real but relatively rare and diffuse —
margin calls on mega-caps are not a frequent, mechanically-triggered event.
**Crypto has a structurally cleaner, more frequent version of exactly this
mechanism: leveraged-position liquidation cascades.** Perpetual futures and
margin trading dominate crypto derivatives volume, liquidation engines close
over-leveraged positions automatically and mechanically at specific,
often-clustered price levels (not a discretionary margin call that may or may
not fire), and cascading liquidations — one forced sale pushing price into the
next cluster of liquidation triggers — are a well-documented, recurring feature
of crypto market structure in a way that has no clean equity analogue. If the
"forced/panic seller" story is the mechanism mean-reversion is really betting
on, crypto may be the market where that mechanism is most mechanically real,
not least.

This deserves its own hypothesis, not the equity one with "crypto" substituted
in, and not a dismissal on the strength of the equity result. A draft framing:
mean-reversion buys into a liquidation-cascade-driven overshoot, where the
seller (a liquidated leveraged long, forced out by the exchange's own risk
engine regardless of their view on fair value) is about as close to CLAUDE.md's
required "a specific, named counterparty who keeps losing money and why" as this
bench's five candidates get. Whether this mechanism actually shows up as a
tradeable RSI-threshold pattern on daily `crypto10` bars (rather than needing
intraday resolution to catch a cascade that plays out over hours, which §2
already rules out for this pass) is exactly what a future execution would need
to test — this section's job is only to establish that the equity failure does
not settle the crypto question, and that the crypto mechanism argument is
genuinely different and worth taking seriously.

### `cross_sectional` — mechanically fine, materially underpowered, no fix available today

`cross_sectional` (`strategies/cross_sectional.py`) ranks the whole universe by
trailing return and holds the top-K — nothing in the code assumes equities, and
it runs against `crypto10` with no change (ADR-0058's real 2024 `@crypto10` run
already exercised this class of strategy on this universe, per `CLAUDE.md`'s
crypto-groundwork bullets, though not `cross_sectional` specifically by name in
those notes).

The problem is statistical power, not mechanics. The equity pass ran
`cross_sectional` against `@sp500` — roughly 500 point-in-time constituents,
default `top_k` swept up to 20 — giving real breadth to rank across.
`crypto10` has **10 symbols total**, so a `top_k` of even 5 is already holding
half the universe, and a rotation strategy with that little breadth to rotate
across is a fundamentally weaker test of the cross-sectional-momentum mechanism
(capital chasing current leadership) than the equity version got. **No PIT
crypto membership tool exists to broaden this** — `sp500_membership.py`
(ADR-0064) is S&P-500-specific and there is no crypto analogue; `crypto10` is
also, per §2, the *only* curated crypto basket in the repo, so there is no
larger basket to substitute even ignoring the PIT question. This is stated here
as a real, currently-unaddressed limitation for a future session to accept
going in — not something this scoping card fixes, and not something a
different `--param top_k` value can fix, since the ceiling is universe size,
not the strategy's own knob.

### `trend_following` — its real hypothesis needs a multi-asset-class universe; testing it on crypto10 alone is redundant, not novel

`trend_following` (`strategies/trend_following.py`) is mechanically fine against
`crypto10` in isolation — it is per-asset absolute momentum with no
cross-symbol ranking, same as `momentum`. But its **actual pre-registered
hypothesis** (`deployment-decision-2026-09-01.md` §1, candidate 5) is
specifically about *cross-asset-class* macro regime persistence — "absolute
(not relative) momentum applied across asset classes (equities, bonds,
commodities, currency) captures macro regime persistence" — and that is why
`trend_etfs` (ADR-0070) was built as a 12-ETF basket spanning equities,
international, bonds, commodities, and currency rather than as a single-name or
single-asset-class list. Running `trend_following`'s code against `crypto10`
alone would test something else entirely: single-asset-class (crypto-only)
absolute momentum — which is exactly what `momentum` already tests, on the same
universe, with a simpler and more direct signal (trailing return vs. this
strategy's 12-1-with-skip construction). That is a redundant test dressed up in
a different strategy class, not a novel one.

**A genuinely novel test of this strategy's real claim would add crypto pairs as
a new "digital assets" leg alongside `trend_etfs`'s existing legs in the *same*
run** — i.e., mix `crypto10` symbols into the multi-asset-class universe rather
than substitute for it. This is checked here as **structurally unsupported by
the engine today, not merely inconvenient**: `src/trading/frequency.py`'s
`Frequency` dataclass carries exactly one `calendar: MarketCalendar` field
(line 59), `Frequency.parse` takes one calendar per call
(`src/trading/frequency.py` lines 78-94), and a `DataAdapter`'s interval —
including its calendar — is a single construction-time property (ADR-0022,
carried forward by ADR-0054/0056/0057 without ever becoming per-symbol). A
single backtest run has exactly one `Frequency`, hence exactly one
`MarketCalendar`, hence one annualization basis and one completeness policy for
every symbol in the universe. There is no mechanism today for `SPY` to
annualize on 252×390 while `BTC/USD` in the *same run* annualizes on 365×1440,
or for the daily-bar completeness rule to be session-based for the equity legs
and rolling-24h for the crypto leg simultaneously — exactly the "24/7 bars
annualized on a 252-day year" class of defect ADR-0054/0056/0057 were built to
make unrepresentable, now encountered from the other direction (a *legitimate*
need for two calendars in one run, not a bug that silently mixes them).

**Recommendation: treat mixing crypto into `trend_following` as explicitly OUT
of scope for this pass.** Solving the mixed-calendar problem is real engine
work — deciding whether `Frequency`/`MarketCalendar` becomes per-symbol, how
`RiskConfig`/`CostConfig` posture selection would work when a book spans two
market postures, and how the annualization basis for a *blended* equity-crypto
equity curve would even be defined — and none of that should be improvised
inside a strategy-research scoping document. If pursued, it is its own future
card (engine-level work, likely its own epic given the surface area touched:
`frequency.py`, `calendar.py`, `cli.py`'s `_resolve_market`, `risk.py`,
`config.py`'s cost selection, and `data/recent_window.py`'s per-market
`fetch_span`/completeness policy all currently assume one market per run).

### Recommended candidate list and priority order for a future execution

1. **`sma_crossover`** (highest priority) — mechanically simplest, has the
   strongest existing crypto operational track record in this repo (ADR-0058,
   ADR-0061's divergence run both used it), and its trend-persistence mechanism
   has at least one plausible crypto-native counterparty story to test (retail
   narrative-chasing).

   **Draft hypothesis:** *Mechanism* — crypto retail attention cycles create
   multi-day-to-multi-week trending moves as a narrative spreads through social
   and community channels faster than fundamentals could justify, and a
   fast/slow SMA cross is a lagging proxy for "the narrative has taken hold"
   entered after the move has started rather than at its origin. *Counterparty*
   — retail traders exhibiting the crypto-native version of the disposition
   effect (well-documented in crypto retail behavior: quick profit-taking on
   small gains, reluctance to realize losses until forced), who exit a winning
   trend too early and get run over by its continuation, plus short-horizon
   traders who fade the first leg of a move on the assumption crypto trends
   mean-revert as fast as they extend. *Kill criteria* — genuinely undecided
   here; a future session should set OOS Sharpe / retention / deflated-P /
   paired-win-rate numbers at execution time following the playbook's own
   step-1 discipline, informed by whatever this pass's step-3 cheap kill test
   shows, rather than this document fabricating numbers it has no basis for.

2. **`momentum`** — same mechanism family as `sma_crossover` via a more direct
   measurement (trailing return vs. a moving-average proxy), same reasoning for
   why it is a distinct rather than redundant test (the equity pass made this
   same argument and it transfers unchanged). Run alongside `sma_crossover` as
   a pair, expecting them to be correlated for the same reason the equity pair
   was (0.773) — testing the same underlying claim twice, not two independent
   ideas.

   **Draft hypothesis:** identical mechanism/counterparty framing to
   `sma_crossover` above (same claim, cleaner measurement); kill criteria
   likewise deferred to execution time, matched to `sma_crossover`'s bars for
   the same reason the equity pass matched them.

3. **`mean_reversion`** — the candidate this scoping document argues most
   strongly deserves a real, fair test rather than being skipped on the
   strength of its equity failure. The liquidation-cascade mechanism is a
   materially different and arguably stronger structural story than the equity
   version, and daily `crypto10` bars are at least a defensible (if
   coarse-grained) resolution to look for its aftermath even if the cascade
   itself plays out intraday.

   **Draft hypothesis:** *Mechanism* — a sharp single-pair drawdown on a
   leverage-heavy crypto venue is disproportionately likely to be a
   mechanically-triggered liquidation cascade (forced closes of over-leveraged
   long positions by an exchange's risk engine, clustering at specific price
   levels and self-reinforcing as one forced sale triggers the next) rather
   than new information about the asset's value, and the reversion this
   strategy buys is the price of providing liquidity into that forced-selling
   event once it exhausts. *Counterparty* — leveraged longs being forcibly
   liquidated by exchange risk engines regardless of their own view (a
   structurally cleaner "forced seller" than an equity margin call, since the
   liquidation is mechanical and price-triggered rather than discretionary),
   plus momentum/panic sellers extrapolating the cascade who get run over on
   the snap-back. *Kill criteria* — deferred to execution time; a future
   session should explicitly note in its pre-registration that this candidate
   carries a *negative* equity prior (two replicated failures) being tested
   under a *different* mechanism claim, and should not lower the bar relative
   to the other candidates just because the crypto story sounds more
   compelling on paper — the whole point of pre-registration is that the
   compelling-sounding story does not get a pass it hasn't earned in data.

4. **`cross_sectional`** (lower priority, run with the underpowering caveat
   attached from the start) — worth running for completeness and because the
   inconclusive equity result (killed by resource exhaustion, not a confirmed
   fail) leaves an open question this bench should eventually close on crypto
   too, but any result should be read through the 10-symbol-universe lens
   from §4 above, not treated as comparable in power to the `@sp500` version.

   **Draft hypothesis:** *Mechanism* — cross-sectional momentum (capital and
   attention chasing whichever crypto sector/narrative currently leads —
   smart-contract platforms, DeFi, payments coins, etc., the same categories
   `crypto10`'s own sector map already encodes) distinct from the time-series
   mechanisms above. *Counterparty* — crypto allocators who systematically
   underweight the current leading narrative on a valuation or discipline
   basis. *Kill criteria* — deferred to execution time, with an explicit
   additional caveat (mirroring the equity pass's own PIT-vs-today's-membership
   qualifier) that any apparent edge must be checked against the possibility
   that it is an artifact of `crypto10`'s narrow, survivorship-biased,
   hand-picked ten names rather than the rotation mechanism itself — a
   qualifier this candidate carries with more force on crypto than it did on
   the `@sp500` equity version, given `crypto10`'s caveat-4 survivorship
   severity (§2).

5. **`trend_following`** — **not recommended for this pass at all**, for the
   reason in its section above: testing it against `crypto10` alone would
   re-test `momentum`'s claim under a different strategy's code, not test this
   strategy's own multi-asset-class hypothesis. If a future session wants a
   trend_following-shaped result on crypto, the honest version of that request
   is the mixed-calendar engine work named above, scoped as its own card — not
   a `crypto10`-only run of this strategy dressed up as answering EPIC-140's
   question.

## 5. `--market crypto` threading — no auto-detection, verified against the shape guard

`DEFAULT_MARKET` in `src/trading/cli.py` (line 605) is
`DEFAULT_MARKET = US_EQUITY.name`, i.e. `"us_equity"`. Every one of `backtest`,
`paper`, `sweep`, and `gen-data` defaults to it. **A future execution of this
pass needs an explicit `--market crypto` (or `--market crypto_24_7`) on every
single command** — there is no code path that infers the market from symbol
shape, confirmed by reading `_resolve_market` (cli.py lines 699-736): it
resolves purely from the `--market` string argument (after alias normalization)
against the `CALENDARS` registry, with no reference to `--symbols` at all.

**What actually happens if you pass `--symbols @crypto10` (or any crypto-shaped
symbol) with no `--market` flag, verified by reading `_check_symbol_shapes`
(cli.py lines 739-785):** the command does **not** silently run under the wrong
market and does **not** silently detect the right one. `_check_symbol_shapes`
runs after `_resolve_market` resolves the (defaulted, `us_equity`) market, checks
`if market.calendar.is_continuous: return` — false for the default equity
calendar, so the check proceeds — and then flags every symbol whose shape
matches `_crypto_shaped` (the segment after a `/`, `-`, or `_` separator is a
known quote currency, e.g. `USD`, `USDT`; `crypto10`'s slash-form symbols like
`BTC/USD` all match this). With one or more symbols flagged, it prints an error
naming each offending symbol and exits with **`raise typer.Exit(2)`** before any
data is fetched — the error text explicitly says "Pass `--market crypto`, or
rename the symbols." So the failure mode for forgetting the flag on a
`crypto10`-shaped run is a **hard, immediate exit 2 naming the fix**, not a
silent equity-calendar misprice and not automatic detection. (This guard is
one-directional by design, per its own docstring — an equity-shaped ticker under
`--market crypto` is *not* checked, because a legitimate continuous symbol may
have no separator at all, e.g. a bare `BTC`; that asymmetry is irrelevant here
since every command this pass runs is explicitly crypto-only.) **Every command a
future execution issues should therefore include `--market crypto` explicitly —
this is not a note about robustness, it is a note that the command will simply
refuse to run without it**, which is a safety net, not something to rely on
instead of just always passing the flag.

## 6. Sequencing

Two operational constraints for whoever executes this pass, both carried
forward from lessons this bench has already paid for once:

- **No concurrent live Alpaca sessions.** EPIC-139 (the `sma_crossover`/
  `momentum` equity paper incubation recommended by the KAN-642 verdict) uses
  the same Alpaca **paper** account this crypto pass would need for
  `--source alpaca` historical fetches and, later, any crypto paper incubation
  of its own. Only one live Alpaca session (paper account) should run at a
  time. Plain historical-data fetches for backtests/sweeps are not themselves
  a live session and do not conflict with this rule, but the moment this pass
  reaches its own live paper-incubation step (playbook step 9 — explicitly out
  of scope for the research phase this document covers), that step and any
  EPIC-139 session must not run concurrently. Check `make paper-status` before
  launching anything live.
- **Resource discipline from the start, not learned mid-run again.** This
  machine hit genuine swap exhaustion (7.4/8GB, load average ~26) during
  KAN-642's own `cross_sectional` walk-forward last session, from a mix of this
  bench's own heavy commands and unrelated concurrent sessions on the same
  box (`docs/deployment-decision-2026-09-01.md`'s resource note, §5 of that
  run's log). A crypto pass has no cache (§1) and is therefore, bar for bar,
  *more* network- and CPU-time-hungry per sweep trial than the equity pass
  that already hit this wall. Every sweep/backtest step in a future execution
  should run under `nice -n 19` (and `ionice -c3` for anything heavier — a
  multi-fold walk-forward or a wide grid sweep), and should be checked against
  `free -h` and `uptime` immediately before launching, exactly as the equity
  pass's operator was forced to adopt mid-session. Doing this from the first
  command rather than discovering the need again is the whole point of writing
  it down here.

## What this document does not attempt

No command was run. No hypothesis's numeric kill criteria were finalized — every
"draft hypothesis" above states a mechanism and a counterparty (the two things
`docs/research-playbook.md` §1 says matter most) but leaves the actual numbers
("kill below OOS Sharpe X") for whoever executes the pass to set, informed by
this pass's own step-3 cheap kill test the way the equity pass's numbers were
informed by prior strategy-family experience. Filling in fabricated numbers here
would defeat the purpose of pre-registration — a number written down with no
basis is not more honest than no number, it just looks more finished than it is.
The mixed-calendar engine work needed for a real `trend_following` crypto test
is named and explicitly deferred, not designed. The "does Alpaca's daily crypto
tape have holes" question (§2) is named as the first cheap step for whoever
executes this pass, not answered by this document.
