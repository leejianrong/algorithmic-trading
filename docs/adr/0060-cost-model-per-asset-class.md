# ADR-0060: Trading costs belong to the market — and the instrument that validates them cannot see the crypto one

- Status: Accepted
- Date: 2026-08-14
- Deciders: strategy developer (project owner)
- Card: KAN-707 (EPIC-87, "Crypto: a 24/7 market", phase 3)

## Context

`CostConfig` has modelled exactly one venue since V1:

```python
commission_per_share: float = 0.0
slippage_bps: float = 5.0
```

That is a commission-free US-equity broker, and it was the right model for the only
market this bench could trade. EPIC-87 changed the market and not the costs, so
since ADR-0058 landed an hour before this card, `trading backtest --market crypto`
has been pricing a venue that charges roughly 25 bps as **free**.

The card states the shape problem precisely:

> Crypto carries explicit taker fees on top of a wider spread, and on Alpaca those
> are percentage-based rather than per-share, so the commission model does not even
> have the right shape.

It is worth being exact about why "free" rather than "mispriced". `commission_per_share`
cannot express a fraction of notional at *any* setting, so there was no wrong number
to pick — the term simply had nowhere to go, and the default 0.0 meant every crypto
backtest published a return with the venue's largest cost missing.

ADR-0058 §5 had already measured the fee and deliberately left it alone:

> The fee is recorded and **not** corrected: nothing in the order carries it, and
> inventing a 25 bps constant from one afternoon is exactly the re-tuning ADR-0052
> refused to do with slippage.

That restraint was right for a card that had one afternoon of observation. This card
is where the number gets **sourced** instead of invented, which is a different act.

## What was confirmed, and by which method

Two independent derivations, kept separate on purpose. Throughout: **published**
means Alpaca's fee schedule, **measured** means KAN-708's observations against the
live paper account on 2026-08-14, and **reasoned** means neither.

### 1. The published schedule (read 2026-08-14)

Source: https://docs.alpaca.markets/us/docs/crypto-fees, page stamped
"Updated September 24, 2025". Maker/taker, tiered by trailing **30-day crypto**
volume (equities volume explicitly excluded):

| Tier | 30-day volume (USD) | Maker | Taker |
|---|---|---|---|
| **1** | **$0–$100K** | **0.15%** | **0.25%** |
| 2 | $100K–$500K | 0.12% | 0.22% |
| 3 | $500K–$1M | 0.10% | 0.20% |
| 4 | $1M–$10M | 0.08% | 0.18% |
| 5 | $10M–$25M | 0.05% | 0.15% |
| 6 | $25M–$50M | 0.02% | 0.13% |
| 7 | $50M–$100M | 0.02% | 0.12% |
| 8 | $100M+ | 0.00% | 0.10% |

The page also states the mechanism, and it matches what KAN-708 saw: *"The crypto fee
will be charged on the credited crypto asset/fiat (what you receive) per trade."*
And: *"Fees are currently calculated and posted end of day."*

### 2. The reconciliation — this is the heart of the card

The card demanded a sourced number rather than a remembered one. Two fresh
measurements were taken for this card (2026-08-16, live paper account, ~$12 notional
each, account flattened after each), and together with KAN-708's they say something
stronger than "the number is right":

| # | Date | Pair | Gross `filled_qty` | Credited | Ratio | Implied fee |
|---|---|---|---|---|---|---|
| a | 08-14 (KAN-708) | BTC/USD | `0.000617391` (4 buys) | `0.000615847` | 0.99749936 | 25.0006 bps |
| b | 08-14 (KAN-708) | BTC/USD | `0.00016` | `0.0001596` | 0.99750000 | 25.0000 bps |
| **c** | **08-16 (this card)** | **ETH/USD** | `0.00638666` | `0.006372609` | **0.99779995** | **22.0005 bps** |
| **d** | **08-16 (this card)** | **BTC/USD** | `0.000190444` | `0.000190025` | **0.99779988** | **22.0012 bps** |

Rows (a)/(b) reproduce the published **tier-1** taker rate of 0.25%. Rows (c)/(d)
reproduce the published **tier-2** taker rate of **0.22%** — and they are the answer
to two questions the card asked explicitly.

**The venue does simulate volume tiering, and this is now observed rather than
unknown.** ADR-0058 could not say. The rate did not drift: it moved from exactly one
published row to exactly the next one. Reconstructing the account's trailing 30-day
crypto notional from its own closed orders gives **$100,636.53** across 53 filled
crypto orders — i.e. the account crossed the published $100,000 tier-1/tier-2
boundary, and it did so on **KAN-708's own live session**, which turned over roughly
$50k of SOL and ETH in half an hour. The schedule's boundary, the account's volume,
and the charged rate all agree.

**And the fee is not per-pair.** ETH/USD and BTC/USD measured on the same day return
22.0005 and 22.0012 bps — the same rate to four decimal places on two pairs of very
different liquidity. ADR-0058's wide spread between its ETH and SOL divergence rows
was therefore *price slippage*, not a fee difference, exactly as the single-column
published schedule implies.

So the reconciliation is much better than a single point: **Alpaca's paper venue
implements the published maker/taker schedule faithfully, confirmed at two tiers, on
two pairs, across two dates.** `CRYPTO_TAKER_FEE_BPS = 25.0` is *sourced* from that
schedule, and four independent observations landing on two of its rows is what
distinguishes this from the fitting ADR-0052 refused.

A test pins the reconciliation rather than only the constant, so the day the
published schedule and the measured ratio part company, that is a red test and not a
silent drift.

### 3. Taker only, and that is a fact about this bench

**Observed, from our own code**: every order this bench emits is a market order —
`sizing.py` produces nothing else and ADR-0004 fills at the next open. A market order
crosses the spread by definition, so it is always the taker. No maker/taker switch is
built, because it would be a knob with exactly one reachable setting; if a limit-order
strategy is ever added, the maker column becomes reachable and that card can add it.

### 4. Tier 1 is the default, and the bench's own account has already left it

**Observed** (§2): tiering is real on the paper venue, and this account is currently
in **tier 2** paying 22 bps, because KAN-708's live session pushed its trailing 30-day
crypto notional to $100,636.53.

The constant nevertheless stays at **tier 1 / 25 bps**, for three reasons:

1. **A fresh account starts in tier 1.** That is the honest default for anyone who
   runs this bench, and the trailing-30-day window means this account will fall back
   into tier 1 within a month of going quiet. A constant cannot track a moving tier;
   pretending otherwise would be worse than picking the entry row.
2. **Tier 1 is the most expensive taker row**, so the model is conservative for any
   account that has traded its way down — the safe direction to be wrong, and the
   same bias ADR-0052 applied to slippage.
3. **The tier is a property of the operator's account, not of the market**, so it does
   not belong in a per-market posture at all. `--taker-fee-bps 22` is the escape
   hatch, and it exists precisely for this.

That third point is worth stating plainly, because it is the one thing this card
models slightly wrong on purpose: `CostConfig.crypto()` is "what this venue charges a
new account", not "what your account will be charged".

### 5. The fee is not per-pair — now observed, not reasoned

ETH/USD and BTC/USD measured minutes apart return **22.0005** and **22.0012** bps
(§2), agreeing to four decimals on two pairs of very different depth. The published
schedule has no per-pair column and the measurement agrees.

This also **re-attributes** ADR-0058's puzzle. Its three divergence rows spread from
8.03 bps (ETH) to 44.34 bps (SOL), and it flagged that the SOL rows were "a far less
liquid pair". That spread is **price slippage**, not fee: the fee is flat across
pairs, so everything in that dispersion belongs to the slippage term KAN-710 has to
measure. Useful, because it means the fee and the spread are separable rather than
confounded.

## Decision

### 1. A third term, added rather than the existing one restructured

```python
commission_per_share: float = 0.0
slippage_bps: float = 5.0
taker_fee_bps: float = 0.0  # new
```

The three are **three different physical quantities**, not three spellings of one:
slippage is a statement about the *price*, `commission_per_share` is dollars per unit
and independent of price, and `taker_fee_bps` is a fraction of notional and
independent of quantity. There is no fixed conversion between the last two, because
the conversion *is* the price — which is exactly why the old field could not express
the fee.

So restructuring was rejected: a venue may legitimately charge both, and folding them
together would mean re-deriving one from a price at every call site. `CostModel.commission`
gains a `price` argument and becomes
`qty * commission_per_share + qty * price * taker_fee_bps / 10_000`. Its one caller is
`SimulatedBroker._execute`, which already has the executed price.

With `taker_fee_bps = 0.0` the second term vanishes identically, which is why the
equity path is arithmetically unchanged rather than merely close.

### 2. The fee is charged in cash, on both sides, and that is an approximation with a stated cost

Alpaca charges on the credited asset: a SELL credits fiat and is docked
`qty*price*f` in **cash**, a BUY credits coin and is docked `qty*f` in **coin**, worth
`qty*price*f` at that same fill price. Both sides are therefore `qty*price*f`, and
charging them equally is exact rather than a simplification.

Charging the BUY side in **cash** rather than in kind is the one real approximation.
Paying `qty*price*(1+f)` cash for `qty` coin is not identical to paying `qty*price`
for `qty*(1-f)` coin; the two differ by `f²` of notional, which at 25 bps is
**0.000625 bps** — five orders of magnitude below the slippage term.

What it buys is worth far more than that error. `Portfolio.apply_fill` stays the
single accounting path for both markets (ADR-0002), and `Fill.qty` keeps meaning
"quantity ordered *is* quantity received" — the assumption every downstream consumer
makes, and whose violation at the venue is precisely the ADR-0058 §7 `SHARE_PRECISION`
oversell that left a live session unable to exit.

**The real cost of the choice is funding, not precision, and it was measured.** A cash
fee needs cash the sizer never reserved, so a fully-invested buy can be rejected where
it previously fit — the ADR-0037 benchmark-flatness class. Across 50 synthetic crypto
seeds, `buy_and_hold` fully invested (guardrails off, so the 25% position cap cannot
mask it):

| Cost model | Runs left flat | Runs with a delayed entry | Worst delay | Total entry rejections |
|---|---|---|---|---|
| fee 0 bps | **0 / 50** | 19 / 50 | 4 bars | 27 |
| fee 25 bps | **0 / 50** | 28 / 50 | 7 bars | 52 |

So the fee roughly **doubles** the entry rejections and delays half again as many
entries — and **nothing is left flat**, because ADR-0037's retry-until-the-position-
exists already handles exactly this. The guard built for that failure catches this one.
Under default guardrails the 25% position cap leaves so much headroom that it never
binds at all. A unit test pins the mechanism directly (an order that fits without the
fee is rejected with it, recorded and never raised) so this cannot regress quietly.

### 3. Two named postures, differing in one field — ADR-0055's shape

`CostConfig.equity()` returns exactly `CostConfig()`. `CostConfig.crypto()` returns
`CostConfig(taker_fee_bps=25.0)` and differs from equity in **exactly one field**,
asserted by diffing the two dataclasses.

**`slippage_bps` deliberately stays at 5.0**, and the restraint is the point. ADR-0052
refused to re-tune it on **60** paired equity fills measuring 0.51 bps, on three
grounds — the measurement was the same order as the reference error, paper fills are
simulated rather than routed, and one afternoon is not a level. The crypto evidence is
**three** paired fills (ADR-0058), an eighth of `MIN_PAIRED_FILLS = 30`. Less evidence
cannot justify more tuning. So the only number that moves is the one that is published
*and* independently confirmed.

`CostConfig.crypto(taker_fee_bps=0.0)` is a **`ValueError`**, mirroring
`RiskConfig.crypto(halt_cooldown_bars=None)`: a 24/7 posture that models a
fee-charging venue as free is the flattering number the preset exists to prevent, and
zero is not reachable from the published schedule anyway — the cheapest taker row is
tier 8's 0.10%, and only a *maker* ever pays 0.00%. A higher tier remains expressible.

There is no `if crypto:` anywhere on the execution path. A posture is a value.

### 4. `--market` selects costs as its fourth seam

`_MARKET_COSTS` sits beside `_MARKET_POSTURES`, keyed by calendar name, and
`_resolve_market` refuses a calendar missing from it rather than falling back — the
same rule `get_calendar` (ADR-0054) and the posture table (ADR-0057) already apply.
It is the sharper case of that rule, because the equity default is *commission-free*:
a market missing from this table would not be merely mispriced, it would be modelled
as free, which is the most flattering wrong answer available.

New `--slippage-bps` / `--taker-fee-bps` on `backtest`, `paper` and `sweep`, both
defaulting to `None`, resolved by `_build_costs` under **ADR-0057's precedence,
unchanged: an explicitly-passed flag always wins, and every term left unset comes from
the selected market's cost model.** On `us_equity` they resolve to the old literals.
They also give KAN-618's cost-sensitivity sweep the hook it needs.

One asymmetry, deliberate: `--taker-fee-bps 0` is *not* refused even though the preset
refuses it. The preset's job is to stop a crypto run being modelled as free by
**default**; a flag the operator typed is a typed choice that appears in the shell
history, exactly the line ADR-0057 drew when it let `--max-drawdown 0.9` override the
posture's 0.20.

### 5. The benchmark pays the same costs

`_run_benchmark` now takes the market's cost model. It stays *unconstrained* in the
guardrail sense (ADR-0037's point is that the comparison must not be clamped), but a
benchmark exempt from the venue's fees is a different thing entirely: on a 25 bps
venue it would beat the strategy by the fees the strategy paid and it did not, and
ADR-0039's paired bootstrap reads that curve. Buy-and-hold pays the fee roughly once,
which is precisely what makes it the right baseline for a turnover cost.

### 6. The fee is **not** folded into the fill price

This is the subtlest decision in the card and it goes the opposite way to the obvious
one.

Rolling the fee into the executed price would make it visible to ADR-0038's divergence
statistic, which is a ratio of fill price to reference price. That is exactly why it
must not be done: the venue's realized fee is taken out of the received asset and is
genuinely **not** in the price it reports. A model that priced it in would show a
permanent ~25 bps gap against every real fill, and invite a future reader to "correct"
a cost model that was right. Visibility bought by fabricating a divergence is worse
than no visibility.

## Consequences

### The headline: the instrument that validates a cost model cannot see its largest crypto term

ADR-0038 is the only thing this bench owns that can check a cost assumption against
reality, and ADR-0058 spotted the problem in one sentence:

> it compares prices, and the fee is taken in quantity.

That is now a property of the shipped model and it must travel with every crypto
divergence number. Every statistic in the report derives from `_adverse_bps`, a price
ratio, so a fee charged on notional and taken out of the received asset moves **none**
of them — not the realized side (the venue reports the same execution price whether it
charges 0 or 25 bps) and not the modelled side (item 6 above). A crypto divergence run
can therefore print a perfectly clean slippage verdict while the largest term in the
cost model has never been examined.

So `DivergenceSummary` gains `modelled_taker_fee_bps` and the report prints, only when
there is a fee:

```
Venue fee:         25.00 bps of notional — NOT MEASURED BY THIS REPORT. The figures
                   below compare fill prices, and this fee is taken out of the
                   received asset, so it moves neither the realized nor the modelled
                   number (ADR-0060, KAN-710).
```

Stating it is not fixing it. **KAN-710 inherits this limitation**, and the honest
route for that card is the position delta — `filled_qty` is gross, so the fee is
recoverable by comparing the quantity ordered against the quantity the account was
actually credited. That is deliberately not built here.

### Alpaca paper fills are SIMULATED, not routed — and this model is now calibrated against one

ADR-0052 recorded this as a footnote and ADR-0058 promoted it to a headline. It
belongs in the headline of *this* ADR too, because it is a property of the model
shipped here rather than of a measurement:

The 25 bps is sourced from a published schedule and confirmed against a **paper**
account. Whether Alpaca's crypto fill *simulation* resembles a real crypto venue —
in spread, in depth, in how a market order actually walks the book — is unestablished,
and ADR-0058's three rows (8, 35, 44 bps realized against 5.00 modelled) point the
**unsafe** direction, opposite to equities. The fee term is well-sourced; the slippage
term on this venue is, on present evidence, probably too small. This ADR fixes the
first and explicitly declines to guess at the second.

### The equity path does not move, proved by hash

All seven baseline artifacts are byte-identical to `43a7d0c`:

| Artifact | SHA-256 |
|---|---|
| daily backtest `equity_curve.csv` | `220e0bb88f1c95afbf2d62b6686a39e8909761fa581fbb704d8b8e1193443e1f` |
| daily backtest `result.json` | `01786310330c8c5d8925cacd4fc6f6040a67ad05a0732772559732ece5c8d699` |
| 5m backtest `equity_curve.csv` | `4ba021e1a007d532f6e14738893c86c0ce49ba761d60fbbd6987be4866c99226` |
| 5m backtest `result.json` | `c72a884d03433c5626c969027798179a3226ea587c506e4547e1f664d8f6d07b` |
| `paper --once` `equity_curve.csv` | `9608600b1cccab04122d35fe3217834ef01cb3c901fdc95f6e708aa0904419f4` |
| `paper --once` `result.json` | `624187176414635e5d11809a91d6ec723cc490c9a0894676955db81b1491b289` |
| `paper --once` `paper_state.json` | `daa33064f415c1fda0608d28780c474f45a22e5199acef2c00be18505e6e32b6` |

`RESULT_SCHEMA_VERSION` stays **1**: nothing was added to `result.json` at all. The
cost model is not recorded there, which is a gap named below rather than a claim of
completeness.

### Three of ADR-0057's own tests went red, correctly

`TestTheCalendarSeam` asserts that `--market crypto` over an identical CSV changes
only the annualized figures. That premise is now false by design — `--market` also
selects costs — so the crypto arm paid 25 bps, filled at different net prices, and
produced a different equity curve and a different beta.

The fix is to hold the new variable constant: both helpers now pass `--taker-fee-bps 0`
on **both** arms, restoring the isolation the class exists for. It is the *calendar*
seam under test there, and the cost seam has its own tests. This is the second time a
`--market` lane has collided with those tests (ADR-0057 records the first), which is
itself evidence the flag is doing real work at several seams.

### Recorded, not fixed

- **`result.json` does not record the cost model.** A reader cannot tell from the
  artifact whether a crypto run was priced with the fee. The `market` key (ADR-0057)
  implies it today, but only because there is exactly one cost model per market — the
  moment `--taker-fee-bps` is used that inference breaks. Additive when someone wants it.
- **The dashboard does not render costs**, for the same reason it does not yet render
  `market` (ADR-0057).
- **`AlpacaBroker` still reports `Fill.commission = 0.0` on crypto.** That is correct
  for the *account* (it reconciles from Alpaca, ADR-0020) but it means the blotter
  still overstates the received quantity by the fee, exactly as ADR-0058 §5 recorded.
  Modelling and observing are different jobs; this card did the first.
- **The modelled tier will drift out of date.** The venue charges by trailing 30-day
  volume and this account is already one tier below the constant (§4). Nothing warns
  about it, nothing queries the account's tier, and `--taker-fee-bps` is the only
  correction. Querying the live tier is not possible through the seam as it stands —
  the account object exposes `accrued_fees` but no volume or tier field, so the
  $100,636.53 figure above had to be reconstructed from closed orders.
- **Maker fees are unreachable and unmodelled**, because the bench emits only market
  orders (§3). A limit-order strategy would need the maker column and a way to know
  which side of the book it landed on.
- **The fee is not applied to `--source csv` crypto data by any automatic means** —
  it comes from `--market`, so a CSV of crypto bars run under `--market crypto` is
  priced correctly and the same CSV under the default market is not. That is the
  ADR-0057 shape guard's job, and it already refuses slash-separated symbols on a
  session market.

### Still open

- **KAN-710** — the crypto divergence measurement, which this card unblocks and whose
  central difficulty (the fee is invisible to the price-based instrument) is now
  documented and printed rather than discovered.
- **KAN-618** — the cost-sensitivity sweep, which now has `--slippage-bps` /
  `--taker-fee-bps` as its hook.
- `sizing.SHARE_PRECISION = 6` (ADR-0058 §8) and `risk.py`'s `_TRADING_DAYS = 252`
  remain equity-shaped and untouched by this card.
