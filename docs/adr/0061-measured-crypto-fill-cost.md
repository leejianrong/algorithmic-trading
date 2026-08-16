# ADR-0061: The crypto fill cost, measured — both terms, separately

- **Status:** accepted
- **Date:** 2026-08-16
- **Card:** KAN-710 (EPIC-87, "Crypto: a 24/7 market", final phase)
- **Builds on:** ADR-0038 (the divergence instrument), ADR-0052 (the equity result
  this sits beside), ADR-0058 (the venue, and the n=3 direction), ADR-0060 (the
  crypto cost model, and its own statement that this instrument cannot check the
  larger half of it)

## Read this first: these are simulated fills, not routed ones

Alpaca's paper account does not route a crypto order anywhere. It **simulates**
execution. Everything below therefore compares this bench's cost model against
**Alpaca's crypto fill model**, and whether that model resembles a real crypto
venue — in spread, in depth, in how a market order walks a book — is
**unestablished by anyone**, including Alpaca.

ADR-0052 could carry this as a caveat because the equity answer came back
*conservative*: if the venue is generous, we were merely too pessimistic. Crypto
does not get that luxury, because the slippage answer comes back the other way. A
model that is optimistic against a **simulator** is optimistic twice over if the
simulator is gentler than a real book. Nothing here settles that, and nothing on a
paper account can.

## Context

`CostConfig.crypto()` carries two cost terms and this bench had checked neither
against a run:

```
slippage_bps  = 5.0     # inherited from equities, never measured on crypto
taker_fee_bps = 25.0    # published and confirmed (ADR-0060), never seen in a session
```

ADR-0038 is the only instrument here that can check a cost assumption against
reality, and ADR-0060 established that it is **structurally blind to the second
term**: every statistic in that report is a ratio of fill price to reference price,
the venue's fee is taken out of the received asset, and the model deliberately
keeps the fee out of the price too (ADR-0060 §6). So a crypto divergence run can
print a clean slippage verdict with the *larger* term never examined. ADR-0060
printed `NOT MEASURED BY THIS REPORT` next to it rather than leave that implicit,
and named the route for this card: **the position delta** — `filled_qty` is gross,
so the fee is the gap between what was ordered and what arrived.

This card therefore makes **two measurements** and keeps them rigorously apart.
They are not two views of one number: one is a price and one is a quantity. They
are combined once, at the end, on purpose.

## The run

2026-08-16, `sma_crossover` over `@crypto10` at `--interval 5m`, `--market crypto
--source alpaca --broker alpaca --live --divergence --max-position 0.01`, against
the Alpaca paper account. Launched detached with `make paper-live`, stopped with
`make paper-stop`. Started 09:40:41 UTC from a flat account at $99,467.59.

`--max-position 0.01` is the sizing decision and it was made against the **fee
tier**, not for convenience. `sma_crossover` targets 95% of equity split across
whatever symbols have a bar, which on this account is ~$9,500 an order; the 1% cap
clamps every entry to about $1,000. That is 100x the venue's $10 notional floor
(ADR-0058 §4), so these are real orders rather than the $4.66 fill generator the
equity runbook rejects — and small enough that the session's whole turnover could
not move the account into the next volume tier mid-measurement, which would have
made the fee number a blend of two rates. Every entry therefore reports a `CLAMP`;
that is the guardrail doing what it was told.

It ran to 10:31:13 UTC, ending on a SIGTERM that `make paper-stop` did not send —
an external stop, which is precisely the case ADR-0043 exists for. It finalized in
under a second and wrote all five artifacts, so the stop cost nothing. **10
completed bars processed, 12 orders tracked, 11 paired fills**, median order
notional **$995.16**, paired-fill notional $10,937.41. Zero venue refusals, zero
guardrail rejections beyond the intended clamps, zero absent symbols, no halt, and
no shadow errors. The 12th order is an ADR-0036 parked order, reported as
`pending` on both sides rather than dropped.

**11 is below `MIN_PAIRED_FILLS = 30`, and the report says so itself:**

```
VERDICT: 11 paired fill(s) is below the 30 this report needs before quoting an
average as evidence (ADR-0029's spirit). The 5.00 bps model is neither confirmed
nor refuted; these rows are observations.
```

That verdict is the honest headline for measurement 1. Everything quoted below is
an observation, not a level.

### The shape of the sample matters more than its size

This matters more than the count, and it is the thing a bare mean would hide.
Grouping the 11 paired fills by the bar that produced them:

| submitted bar | fills | what it is |
|---|---|---|
| 09:40 | **8** | the opening entry burst — every symbol whose fast SMA was already above its slow one, entered simultaneously |
| 09:45 | 1 | `SOL/USD` sell |
| 09:50 | 1 | `UNI/USD` sell |
| 10:10 | 1 | `LINK/USD` sell |

So **8 of 11 rows share a single market instant**. They are not eight independent
draws of "how does this venue fill an order"; they are close to *one* draw of "how
did this venue fill a simultaneous eight-pair basket at 09:45", plus three later
ones. Whatever was true of the tape in that one five-minute window — a spread
regime, a burst of volatility, an artefact of how Alpaca's simulator prices eight
concurrent orders — enters the mean eight times over.

The standard error quoted below assumes independence and is therefore
**understated**, by an unknown factor. The effective sample size is nearer 4 than
11. A strategy that enters on a transition and exits on the opposite one will
always produce this shape at the start of a session, because ADR-0042's warmup
primes history without trading it, so every symbol already in signal enters on the
first live bar at once. Naming it is the fix available here; a session long enough
for the burst to be a minority of the sample is the real one.

### The tier this account was actually charged at

The published schedule is tiered on trailing 30-day **crypto** notional, and the
account object exposes no volume or tier field (ADR-0060's closing note), so
`scripts/crypto_fee_reconcile.py` reconstructs it from the account's own closed
orders:

```
Trailing 30d notional  $148,352.84   (104 filled crypto orders)
  -> published tier 2 ($100,000-$500,000): maker 12 bps, TAKER 22 bps
```

Measured on the day, not inherited: ADR-0060 recorded $100,636.53 across 53 orders,
and this card's own trading pushed it to $148,352.84 across 104. Both sit in
**tier 2**, so the run was charged **22 bps** throughout and no tier boundary was
crossed mid-measurement. That was the point of capping order size.

Every order this bench emits is a market order, so every fill is **taker**
(ADR-0060 §3); the maker column is unreachable and unmeasured.

**This is not the tier the model ships.** `CRYPTO_TAKER_FEE_BPS` is tier 1's 25 bps
because a fresh account starts there and a constant cannot track a moving tier
(ADR-0060 §4). The run was deliberately left on that default: the fee moves no
figure in the divergence report, so overriding it would have changed only the
caveat line — and that line is more useful stating the *modelled* number. The gap
is carried into the combined arithmetic below, where it belongs.

## Measurement 2 first — the fee, because it is the stronger half

The fee is invisible to ADR-0038's report and it is the **better measured** of the
two, because it is exact arithmetic on observed quantities rather than a mean over
a noisy sample. `trading.fees` recovers it two independent ways, and this run
exercised both.

**The buy side, from the coin.** A BUY credits coin and the fee is taken from that
credit, so a symbol's closing position falls short of `bought - sold` by exactly
the fee. Every pair round-tripped to zero, so the shortfall is the whole fee:

| pair | gross bought | gross sold | closing | implied |
|---|---|---|---|---|
| `AVAX/USD` | 156.836994 | 156.491952613 | 0 | **22.0000** |
| `BCH/USD` | 4.880647 | 4.869909576 | 0 | **22.0000** |
| `DOGE/USD` | 14271.225860 | 14239.829163108 | 0 | **22.0000** |
| `ETH/USD` | 0.529694 | 0.528528673 | 0 | **22.0000** |
| `LINK/USD` | 105.556344 | 105.324120043 | 0 | **22.0000** |
| `LTC/USD` | 22.430396 | 22.381049128 | 0 | **22.0000** |
| `SOL/USD` | 13.225314 | 13.196218309 | 0 | **22.0000** |
| `UNI/USD` | 303.949855 | 303.281165319 | 0 | **22.0000** |
| **pooled** | | | | **22.0000** |

**The sell side, from the cash.** A SELL credits fiat and is docked there instead,
so cash falls short of the realized notionals by exactly the fee — whatever the
prices did in between, because the notionals are the realized ones:

```
buy notional   $7,964.53      sell notional  $7,929.84
cash           $99,467.59 -> $99,415.39      missing $17.5095
implied                                       22.0806 bps
```

Three things follow, and the first is the one ADR-0060 asked this card to settle.

**1. The fee is not per-pair, and this is now eight pairs rather than two.**
ADR-0060 §5 inferred it from `ETH/USD` and `BTC/USD` agreeing to four decimals.
Here eight pairs spanning four orders of magnitude in unit price — `DOGE/USD` at
14,271 units against `ETH/USD` at 0.53 — return **22.0000 bps each**. So ADR-0058's
8.03 / 35.29 / 44.34 bps divergence spread was **entirely price slippage**, not a
fee difference. Confirmed, not merely re-asserted. The fee and the slippage are
separable, which is what makes the rest of this ADR possible.

**2. The two ledgers agree**, at 22.0000 (coin) and 22.0806 (cash). ADR-0060 §2
argued both sides come to `qty*price*f`; two different assets, two different
readings, one rate. The 0.08 bps gap is $0.06 across eight sells and is consistent
with the venue rounding each sell's fee to the cent.

**3. The venue takes nothing out of the reported fill price.** Cash debited on the
buys equalled gross notional to **$0.0003** across $7,964. That is the observation
that makes measurements 1 and 2 genuinely independent rather than two views of one
number — and it confirms ADR-0060 §6's decision not to fold the fee into the
modelled price, since the venue does not fold it into the realized one either.

**Against the published schedule: exact.** Tier 2's taker row is 0.22%. Nothing
here was fitted.

## Measurement 1 — price slippage

```
realized slippage   mean  +13.02 bps   median +14.29   stdev 10.59
                    range  -4.83 .. +26.83
modelled                   +5.00 bps
error                      +8.02 bps
better than model          2 / 11
by side             buy   +9.14 bps  (n=8, all one bar -- see above)
                    sell +23.39 bps  (n=3)
```

**The model is optimistic by ~8 bps on this venue**, the opposite direction to
equities. ADR-0052 measured 0.51 bps against the same 5.00 bps model on 60 equity
fills and called it conservative by 4.49; here the sign flips.

| | equities (ADR-0052) | crypto (this run) |
|---|---|---|
| paired fills | 60 | **11** |
| realized mean | +0.51 bps | **+13.02 bps** |
| modelled | 5.00 bps | 5.00 bps |
| error | **-4.49** (conservative) | **+8.02** (optimistic) |
| better than model | 54 / 60 | **2 / 11** |
| median order | $4,748 | $995 |

Taking the interval at face value, the standard error is 3.19 bps and an
approximate 95% interval is **+6.77 to +19.28 bps**, which excludes 5.00 — 5.00
sits 2.5 standard errors below the mean. **Do not read that as significance.** It
assumes 11 independent draws and there are closer to 4 (see "the shape of the
sample"); correcting for that alone would widen the interval enough to reach 5.00.
The direction is worth acting on as a prior; the level is not.

The buy/sell asymmetry — +9.14 against +23.39 — is real in the sample and
**uninterpretable at n=3 on the sell side**, especially as all three sells are
exits of the same opening basket. It is recorded so a larger run can check it, not
because it means anything yet.

### The reference price on this tape is stale, and that is not a small correction

Alpaca publishes a crypto bar only for an interval that **traded on Alpaca**, and
its crypto volume is its own rather than the market's. ADR-0038's reference price
is the open of the first bar the feed serves for the symbol *after* submission, so
on a pair that skips intervals that bar post-dates the fill and the drift in
between lands inside "slippage". Measured 5m bar coverage over 2026-08-15, out of a
possible 288:

| pair | bars | | pair | bars |
|---|---|---|---|---|
| LINK/USD | 289 | | LTC/USD | 210 |
| BTC/USD | 284 | | SOL/USD | 168 |
| UNI/USD | 281 | | DOGE/USD | 157 |
| AVAX/USD | 256 | | ETH/USD | **137** |
| AAVE/USD | 242 | | BCH/USD | 226 |

**Coverage does not track coin size**, which is what makes this untreatable by
intuition: `LINK/USD` is at 100.3% while `ETH/USD` is at 47.6%, and (independently
reproduced and extended) `BONK/USD` is at 95.5% while `SOL/USD` is at 58.3%. At
`--interval 1m` `ETH/USD` falls to **12.8%**. That is now **KAN-863**.

Two consequences, both load-bearing for the number above:

1. **The error bar is wider than the equity one**, and not by a knowable factor.
   ADR-0052's systematic error was an IEX-vs-consolidated print difference of
   ~0.4 bps against a 5 bps model. Here it is however far a thin pair moved over
   the reference lag, which is minutes rather than a tick.
2. **A per-pair spread on this tape is not evidence about execution until it is
   read against the per-pair lag.** ADR-0060 §5 already re-attributed ADR-0058's
   8 → 44 bps spread away from the fee; this run is where it can be attributed
   *to* something.

`fill_divergence.csv` now carries `reference_ts` and `reference_lag_seconds`, and
the divergence report prints a staleness block when the tape skipped — only then,
so a dense-tape (equity) block is byte-identical to before.

**In this particular run staleness was almost absent, and that is a finding too.**
The report's own block:

```
Reference staleness: 1 of 11 comparable fill(s) were priced
  against a bar later than the tightest gap seen (300.0s); median 300.0s, worst 600.0s.
```

10 of 11 references were exactly one 5m interval old — the dense, uncontaminated
case — and the single exception is `LTC/USD` at two intervals. So the +13.02 bps
mean is **not** an artefact of stale references in this window, and the per-pair
spread (`UNI/USD` +23.47 down to `ETH/USD` -4.83) cannot be blamed on lag either,
because those two rows carry the *same* 5-minute lag.

That is a stronger statement than the run could have made without the column, and
it is the reason the column was added before the run rather than after. It does
**not** generalize: the coverage table above says a longer session, or one at 1m,
or one weighted toward thin pairs, will not be so lucky. Observation latency was
mean 294s, max 322s — one bar interval, as expected of a polling broker.

| pair | n | mean bps | median | stdev | min | max | median ref lag |
|---|---|---|---|---|---|---|---|
| `UNI/USD` | 2 | +23.47 | +23.47 | 4.75 | +20.12 | +26.83 | 5 min |
| `LINK/USD` | 2 | +20.17 | +20.17 | 0.99 | +19.47 | +20.87 | 5 min |
| `AVAX/USD` | 1 | +14.29 | +14.29 | - | +14.29 | +14.29 | 5 min |
| `SOL/USD` | 2 | +10.12 | +10.12 | 19.44 | -3.63 | +23.87 | 5 min |
| `DOGE/USD` | 1 | +9.97 | +9.97 | - | +9.97 | +9.97 | 5 min |
| `BCH/USD` | 1 | +9.72 | +9.72 | - | +9.72 | +9.72 | 5 min |
| `LTC/USD` | 1 | +6.57 | +6.57 | - | +6.57 | +6.57 | 10 min |
| `ETH/USD` | 1 | -4.83 | -4.83 | - | -4.83 | -4.83 | 5 min |

Read that table as eight one-or-two-row observations, not as a ranking. The widest
single-pair spread is `SOL/USD`'s two rows, 27.5 bps apart from each other — larger
than the distance between most pairs' means, which is the same warning ADR-0058
recorded and the reason no pair here is called liquid or illiquid on this evidence.

## Putting the two together

Neither measurement alone is what a backtest's P&L depends on. Per fill, on this
account:

| | modelled | realized |
|---|---|---|
| price slippage | 5.00 bps | **13.02 bps** (n=11, wide) |
| venue fee | 25.00 bps (tier 1) | **22.00 bps** (tier 2, exact) |
| **total per fill** | **30.00 bps** | **35.02 bps** |

So the model under-charges by about **5 bps per fill**, or ~10 bps per round trip —
**not** the ~8 bps the slippage line alone suggests, because the fee is modelled
**3 bps too expensive** for this account and that offsets part of it.

The useful way to state it, and the one that survives the sample being small:
**the total cost model is right as long as realized slippage stays below 8 bps**,
because tier 1's 25 bps buys 3 bps of headroom over the 22 actually charged. This
run puts realized slippage above that, so the model is optimistic overall — but
only just, and the margin is inside the sample's error.

Two things this arithmetic quietly assumes and a reader should not: that the
account stays in tier 2 (it is a trailing-30-day property and will fall back to
tier 1 within a month of going quiet, at which point the fee term becomes exact
and the model becomes optimistic by the full 8), and that a fresh operator's
account — the one the constant is chosen for — pays 25, where the model is
conservative on the fee and the two errors offset differently again.

## Decision

**Record both measurements. Change no constant.**

`slippage_bps` stays at **5.0** and `CRYPTO_TAKER_FEE_BPS` stays at **25.0**.

ADR-0052 refused to re-tune slippage on **60** paired equity fills with a
systematic error of 0.4 bps. This sample is smaller, drawn from one strategy's
order flow over one afternoon, and carries a systematic error measured in minutes
of price drift rather than in ticks. Less evidence, and noisier evidence, cannot
justify more tuning. ADR-0060 refused to fit the fee and sourced it from a
published schedule instead; that reasoning is unchanged and the fee measurement
here *confirms* the schedule rather than replacing it.

If the slippage figure is to move at all, the honest next step is KAN-618's
cost-sensitivity sweep — which already has `--slippage-bps` / `--taker-fee-bps` as
its hook — showing how a conclusion moves across a range, not a single re-tuned
constant carrying more precision than this sample supports.

## What this sample cannot settle

**The level.** The naive 95% interval on realized slippage is **+6.77 to +19.28 bps**
around a mean of **+13.02**. That interval already excludes the modelled 5.00, which
is why the *direction* is reported with some confidence — but it assumes eleven
independent draws, and §"The shape of the sample" establishes that eight of them share
a single market instant. The effective sample is nearer four. **The true interval is
wider than the one printed above by an unknown factor**, and nothing here justifies
quoting 13.02 as a number rather than as a range whose lower edge happens to sit above
the model.

**Whether buys and sells differ.** Split by side, buys average **+9.14 bps** (n=8) and
sells **+23.39 bps** (n=3). That is a large gap in the direction one would expect if
crossing the spread costs more on exit, and it is also exactly what three observations
drawn from one afternoon look like when they mean nothing. ADR-0052 found the equity
sides agreeing closely (buy +0.02, sell +1.20) on 60 fills; this sample cannot say
whether crypto genuinely differs or whether three rows landed badly.

**Whether it generalises past this window.** One strategy, one 45-minute stretch, eight
pairs, one account, order sizes capped at 1% of equity. ADR-0058's n=3 and this n=11
agree on direction, which is worth something, but they were produced by the same
strategy on the same venue and are not independent evidence in the way two different
methods would be.

**Whether tape density explains any of it.** Only **1 of 11** rows carried a stale
reference (600 s against the normal 300 s), so staleness is *not* the explanation for
this particular mean — the correction is real but small here. That is not the same as
showing density does not matter: the pairs traded were the denser end of the venue's
listing, and KAN-863 exists precisely because coverage on this tape ranges from 47.6%
to 100.3% at 5m without tracking coin size. A sample drawn from thin pairs could look
very different, and this one cannot rule that in or out.

**The deepest one, and it is not a sample-size problem: whether Alpaca's crypto fill
simulation resembles a real crypto venue at all.** These are paper fills. The number
above is our cost model measured against *Alpaca's* cost model, and no amount of
additional paper data can close that gap — only routed execution can. On equities
ADR-0052 could file this as a footnote because the answer was comfortable. Here the
answer is uncomfortable, so it is the first question a reader should ask and the one
this ADR cannot answer.

One bookkeeping note: the run produced **12** divergence rows, of which **11** carry a
realized figure. The twelfth is a `BCH/USD` sell still `pending` at both the venue and
the model when the session stopped — a legitimate row under ADR-0038, and excluded from
every statistic above rather than counted as a zero.

## Consequences

**The direction is now the finding, and it is stable across two samples.** ADR-0058
measured n=3 at 8/35/44 bps; this run measures n=11 at a mean of +13.02. Both are
optimistic — the model under-charges — and both are the **opposite sign** to ADR-0052's
equity result, where 60 fills came in at 0.51 bps against the same 5.00 model and the
error was conservative. Two asset classes measured by one instrument now disagree in
sign, which answers the question KAN-710 was written to ask: **the modelling error is a
property of the venue, not of the method.** That is worth more than either number.

**A crypto backtest is still optimistic, but only on one term now.** ADR-0060 fixed the
fee, which was the larger error and was previously *absent* rather than mispriced.
Slippage remains understated by roughly 8 bps per fill, partly offset by the fee being
modelled 3 bps expensive for a tier-2 account. Read a crypto backtest's P&L as an upper
bound, and read it as a *worse* upper bound the higher its turnover.

**`slippage_bps` and `CRYPTO_TAKER_FEE_BPS` do not move**, per the Decision above. The
honest follow-up is KAN-618's cost-sensitivity sweep, which shows how a conclusion moves
across a range instead of asserting a re-tuned constant.

**A proper measurement needs a different experiment, not a longer one.** The binding
constraint was fill **rate**, not session length: `sma_crossover` emitted eight fills on
its opening bar and then roughly one per five-minute bar, because a transition-driven
long/flat strategy stops trading once its book matches its signal. Running the same
session for eight hours would have added tens of rows, not hundreds, and the opening
burst would still dominate. Reaching n≥30 of genuinely independent fills wants a
higher-turnover strategy, a shorter rebalance cadence, or several sessions started at
different times — and starting the sample **after** the entry burst has cleared.

**Two things outlive this card.** `divergence.py` gained `reference_ts` /
`reference_lag_seconds` and a staleness block, which is not crypto-specific and will
report honestly on any tape with gaps. And `scripts/crypto_fee_reconcile.py` reconstructs
the account's trailing-30-day notional and tier from its own closed orders, which the
account object does not expose — a durable operator tool, since the tier moves as you
trade and the modelled constant cannot track it.

**Raised by this work:** KAN-863, screen the crypto universe by venue tape density rather
than market cap — coverage on this venue ranges 47.6% to 100.3% at 5m and does not track
coin size, so `ETH/USD` is one of the thinnest tapes here while `LINK/USD` is complete.
