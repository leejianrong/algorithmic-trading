# ADR-0058: Alpaca's crypto venue is a second venue behind the same seam — and it disagrees with itself about symbols

- Status: Accepted
- Date: 2026-08-14
- Deciders: strategy developer (project owner)
- Card: KAN-708 (EPIC-87, "Crypto: a 24/7 market", phase 3)

## Context

EPIC-87's phases 1 and 2 landed five ADRs — 0053 through 0057 — that between them
give this bench a 24/7 calendar, a continuous completeness rule, a crypto risk
posture, a continuous synthetic generator, and one `--market` flag that selects all
of them. Every one of those decisions closed with the same disclaimer, and EPIC-87's
own status note in `CLAUDE.md` says it plainly:

> every claim about a live crypto venue in ADR-0053 through ADR-0057 is **arithmetic
> and generated data, never observed** — no crypto credentials exist and no crypto
> network call has been made.

This card is the first one that can replace some of that with observation. It also
carries the epic's one remaining functional hole: `--source csv` was the only route
to real crypto bars, so nothing could actually *trade* a continuous market.

The card predicted where the difficulty would be:

> The seams should mostly hold. `AlpacaClient` already abstracts the SDK with our own
> DTOs (ADR-0017/0018), `AlpacaBroker` reconciles from the account (ADR-0020), and the
> duplicate-order guard is keyed on symbol and side (ADR-0036) rather than anything
> equity-specific. Expect the surprises in asset metadata and precision, not in the
> loop.

That prediction was **right about the loop and wrong about where the surprises are**.
The loop needed nothing at all. The surprises were not in metadata or precision
either — they were in the two places where Alpaca's crypto venue is not a parameter
on the equity venue but a *different service that disagrees with it*.

Everything below was measured against the real Alpaca paper account on 2026-08-14
unless it says otherwise.

## What was measured

### Premises this epic had only reasoned about, now observed

| Premise | Source | Verdict |
|---|---|---|
| A 24/7 daily bar closes at UTC midnight | ADR-0053 §convention, ADR-0056 §anchor | **Confirmed.** Eight consecutive `BTC/USD` daily bars, `2026-08-07T00:00:00Z` … `2026-08-14T00:00:00Z`, every stamp exactly midnight; 30-day sweep, all midnight. |
| A continuous market has no weekend gap | ADR-0053, ADR-0056 | **Confirmed.** 957 daily bars since 2024-01-01, **272 weekend bars**, **zero** non-1-day gaps. |
| The provider serves a still-forming daily bar | ADR-0053's whole reason to exist | **Confirmed.** At 09:08 UTC the `2026-08-14T00:00:00Z` bar was already returned. The equity session rule would call it complete the instant the UTC date turned over; `ts + interval` withholds it. ADR-0053 is now load-bearing against a real feed. |
| Alpaca's crypto symbol format | ADR-0057's shape guard rests on this guess | **Confirmed and complete.** All 73 crypto assets are slash-separated, no exceptions. The four quote currencies in use — `USD`, `USDC`, `USDT`, `BTC` — are **all already in** `_CRYPTO_QUOTE_CODES`. ADR-0057's rule needs no widening. |
| An absurdly early start | ADR-0047 measured 0 bars on equities | **A third behaviour, and worse.** See below. |
| Crypto-like volatility makes the ADR-0013 latch permanent | ADR-0055, measured on GBM | **Confirmed on real data**, see §"the risk posture, observed". |
| GBM understates crypto's tails | ADR-0055's own honesty caveat | **Confirmed.** ADR-0055's GBM worst single-bar portfolio loss was 9.29%; real crypto gave **15.82%**, with 6 days worse than −10% in 943. |

### 1. An absurd start returns *one* bar — quieter than the zero ADR-0047 fixed

`BTC/USD`, daily:

| Start | Bars | First |
|---|---|---|
| `datetime.min` | **1** | today's forming bar |
| `1900-01-01` | 2052 | 2021-01-01 |
| `1990-01-01` (our synthetic `EPOCH`) | 2052 | 2021-01-01 |
| `now − 5d` | 5 | — |

ADR-0047 found the equity endpoint answering `datetime.min` with **0** bars, which at
least tripped ADR-0035's per-symbol absence and ADR-0047's universe-wide ERROR. One
bar trips **neither**. The poll looks successful, every symbol looks present, the
empty-poll counter resets, and a live session primes a single bar and reports itself
healthy while the strategy starves for its lookback. **This is a strictly quieter
failure than the one that blocked the Monday run.**

ADR-0047's bounded window already prevents it — nothing new is needed — but it was
prevented by luck of sequencing, and it is now asserted rather than assumed.

**ADR-0040's lesson, fifth sighting.** `SyntheticAdapter` clips an absurd start to its
1990 epoch (ADR-0030, ADR-0056 both document the clipping as deliberate) and
`FakeAdapter` filters any range. Both are *more forgiving than this provider*, so a
regression test written against either passes whether or not the bug exists. This is
pinned directly: `TestStandInsCannotTestTheVenue` asserts both stand-ins' forgiveness,
so the file cannot be quietly rewritten onto them.

Also newly known: `BTC/USD` data **inception is 2021-01-01**, a real inception date
where `SyntheticAdapter` has none, and the tape has genuine holes — `SOL/USD` returns
1,634 bars across a 2,052-day span.

### 2. Alpaca disagrees with itself about how a crypto symbol is spelled

One round trip, one asset, three answers:

- `submit_order(symbol="BTC/USD", …)` → accepted; the order echoes `symbol='BTC/USD'`.
- `get_order_by_id(...)` → `symbol='BTC/USD'`.
- the position that fill created → **`symbol='BTCUSD'`**.

And separately: `TradingClient.get_asset("BTCUSD")` succeeds and *normalizes* to
`BTC/USD`, while `CryptoBarsRequest(symbol_or_symbols="BTCUSD")` is refused —
`invalid symbol: BTCUSD does not match ^[A-Z]+x?/[A-Z]+$`. So the concatenated form is
a trading-API alias and the slash form is the venue's canonical one.

**This is the defect with the worst blast radius in the card.** `AlpacaBroker._reconcile`
keys its `Portfolio` on whatever `list_positions` reports, and the engine, the sizer
and the guardrails all key on the symbol the *bars* carry. Left concatenated, a held
position is invisible to every one of them: gross exposure reads zero, the
target-weight sizer sees a permanently unmet target, and the run buys the same coin
every bar until the cash runs out. It is silent — the same shape ADR-0036 fixed for
parked orders, arriving through a different door.

### 3. `TimeInForce.DAY` is refused for crypto

```
422 {"code":42210000,"message":"invalid crypto time_in_force"}
```

`RealAlpacaClient.submit_order` hard-coded `TimeInForce.DAY`, so **every** crypto order
would have failed. And it would have failed *tidily*: 422 with an Alpaca error code is
a textbook ADR-0041 refusal, so each one would have become a legible `(Order, reason)`
rejection carrying the venue's own words, reached `result.json`, and printed in the
summary — while the session traded nothing at all. Loud, correct, and useless.

### 4. The binding order floor is a $10 notional the metadata does not carry

The card asked for a minimum-order-size check. The measurement says that would be the
wrong thing to build. For `BTC/USD` at ~$62,800:

| Order | Notional | Result |
|---|---|---|
| `0.000155` | $9.73 | refused, `403`/`40310000` `cost basis must be >= minimal amount of order 10` |
| `0.00016` | $10.05 | accepted |

Published `min_order_size` for the same asset is `1.5739e-05` — about **$0.99**, an
order of magnitude below the floor the venue actually enforces. A client-side gate
built on the published number would wave through orders the venue then refuses: a
false negative dressed as a safety check, and a second copy of a venue rule to keep in
sync. Across all 73 assets `min_order_size` ranges from `1.5739e-05` to `444444.44`,
so it is real metadata — it is just not the constraint.

### 5. Alpaca's paper crypto venue charges ~25 bps, taken in the received asset

Four BUYs totalling `0.000617391` BTC produced a position of `0.000615847` — ratio
`0.99749936`. An independent `0.00016` BUY added `0.0001596` — ratio `0.99750000`.
`filled_qty` is reported **gross** in both cases, so nothing in the order carries the
fee.

`AlpacaBroker._to_fill` sets `commission=0.0` with the comment "Alpaca paper
commission is zero". That is true of equities and **false of crypto**.

### 6. `cancel_order` is not idempotent on a *filled* order

```
422 {"code":42210000,"message":"order is already in \"filled\" state"}
```

ADR-0036 established the seam's contract — "cancelling an order that is already
terminal succeeds silently — verified against the live paper venue, which answers a
repeat cancel with 200, not an error." That was measured **with the market shut**, so
the only terminal state reachable was `canceled`. Nothing had ever cancelled a *filled*
order, because until crypto this bench could not fill one on demand.

This is almost certainly not crypto-specific. Crypto is simply the first venue where
it could be observed.

### 7. A live crypto session could not sell what it held

Found on the **last bar of a real `--live` session**, not by reasoning:

```
REJECT SELL ETH/USD (Alpaca refused sell 13.339 ETH/USD (HTTP 403, code 40310000):
  insufficient balance for ETH (requested: 13.338989, available: 13.33898895))
```

`sizing.SHARE_PRECISION = 6`, and `sizing.size` emits
`round(desired - current, SHARE_PRECISION)`. For a full exit that is
`round(0 - 13.33898895, 6) = -13.338989` — **more than the account holds**.

The equity path never sees it because Alpaca quantizes fractional shares at six
decimals or fewer, so `current` has no seventh digit and the rounding is exact.
Crypto publishes `min_trade_increment = 1e-9`, so a reconciled quantity routinely
carries nine decimals and rounding to six rounds *up* — roughly half of all exits.
`SimulatedBroker` has no balance constraint, so a backtest cannot see it either.

**This is a domain-invariant break, not a cosmetic refusal.** This bench is
long-or-flat (ADR-0011), a SELL is the only exit there is, and ADR-0013/0031 and
ADR-0036 each go out of their way to keep exits unblocked. A position that cannot
be sold is the worst thing this card found.

### 8. Asset flags mean the same thing, and they do have work to do

Across all 73 crypto assets, exactly **one** fails ADR-0028's test, and it fails on the
flag you would not guess: **`SHIB/USDT` is `tradable=True` but `fractionable=False`**
(`min_order_size` 86,880 SHIB). So `validate_universe`'s usable / unusable / unverified
sort is meaningful here and needed no change. `get_asset` works on crypto symbols in
both spellings.

### 9. Two smaller measurements

- **Crypto market data needs no credentials at all.** A bare
  `CryptoHistoricalDataClient()` returned bars byte-identical to a keyed one, and
  `CryptoBarsRequest` has no `feed` field. **ADR-0034's free-plan SIP restriction has
  no crypto analogue**, and `--data-feed` is meaningless here. Recency is fine: at
  09:08:54 UTC the newest 5m bar was stamped 09:05:00, a lag of 3m54s.
- **Quantity precision is the venue's problem, not ours.** `0.00021739130434782607` —
  what the target-weight sizer actually emits — was accepted and silently truncated to
  `0.000217391`, the nine decimals `min_trade_increment` publishes. No rounding is
  needed on this side, and none is added.
- **There is no `adjustment` concept.** `CryptoBarsRequest` has no such field, and the
  corporate-actions endpoint answers a crypto symbol cleanly with **no data keys at
  all** — not an error, and not a refusal.

## Decision

### 1. The asset class is a client **construction** property, and `--market` picks it

`RealAlpacaClient(asset_class="us_equity" | "crypto")`. One client serves one venue,
exactly as `feed` means one client serves one tape (ADR-0034) and the interval means
one adapter serves one cadence (ADR-0022). `require_asset_class` **raises** on an
unknown value rather than defaulting to equity — `get_calendar`'s rule (ADR-0054) one
layer down, and for a concrete reason: a venue that silently became the equity one
would send every crypto order to the stock tape, which answers `invalid symbol:
BTC/USD`.

Three things it does not change: the trading endpoint (one `TradingClient` serves
both), the credentials, and the client's public surface.

`AlpacaAdapter` and `AlpacaBroker` each take a **`MarketCalendar`**, not an asset
class, and derive the venue from `calendar.is_continuous`.

**This is ADR-0056's argument reused, and it is the load-bearing design choice here.**
A separate `--asset-class` or `AlpacaAdapter(asset_class=…)` flag would keep "crypto
bars annualized on a 252-day year" representable one keyword away — ADR-0054's exact
defect, which this epic was sequenced to remove. Deriving the venue from the same
value that already sets `periods_per_year` (ADR-0054), the completeness rule
(ADR-0053) and the risk posture (ADR-0055) *removes the combination* rather than
documenting it. `cli.py` needed no new flag: `_make_adapter` already receives the
`Frequency`, which already carries its calendar.

### 2. Position symbols are canonicalized from the venue's own asset listing

`list_positions` rewrites a crypto position's concatenated symbol to the venue's
canonical slash form, using a map built from `get_all_assets(asset_class=crypto)` —
lazily, cached for the client's lifetime, one request per session paid the first time
a crypto position is read.

**The venue's own listing, not a suffix rule.** A longest-quote-suffix rule over the
four live quote currencies *does* reproduce the map exactly — checked on all 73 pairs,
zero mismatches, zero collisions, and no concatenated crypto symbol collides with any
of Alpaca's 33,183 equity tickers. It is free and needs no request. It was still
rejected as production code: it is a rule of ours to maintain against a venue that has
already been caught disagreeing with itself twice in this ADR, and `USDCUSD` /
`BTCUSDT` are exactly the shapes that split two ways by eye. The agreement is pinned as
a **nightly contract test** instead, so the day it stops holding we learn it from CI
rather than from a position reconciled under the wrong key (ADR-0035's reuse rule: one
mechanism, not two).

A symbol absent from the map that the venue *calls crypto* **raises**. Reconciling a
crypto position under a key nothing else uses is worse than stopping — ADR-0028's bias
toward propagating, and the failure this whole item exists to prevent. A non-crypto
position on the same account passes through untouched; it is not ours to rewrite.

### 3. Crypto orders are GTC; `IOC` was considered and refused

`_TIME_IN_FORCE` maps the asset class to the duration the venue accepts: `day` for
equities (unchanged, and asserted so it cannot drift), `gtc` for crypto.

`IOC` is the alternative and is deliberately not used: it would cancel an unfilled
remainder immediately, turning every partial fill (ADR-0033) into a permanent one and
quietly changing what a fill *means* between markets. `GTC` is the closest analogue of
the equity `DAY` order.

**The cost is stated rather than hidden:** a crypto order that does not fill never
expires. On equities an unfilled DAY order expires at the close, which is the routine
end of every order ADR-0036's parked-order branch handles. Here there is no close, so
a session that ends with a working crypto order leaves it working indefinitely.
ADR-0052 already learned that a session ends holding its book; on this venue that is
now also true of its orders.

### 4. No client-side minimum-order-size gate. `min_order_size` is recorded, not enforced

`AssetInfo` gains `min_order_size: float | None` — `None` rather than `0.0` when the
venue publishes none, because `0.0` would read as "no minimum" instead of "it did not
say". It is reported and available to `verify-universe`; nothing gates on it.

The reason is §4 above: the published number is an order of magnitude below the binding
floor, so a gate on it would be a false negative. What already handles this correctly
is ADR-0041's classifier — an under-floor order is refused, classified, and recorded as
`(Order, reason)` carrying the venue's own sentence (`cost basis must be >= minimal
amount of order 10`), which reaches `rejections`, `result.json` and the summary.
Nothing vanishes silently, which was the actual requirement.

### 5. Crypto has one price notion, and that is a fact about the asset rather than a flag being ignored

`CryptoBarsRequest` has no `adjustment`, so `adjusted=True` and `adjusted=False` return
the same bars. This is **not** the flag being swallowed: a crypto pair has no splits
and no dividends, so the raw series *is* the total-return series and ADR-0008 and
ADR-0021 are asking for the same thing. A nightly test asserts the two agree, so if
Alpaca ever adds an `adjustment` and they diverge, we hear about it rather than
silently marking a raw account on adjusted prices.

ADR-0045's split guard is therefore **skipped on crypto, not merely inert**:
`get_splits` is never called. That matters because the guard's failure mode is a
warning per symbol per window, and a warning about a cross-check that could never
apply is noise that trains an operator to ignore warnings. `get_splits` returns `[]`
for a crypto client without paying a request — which is a claim we can make honestly
because the endpoint *was* asked and answered with empty data.

**The honest limit, recorded and not solved:** a token redenomination is a real
rescaling, Alpaca publishes no corporate-actions record for crypto that would carry
it, and nothing in this bench would notice. That is a genuine ADR-0008 hole on this
venue with no available fix.

### 6. `cancel_order` becomes idempotent by re-reading the order, not by matching text

On a cancel failure the client re-reads the order; if it is already terminal, the
caller has what they asked for and the failure is absorbed. If the re-read says it is
still working — or the re-read itself fails — the original failure propagates.

Discriminating on **state**, not on the message, because Alpaca answers both "invalid
crypto time_in_force" and "order is already in filled state" with the same `42210000`,
so the error taxonomy ADR-0041 relies on cannot separate them here. The extra request
is paid only on the failure path. This matters to a real operator: the runbook tells
you to flatten a session's book by hand (ADR-0052), and doing that over a list of
order ids hits a filled one immediately.

### 7. `crypto10` is a curated basket with the worst survivorship bias in the repo

Ten USD-quoted pairs, in the venue's canonical slash form, all verified `tradable` and
`fractionable` on 2026-08-14. Stablecoins and `PAXG/USD` are excluded: a pegged asset
has no trend or relative strength to rank, so it would sit in a cross-sectional basket
contributing only turnover.

`universe.py` gains an honesty caveat 4 saying plainly that this is an upper bound on
an upper bound, through three compounding filters: the tokens are 2026's survivors of a
market with a far higher failure rate than equities; **Alpaca's 73-asset listing is
itself a survivor filter** that `blue20` does not have, since a stock exchange lists the
losers until they die; and the tape starts at 2021-01-01 and serves only currently
listed pairs, so there is no way to put the dead names back even by hand.

### 8. A dust oversell is trimmed in the broker; the root cause is named, not fixed

`AlpacaBroker.submit` trims a SELL down to the reconciled holding **only** when the
excess is at most half a unit at `SHARE_PRECISION` — i.e. exactly the rounding
artifact and nothing else. Narrow in three ways, because silently rewriting an order
is the kind of helpfulness this bench refuses: SELL only (a BUY has no holding to
exceed); dust only (an exit for twice the position is an upstream bug that must reach
the venue and be refused, ADR-0041); and only against a position that exists (selling
what you do not hold is ADR-0011's implicit short, and the venue is the right thing to
say so).

Logged, not recorded as a rejection or a clamp. The exit *happens*, at the only
quantity that could have worked, so calling it a guardrail action would misdescribe it:
the ADR-0009 clamps are policy decisions about how much to hold, this is arithmetic
about how much exists.

**The root cause is `sizing.SHARE_PRECISION = 6` — a US-equity fractional-share
convention applied to every market — and it is deliberately not fixed here.** The
honest repair is for the sizer to round *toward zero*, or to the venue's own
`min_trade_increment`. That lives in `sizing.py`, is shared by the backtest path, and
would move every equity figure this repo has published, so it belongs to a card that
owns that file and can re-baseline the goldens. This is the symptom-level defence, the
same choice ADR-0036 made over KAN-678 — defence in depth, not alternatives.

One related artifact left alone: the same rounding drops sub-`1e-6` deltas as dust
(`sizing._DUST`), so a crypto exit can leave a residue — the live session ended with
`0.000000397 SOL`. At any plausible price that is worth a fraction of a cent and is far
below the venue's $10 floor, so it can never be sold; it is inert, and naming it is
better than adding a special case for it.

### 9. ADR-0034's IEX default is equity-only

`cli.py` set `--data-feed iex` for every live Alpaca run. On crypto that is a feed the
request has no field for, so `paper --market crypto --broker alpaca --live` died at
client construction. The refusal is the guard working — loud, before any network call —
but the default had no business being set. It is now conditioned on the market not
being continuous.

## Consequences

### The seam was right, and that is the headline

**The venue split cost the `AlpacaClient` protocol no new method.** `cancel_order`
(ADR-0036) and `get_splits` (ADR-0045) were each a widening the seam paid for; crypto
rides the nine calls that already exist. `AlpacaBroker` gained **no asset-class-aware
logic at all** — the poll loop, the terminal-status set, the duplicate guard's
`(symbol, side)` key and the reconcile-from-the-account rule were all written without a
market in mind and all held against a real crypto fill. Everything that had to change
lives one layer down in the client, where the venue's own inconsistencies are. Both
claims are pinned by tests rather than asserted here.

### The risk posture, observed on real crypto for the first time

`cross_sectional` over `@crypto10`, 2024-01-01 → 2026-08-01, real Alpaca daily bars,
944 bars: **7 halt episodes, all 7 re-armed, 0 still in force at the end**, max
drawdown 68.5%, 69 rejections, 20 clamps. ADR-0055 designed exactly this behaviour
from GBM at 80% annualized volatility and said in terms that a GBM series is not
crypto. It now has a real observation: the bounded halt fires repeatedly on real data
and recovers every time, which is the shape ADR-0055 argued for and the latch would
have turned into a permanent stop in July 2024.

Two caveats that must travel with that number. It is a **survivorship-biased basket**
(§7), and 68.5% drawdown on a −28.98% run is not a recommendation of the strategy —
it is evidence about the *guardrail*.

### The live session: what a real crypto paper run looks like

`sma_crossover` over `BTC/USD, ETH/USD, SOL/USD` at 5m, `--broker alpaca --live
--divergence`, 2026-08-14 09:35–10:05 UTC. Short by design — the point was to execute
the path, not to measure anything.

Every EPIC-87 guard held on a continuous market:

- **ADR-0042/0047** — `Warmup: primed 565 completed bar(s) 2026-08-12 10:35..2026-08-14
  09:35 as history; no orders submitted for them`. The bounded window works on a tape
  with no weekends.
- **ADR-0009** — both opening orders clamped at the 25% position cap.
- **ADR-0020/0058** — `exposure: 0.0% → 50.0% → 25.1%`. That progression *is* the proof
  the symbol canonicalization works: under the concatenated key exposure would have
  stayed at 0.0% and the run would have kept buying.
- **ADR-0055's posture** was in force (`halt re-arms after 30 bar(s)`); nothing halted
  in half an hour, as expected.

And two things only a live run could show: the **~25 bps fee, visible end to end** — a
`BUY 331.8018 SOL/USD` was followed by a `SELL 330.9723`, the sizer exiting the
*post-fee* holding the account actually credited — and the **unsellable exit** of §7,
on the final bar.

**ADR-0043's SIGTERM finalize was checked on this path separately**, and the first
attempt is worth recording because it was a false alarm that looked like a regression.
Stopping the session with `timeout`, which signals the `uv run` **wrapper**, left only
`paper_session.log`, `paper_state.json` and `fill_divergence.csv` — no `equity_curve.csv`,
no `result.json`. That is the wrapper-signalling fragility `CLAUDE.md` already documents
for `nohup`, not a product defect: repeating it with SIGTERM to the **python child**
(`--broker simulated`, real crypto data, no orders) finalized cleanly, wrote all five
artifacts, and printed its summary. The operator path (`make paper-stop`) signals the
child, so it is unaffected — but a crypto runbook should say so, because a 24/7 session
has no close to end it and *will* be stopped by hand.

### Alpaca paper fills are SIMULATED, not routed — and on crypto that now matters much more

ADR-0052 recorded this as a comfortable footnote: measured equity slippage of 0.51 bps
against a 5.00 bps model, i.e. our cost model against *Alpaca's* fill model, and the
gap was in the safe direction.

**On crypto it belongs in the headline of any measurement, not a footnote.** The venue
charges ~25 bps in fees alone (§5), the bench's model is 5 bps of slippage and zero
commission, and `Fill.commission` is `0.0` while `filled_qty` is gross — so the first
crypto divergence report will compare a 5 bps model against a venue charging roughly
five times that, **in the unsafe direction**, and the honest first question will be
whether Alpaca's crypto fill simulation resembles a real crypto venue at all. Nobody
knows. KAN-710 owns that measurement; this ADR owns making sure it starts from the
right question.

The fee is recorded and **not** corrected: nothing in the order carries it, and
inventing a 25 bps constant from one afternoon is exactly the re-tuning ADR-0052
refused to do with slippage.

**The first crypto divergence rows exist, and they point the wrong way.** The live
session produced three paired fills:

| Fill | Realized | Modelled | Error |
|---|---|---|---|
| ETH/USD buy | 8.03 bps | 5.00 | +3.03 |
| SOL/USD buy | 35.29 bps | 5.00 | +30.29 |
| SOL/USD sell | 44.34 bps | 5.00 | +39.34 |

On equities ADR-0052 measured 0.51 bps against a 5.00 bps model — the model was
**conservative**. Here it is **optimistic**, by between 3 and 39 bps of price slippage
*before* the ~25 bps fee the divergence report cannot see at all (it compares prices,
and the fee is taken in quantity). Three things must travel with that, in this order:
**n = 3**, an eighth of `MIN_PAIRED_FILLS = 30`, so it is a direction and not a
measurement; the two SOL rows are a far less liquid pair than the ETH one, and the
spread between them is larger than either's distance from the model; and these are
**paper fills**, so the comparison is our cost model against Alpaca's crypto fill
*simulation*, whose resemblance to a real crypto venue nobody has established. KAN-710
owns the actual measurement.

### Recorded, not fixed

- **`Bar.volume` is an `int`, and crypto volume is fractional.** The venue served
  `BTC/USD` daily volumes of `1.205143732` and `0.147082239`; `int()` makes those `1`
  and **`0`**, and a zero-volume bar is a lie about a day that did trade. Blast radius
  is exactly one caller — `volume` is read only by `liquidity.py`'s opt-in ADV screen
  (ADR-0029) — and that screen is unusable on this tape for a larger, separate reason
  anyway: Alpaca's crypto venue volume is its own, not the global market's, so BTC/USD
  averages tens of thousands of dollars a day against an equity-calibrated $20M floor.
  Widening `Bar.volume` to a float touches `types.py` and every adapter. Pinned as a
  characterization test so it cannot be forgotten.
- **`fetch_span` and `MIN_LIVE_EMPTY_POLLS` are still equity-shaped.** ADR-0053
  assessed `fetch_span`; this card confirms it against a real continuous venue and
  changes nothing. Over-asking is the safe direction, so a continuous lookback cannot
  be truncated. `MIN_LIVE_EMPTY_POLLS = 4` is more interesting on a market that never
  closes — its floor was calibrated against equity weekends that do not exist here —
  but it did not bite in this card's live session and is left alone.
- **`risk.py`'s `_TRADING_DAYS = 252`** remains reachable via `--market crypto
  --target-vol`, where it would allow a vol-targeted book ~20.4% more gross than asked
  for. Unchanged from ADR-0057's note; this card did not use `--target-vol`.
- **`halt_cooldown_bars` is still a count, not a duration.** 30 bars is 30 days on the
  crypto daily calendar and 2.5 hours at 5m. The crypto backtest above used daily bars,
  where 30 is what ADR-0055 intended.
- **The token-redenomination hole** in §5.

### Still open

- No crypto `--interval` guard: the venue serves 1m/5m/1h/1d and all work, but nothing
  checks a *primed* history is long enough for the strategy's lookback (KAN-702,
  unchanged).
- `gen-data`, `dashboard` and `verify-universe` still have no `--market`.
  `verify-universe` nonetheless works on `@crypto10` unchanged, because `get_asset`
  goes through the asset-class-agnostic trading client — measured, not assumed.
- No crypto divergence measurement (KAN-710 owns it; this card makes it possible).
