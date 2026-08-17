# ADR-0063: Cost model per liquidity tier — and a measurement at the thin end

- **Status:** accepted
- **Date:** 2026-08-18
- **Card:** KAN-861 ("Cost model per liquidity tier")
- **Builds on:** ADR-0060 (per-asset-class costs, the fee), ADR-0061 (the crypto
  slippage measurement, the same instrument), ADR-0052 (the equity mega-cap
  measurement and its refusal to re-tune), ADR-0029 (the ADV screen and its
  no-look-ahead formation window, reused here rather than re-implemented)

## Context

ADR-0060 made trading costs a property of the **market** — `CostConfig.equity()` /
`.crypto()` differ in one field, and a market with no researched cost model is
unselectable rather than silently priced free. That card's own closing note named
the corollary it did not build:

> Note the corollary — **cost is a function of liquidity, not of asset class**:
> ADR-0052's 0.51 bps was measured on twenty mega-caps and must not be
> extrapolated down the cap scale (KAN-861).

`slippage_bps` is still one flat number applied to every symbol in a run, whatever
that symbol's own liquidity. `cross_sectional` (ADR-0025) ranks and holds names
from the whole curated universe by trailing return; a universe screen by `--min-adv`
(ADR-0029) sets a floor, but everything above that floor — a mega-cap and a name
sitting just over the floor — is charged the identical 5.0 bps. ADR-0052 already
measured that this is wrong at one end (mega-caps trade far better than modelled)
and refused to fix it, on grounds worth restating: the measurement was thin (n=60,
one afternoon) and close to its own systematic error. This card supplies the other
end of the scale the corollary asked for, and then builds the mechanism.

## The measurement (read from the raw artifacts, not the ticket description)

The PM ran a live Alpaca paper `--divergence` session on 2026-08-17
(`sma_crossover`, `--interval 5m`, `--market us_equity --source alpaca --broker
alpaca --live --divergence`) against ten real, currently-tradable S&P 500
constituents chosen specifically for **low, but real, liquidity**: ranked by
measured ADV via `trading.liquidity.screen_by_adv` against real yfinance data over
a 90-day formation window ending 2026-08-16, the bottom ~10 of a historically
smaller-cap candidate list, each independently confirmed tradable and fractionable
via `trading verify-universe`.

The artifacts (`results/paper/2026-08-17T183346Z-liquidity-tier/{result.json,
fill_divergence.csv,console.log}`) were read directly for this ADR rather than
taken from the ticket's summary — the repo's own convention (KAN-631's agent did
the same the same day) is not to trust a secondhand number when the primary
artifact is on disk. `fill_divergence.csv` carries 12 rows; the twelfth (`CE`,
sell) is `pending` (parked, market shut) and is excluded from every statistic
below, exactly as ADR-0061's twelfth row was.

Symbols traded, with their measured ADV (`$/day, formation window
2026-05-18..2026-08-16`):

| Symbol | ADV ($/day) | | Symbol | ADV ($/day) |
|---|---|---|---|---|
| OGN | 35.6M | | BXP | 94.2M |
| FMC | 45.3M | | MHK | 95.0M |
| EMN | 85.5M | | WHR | 101.4M |
| FOX | 88.5M | | AOS | 103.2M |
| CE | 88.9M | | ZION | 109.3M |

Contrast: `blue20`'s mega-caps trade in the **billions** per day — three orders of
magnitude above this tier.

**11 comparable (paired) fills.** Signed realized slippage (bps, positive = worse
for us), read directly from `fill_divergence.csv`'s `realized_slippage_bps` column:

```
-9.71, +8.89, +4.91, +7.29, +9.50, -0.00, +0.48, +16.75, +8.91, +5.06, -5.58
```

```
mean    +4.23 bps
median  +5.06 bps
stdev    7.49 bps
modelled 5.00 bps
error   -0.77 bps mean (realized - modelled)
```

`console.log`'s own printed verdict, exactly as the report always renders it below
`MIN_PAIRED_FILLS = 30` (ADR-0029's spirit, ADR-0038's report):

```
VERDICT: 11 paired fill(s) is below the 30 this report needs before quoting an
average as evidence (ADR-0029's spirit). The 5.00 bps model is neither confirmed
nor refuted; these rows are observations.
```

**11 is below `MIN_PAIRED_FILLS = 30`.** Everything below is an observation, in
exactly the sense ADR-0052 and ADR-0061 used that word, not a level.

### The headline, stated carefully

At mega-cap liquidity (ADR-0052, n=60), realized slippage measured **+0.51 bps**
against the flat 5.0 bps model — conservative by ~4.5 bps. At this thin-but-real
S&P 500 tier (n=11), realized slippage measures **+4.23 bps mean / +5.06 bps
median** — close to the model, and arguably very slightly *more* pessimistic than
the model at the median. Put the two next to each other:

| | mega-cap (ADR-0052) | this tier (KAN-861) |
|---|---|---|
| paired fills | 60 | **11** |
| ADV | billions/day | $35.6M-$109.3M/day |
| realized mean | +0.51 bps | **+4.23 bps** |
| realized median | +0.59 bps | **+5.06 bps** |
| modelled | 5.00 bps | 5.00 bps |
| error (mean) | -4.49 (conservative) | **-0.77** (roughly right) |
| median order | $4,748 | ~$10,481 |

This is a real, if statistically thin, confirmation of this card's founding
premise: **cost is a function of liquidity.** The existing flat 5.0 bps model looks
approximately right — maybe very slightly conservative — for a name at this
liquidity tier, and was measurably too pessimistic for a mega-cap. Neither sample
clears this bench's own significance floor (`MIN_PAIRED_FILLS = 30`); both are
directions, not levels, exactly as ADR-0052 and ADR-0061 stated about their own
measurements. n=11 here is no more precise than n=60 was there, and n=60 already
was not precise enough to move a constant.

## Decision

### 1. A tiered slippage override, additive to `CostConfig`

`CostConfig` gains a fourth, **optional** field:

```python
symbol_slippage_bps: Mapping[str, float] | None = None
```

`None` by default, so `CostConfig()`, `CostConfig.equity()`, and
`CostConfig.crypto()` are **completely unaffected** — none of them ever populate
this field, so choosing a market does not silently enable tiering, and it is a
`ValueError` for any entry to be negative, exactly like the other three fields.

`CostModel.fill_price` gains an optional third argument:

```python
def fill_price(self, side: Side, reference: float, symbol: str | None = None) -> float:
```

When `symbol` is omitted, or the config carries no map, or the symbol is not a key
in the map, the method falls back to the flat `slippage_bps` — the exact
computation it has always done. `SimulatedBroker._execute` is the only caller and
now passes `order.symbol` through. This is why the mechanism can be added without
touching either of the two existing dataclasses' presets, and why a `CostConfig`
built the old way is priced identically to before, to the bit.

### 2. The classification reuses the ADV screen's own machinery, doesn't duplicate it

`trading.liquidity` gains two small functions rather than a parallel
implementation:

- `classify_liquidity_tier(adapter, symbols, backtest_start, ...)` — for each
  symbol, fetches its `formation_window` bars (the **same** function
  `screen_by_adv` calls) and computes `average_dollar_volume` (the same function
  too), returning `dict[symbol, adv | None]`. It never drops a symbol — that is
  the whole difference from `screen_by_adv`, and the reason it is a genuinely
  separate function rather than a wrapper: the ADV screen and the cost tier are
  independent decisions, and a run may use either, both, or neither.
- `liquidity_tier_rates(advs, tier_adv_floor, tier_slippage_bps)` — turns those
  ADVs into the actual `dict[symbol, float]` override, keeping only symbols at or
  above the floor. A symbol below the floor, or with no formation-window data at
  all, is **omitted** rather than given an explicit entry at the default rate:
  "absent from the map" and "priced at the market's default rate" are the same
  fact, by construction of `fill_price`'s fallback.

Reusing `formation_window` means the identical no-look-ahead guarantee applies
without a second implementation to keep in sync: nothing here can read a bar the
backtest will trade on (ADR-0001, ADR-0029), pinned by the same style of
requested-range assertion `test_liquidity.py` already uses for `screen_by_adv`.

### 3. Exactly two tiers, and where the breakpoint sits

`config.py` names the tier's rate; `liquidity.py` names the tier's floor:

```python
LIQUID_TIER_SLIPPAGE_BPS = 2.0  # config.py
DEFAULT_TIER_ADV_FLOOR = 1_000_000_000.0  # liquidity.py, $1B/day
```

**The floor sits in the gap between the two measured samples, not at either
sample's edge.** The thin tier measured here tops out at ZION's $109.3M/day; the
mega-cap tier (`blue20`) is described as trading in the billions. $1B/day is a
full order of magnitude above the thinnest-tier ceiling and squarely inside "the
billions" description of the liquid tier — so a symbol between ~$110M and $1B,
which nothing here measured, stays on the unchanged 5.0 bps default rather than
being assumed comparable to a mega-cap. That is the conservative placement: an
unmeasured gap defaults to the more pessimistic rate, not the more optimistic one.

**The rate is 2.0 bps, not 0.51 and not left at 5.0.** Three things pinned this
number rather than a re-tune to the point estimate:

1. **Not 0.51, because that would repeat the mistake ADR-0052 explicitly refused
   to make** — treating a thin, paper-fill, one-afternoon measurement as a level
   rather than a direction. 60 paired fills did not clear this bench's own
   significance floor there; 11 does not clear it here either, and the *thin*
   tier's own measurement (this card) landed close to the *unmoved* 5.0 bps
   default, which is itself evidence that this bench's flat-rate history has been
   roughly right for a name this size — not evidence for aggressively repricing
   the *other* end down to a single afternoon's mega-cap mean.
2. **The margin of conservatism is comparable to the one the flat model already
   carried.** The existing, unmoved 5.0 bps default sits `5.0 / 0.51 ≈ 9.8x` above
   ADR-0052's own point estimate. `2.0 / 0.51 ≈ 3.9x` is a smaller margin — the
   whole point is to move the mega-cap tier down from a number now measured to be
   too pessimistic — but it is still several times the measured mean, and
   comfortably outside the ~0.4 bps IEX-vs-consolidated reference-price noise
   floor ADR-0052 named as the limit of what that measurement could resolve.
3. **The default (below-floor) tier is left at 5.0 exactly, unmoved.** This card's
   own measurement is the evidence *for* leaving it alone: a genuinely thin S&P
   500 name measured close to the flat model, not below it.

Both the rate and the floor are ordinary CLI defaults, not registry entries a
market selection resolves through — this is deliberately a simpler seam than
ADR-0060's `_MARKET_COSTS`, because there is exactly one tier boundary being
added, not a second market whose absence must be refused. Both are overridable per
run.

### 4. Opt-in at the CLI, off by default

`backtest` gains two options, mirroring `--min-adv`'s shape and sharing its
formation-window flag (`--adv-window`) rather than adding a third:

```
--liquidity-tier-adv FLOAT        # ADV floor; None (off) by default
--liquidity-tier-slippage-bps FLOAT  # rate at/above the floor; defaults to LIQUID_TIER_SLIPPAGE_BPS
```

`--liquidity-tier-adv` defaults to `None`, meaning **off**: a run without the flag
computes nothing extra and prices every symbol at the flat rate exactly as before
— pinned by a CLI test that runs the same backtest with and without a
never-clearable floor and gets byte-identical `total_return`. When set, the
backtest's already-resolved `tickers` (after any `--min-adv` screen) are
classified via `classify_liquidity_tier` + `liquidity_tier_rates`, and the
resulting map is folded into the run's `CostConfig` (built from `_build_costs`,
which already handles `--market`/`--slippage-bps`/`--taker-fee-bps` precedence) as
`symbol_slippage_bps`. A one-line-per-symbol report prints before the run, the
same convention `_apply_liquidity_screen` already established, so a tiered run's
cost basis is visible on stdout rather than a number baked silently into
`result.json`.

`paper` and `sweep` do **not** get this flag in this card. `backtest` is where the
measurement above and the corollary in CLAUDE.md are stated (a cross-sectional
strategy over a wide universe), and adding it to the other two entry points is a
mechanical follow-up rather than a new decision — left for whoever needs it next.

## Consequences

### Equity is byte-identical without the flag

`CostConfig()`/`.equity()`/`.crypto()` never populate `symbol_slippage_bps`, and
`fill_price`'s new third argument defaults to `None` and only changes behavior when
both a symbol *and* a matching map entry are present. Every existing call site
(`SimulatedBroker._execute`, the only production caller) either now passes
`order.symbol` into a model whose map is `None` (no-op) or is unaffected. A CLI
test asserts a backtest with a floor no symbol can ever clear reproduces the
untouched baseline exactly.

### What this does and does not settle

Like ADR-0052 and ADR-0061 before it, this is a **direction**, not a **level**.
Eleven paired fills at one liquidity tier, on one afternoon, on one venue's
simulated paper fills, is not enough to certify 2.0 bps (or any other number) as
the "right" mega-cap rate — it is enough to say that a flat rate is the wrong
shape, which is the mechanism this card builds, and that the unmoved 5.0 bps
default is not obviously wrong for a name this thin, which is why it stays. The
honest next step for the *rate itself*, exactly as ADR-0052/0060/0061 named for
their own constants, is KAN-618's cost-sensitivity sweep — showing how a
conclusion moves across a range of tier rates, not a single re-tuned constant
carrying more precision than an n=11 (or n=60) sample supports.

### Known gaps

- **Only two tiers.** A single floor and a single override rate is the minimal
  shape that expresses "liquidity is not asset class"; a richer tiering (more
  breakpoints, a continuous function of ADV) is a bigger change and unmotivated by
  evidence this thin — two tiers is already more than either measurement alone
  can justify precisely.
- **`paper` and `sweep` have no `--liquidity-tier-adv`.** Only `backtest` is
  wired. A cross-sectional paper session over a wide universe still prices every
  symbol flat.
- **`result.json` does not record the tier map or its parameters.** A reader
  cannot tell from the artifact alone whether a run used tiered costs, only that
  `metrics` differ from an untiered run of the same command. Additive when
  someone wants it (the same gap ADR-0060 recorded for its own cost model).
- **The gap between ~$110M and $1B/day is unmeasured.** The floor was placed
  conservatively in that gap rather than at either sample's edge, but nothing
  here says where within that gap liquidity actually starts to matter.
- **These are still Alpaca paper fills, simulated rather than routed** — the
  same limitation ADR-0052 and ADR-0061 both carried. Whether Alpaca's paper
  simulation of a thin-but-real S&P 500 name resembles how a real venue would
  fill it is unestablished; this measures our model against Alpaca's model, at
  this tier, same as the other two cards.
