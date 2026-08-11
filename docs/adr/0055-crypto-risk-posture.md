# ADR-0055: The risk defaults are an equity posture; a 24/7 market gets a bounded halt, not wider numbers

- Status: Accepted
- Date: 2026-08-11
- Deciders: strategy developer (project owner)
- Card: KAN-709 (EPIC-87, "Crypto: a 24/7 market")

## Context

`RiskConfig` defaults `max_position_pct = 0.25`, `max_gross_exposure = 1.0`,
`max_drawdown_pct = 0.20` (ADR-0009 chose the levels, ADR-0013 the semantics). Those
are mega-cap US equity numbers at roughly 20% annualized volatility. A large-cap
crypto runs about four times that, and the card's claim was that the defaults "would
trip constantly" there — with the kill switch latching for the whole run unless
recovery is configured (ADR-0013 §3 as amended by ADR-0031), which ADR-0031 already
measured as fatal to a long run.

Two things needed checking before deciding anything.

**First, the ADR-0031 numbers the card quotes.** Verified against the source, not
repeated from the brief: ADR-0031's Context section records the halt firing for all
six strategies on real yfinance 2000-2020 data, typically in 2001, and
`cross_sectional` returning **-3.91%** latched against **+1727%** neutralized, with
**1853** rejected orders against 142. The card quotes that correctly. The code backs
the mechanism it describes: `Guardrails._rearm_due` returns `False` immediately unless
`RiskConfig.halt_recovery_enabled`, and both recovery fields default to `None`, so the
shipped default really is a permanent latch.

**Second, whether the claim is true at crypto volatility, measured rather than
asserted.** `SyntheticAdapter` takes `SyntheticParams(annual_vol=…)`, so the same
engine, the same `Guardrails`, the same strategy can be driven at 20% and at 80% with
drift held at the default 0.08 — volatility as the only changed variable.

### What the equity defaults do at 80% annualized volatility

`sma_crossover`, 5 synthetic symbols, 2015-2025 daily (2,610 bars), default
`RiskConfig()`, 20 seeds:

| | equity vol 20% | crypto-like vol 80% |
|---|---|---|
| seeds where the latch tripped | **0 / 20** | **20 / 20** |
| median bar of the first halt | — | **250** of 2,610 (min 86, max 920) |
| median share of the run spent halted | 0% | **90.5%** |
| median total return | +63.02% | **+8.95%** |
| median total return, drawdown halt neutralized | +63.02% | **+561.93%** |
| rejections / avg exposure (seed 0) | 2 / 52.9% | **327 / 3.8%** |

So the card is right, and the failure is ADR-0031's exactly — one halt inside the
first year, then nine years of refusing entries — except that at four times the
volatility it is unanimous across seeds rather than merely likely. Every strategy in
the registry shows it (seed 0, latched → cooldown-30): `sma_crossover` +11.72% →
+578.94%, `momentum` +27.11% → +545.22%, `mean_reversion` +64.39% → +188.84%,
`cross_sectional` +162.04% → +642.33%, `equal_weight` +589.98% → +4376.89%.

**The caps are not the failing part.** Across the same runs the position and gross
caps clamped **1 to 13** orders in 2,610 bars. What breaks is the latch.

### Why widening the level is not the answer, with the number

The obvious move is to scale `max_drawdown_pct` with volatility. Measured on the
per-bar drawdown-from-running-peak of 50 series per volatility (5 symbols × 10 seeds,
2015-2025 daily), pooled to 130,500 bars each side:

| drawdown from running peak | share of equity-vol bars at or beyond | share of crypto-vol bars |
|---|---|---|
| ≥ 20% | 30.19% | **84.25%** |
| ≥ 35% | 10.04% | 72.82% |
| ≥ 50% | 0.99% | 59.61% |
| ≥ 70% | 0.00% | 40.09% |

`0.20` sits at the **69.81st percentile** of the equity pool. The crypto drawdown at
that *same tail rank* is **77.9%** — past even a 2022-style crypto drawdown. A
threshold there does not fire, and measured directly, `max_drawdown_pct = 0.50` with
recovery produced **zero halts** on the series that broke the default. That is the
card's warning made concrete: scaling the level to preserve its rarity turns the
guardrail off. The rarity is not the property worth preserving.

## Decision

**Crypto does not get its own numbers. It gets a halt that cannot be permanent.**

`RiskConfig` gains two named postures beside the existing `unlimited()`:

| | `RiskConfig.equity()` | `RiskConfig.crypto()` |
|---|---|---|
| `max_position_pct` | 0.25 | 0.25 |
| `max_gross_exposure` | 1.0 | 1.0 |
| `max_drawdown_pct` | 0.20 | 0.20 |
| `max_daily_loss_pct` | `None` | `None` |
| `halt_recovery_drawdown_pct` | `None` | `None` |
| `halt_cooldown_bars` | `None` | **30** |

1. **`equity()` returns exactly `RiskConfig()`** — the field defaults do not move, and
   a test pins the equality. It exists so choosing a market is a choice, rather than
   the equity assumption being what happens when nobody chooses. Every existing run,
   result and test is untouched; two CLI invocations covering the guardrail-clamp path
   and the paper path are byte-identical (hashes in the PR).

2. **`crypto()` differs in one field.** `test_crypto_posture_widens_nothing` diffs the
   two configs and asserts the difference is exactly `{"halt_cooldown_bars"}`, so
   widening a cap here turns a test red. There is no crypto number to justify because
   nothing was widened.

3. **Halt recovery stops being optional.** That is the answer to the card's question.
   `crypto(halt_cooldown_bars=None)` raises `ValueError` rather than returning a
   config that latches — a 24/7 posture whose kill switch is permanent is the thing
   this preset exists to prevent, so it is a refusal, not a value. (The parameter's
   type admits `None` only so the refusal is expressible; an untyped caller — a future
   CLI flag, a config file — can hand one over.)

4. **A 20% drawdown becomes an event that recurs, and that is the point.** Under the
   posture the switch fires 7-8 times per ten years, each episode bounded, holding a
   median **8.6%** of the run — a circuit breaker, versus a kill switch that fires
   once and ends the run at 90.5%. It still refuses entries, it still costs return
   (see below), and it is still reachable. The guardrail is intact; only its duration
   changed.

5. **The cooldown's floor is arithmetic; only the rounding is taste.** A cooldown
   shorter than `(max_drawdown_pct / per-bar sigma)²` bars re-arms before the market
   has moved a threshold's worth of dispersion — i.e. inside the same move that
   tripped the switch. At 80% annualized volatility on daily bars, per-bar sigma is
   5.04% and the floor is **15.8 → 16 bars**. `CRYPTO_HALT_COOLDOWN_BARS = 30` is the
   next legible unit above it (one month of a market that never closes) and gives
   27.6% of dispersion against a 20% threshold. The floor is asserted in code
   (`test_cooldown_clears_the_dispersion_floor`), so shortening the cooldown until the
   halt stops costing anything turns red. Note the same arithmetic at equity
   volatility gives 6.9% against the same threshold: **30 is a crypto number, not a
   universal one.**

   The measured alternatives, published here so the choice is inspectable rather than
   tuned (medians over 20 seeds):

   | cooldown | halts | share of run halted | total return | rejections |
   |---|---|---|---|---|
   | none (latch) | 1.0 | 90.5% | +8.95% | 322 |
   | 5 (below the floor) | 9.0 | 1.7% | +537.18% | 12 |
   | 16 (the floor) | 8.0 | 4.9% | +509.76% | 23 |
   | **30 (chosen)** | 7.5 | 8.6% | +539.22% | 39 |
   | 60 | 7.0 | 16.1% | +480.06% | 66 |
   | 180 | 6.0 | 40.0% | +189.14% | 140 |

   Return falls monotonically past 30, so a return-maximizing reader would pick 5 —
   which is below the floor and therefore refused. **The criterion picked the number,
   not the return column.**

6. **`halt_recovery_drawdown_pct` stays `None`, also on evidence.** Alone at this
   volatility it re-armed *nothing*: 1 halt, never resumed, +11.72% — the permanent
   latch in disguise, because a halted long-or-flat book drains to cash and freezes
   its drawdown above the threshold. That is ADR-0031 §2's measured AND-deadlock
   biting under OR, and it has its own regression test. The cooldown is the liveness
   guarantee; a recovery threshold is an early re-arm a caller may add, and adding one
   to the preset would be a number with no evidence behind it that changes nothing.

7. **Exits stay allowed while halted** (ADR-0013 §2, ADR-0031 §4, and the same
   asymmetry ADR-0036 keys on). Unchanged and tested twice under the new posture: at
   unit level a SELL of a held position passes while a BUY is refused, and end to end
   every rejection in a crypto-posture run is asserted to be a BUY, so no episode ever
   trapped the book.

8. **`risk.py` gains no executable line.** The posture is a `RiskConfig`; nothing in
   the guardrails knows which market it is guarding, so there is no `if crypto:` and no
   second code path (ADR-0002's spirit). The `risk.py` diff is documentation.

9. **No CLI flag.** The surface that *selects* a posture is a later integration PR; the
   library seam ships now. Until then a crypto posture is reachable only from Python,
   which is stated plainly under Consequences.

## What set the level, and what would falsify it

Stated separately because this is the part the card demanded and the part that is
weakest.

**What set it:** the measurement above, i.e. a deterministic GBM series at 80%
annualized volatility driven through the real engine, plus the arithmetic floor under
the cooldown. That evidence is sufficient to establish (a) the latch is what breaks,
unanimously across seeds and across all six strategies, (b) the caps are not what
breaks, and (c) that raising the threshold to preserve its rarity switches the
guardrail off. It is sufficient because those are all *structural* claims about the
mechanism, not claims about a level.

**What it explicitly does not establish: the right level for real crypto.** A synthetic
GBM series at crypto-like volatility **is not crypto.** It has no fat tails, no
regime breaks, no funding-driven liquidation cascades, and no 2022-style 75%
drawdown — its worst single bar across 26,090 portfolio returns lost 9.29%, where real
crypto has single days past 20%. The measurement shows the *shape* of the failure. It
cannot calibrate a number, and this ADR does not claim it has: the only level that
moved is a cooldown whose floor is arithmetic and which is the same in either
direction.

**What would falsify it:**

- A real crypto series (the `--source csv` path is the existing hook) where the
  posture's 7-8 bounded episodes per decade turn into per-cooldown churn, or where the
  latch is *not* the dominant term. Either would say the level, not the duration, is
  where the problem lives.
- A fat-tailed sample where a single bar takes the book past the drawdown threshold
  from a peak, so a cooldown counted in bars is the wrong instrument and a single-bar
  breaker is the right one.
- Forward paper evidence, which per ADR-0027 should outweigh any backtest here: a
  24/7 paper session where the halt either never fires (the level is off) or holds the
  book flat through a recovery (the cooldown is too long).
- The dispersion floor being wrong because the *bar interval* changed: 30 bars is 30
  days at `1d` and 2.5 hours at `5m`, and the floor scales with per-bar sigma. A run at
  another interval must re-derive it.

## Deliberately not calibrated

- **`max_daily_loss_pct` stays off.** Over 26,090 bar-to-bar portfolio returns at 80%
  volatility the worst single bar lost **9.29%** and nothing reached 10%, so any
  breaker at or above 10% is a dead knob on this evidence, and one at 5% would fire on
  0.345% of bars — ~90 times a decade — for no measured benefit. A diversified,
  partly-invested book's per-bar loss distribution is far tighter than its assets'
  (stdev 1.76% against a 5.04% per-asset sigma), and GBM has no tails, which is exactly
  where a real crypto breaker would earn its keep. Sizing it needs real returns, so it
  is left `None` and said out loud rather than guessed.
- **`max_position_pct` and `max_gross_exposure` are unchanged, not endorsed.** They
  measured as non-binding here, which is a statement that they are not the defect —
  not that 25% concentration is right for a four-name crypto universe where two names
  are most of the market cap. Gross ≤ 100% (no leverage) is deliberate and, if
  anything, matters more in a market with no closing bell to interrupt a liquidation.
- **`_TRADING_DAYS = 252` in `risk.py` is documented, not fixed.** It annualizes
  realized volatility for the ADR-0015 vol target. On a 365-bar-per-year market it
  understates realized volatility by `sqrt(252/365) = 0.8309` and therefore overstates
  the `target / realized` scale by `sqrt(365/252) = 1.2035` — a volatility-targeted
  24/7 book would be allowed **20.4% more gross than it asked for**. Nothing is
  mis-annualized today: `target_volatility` is `None` in both postures, so this is a
  latent seam rather than a live bug, and the crypto posture does not turn it on. It is
  not fixed here because a market's periods-per-year belongs to the market calendar
  (KAN-687 / KAN-705); answering it a second time inside the guardrails is how two
  answers to one question drift apart. `_VOL_WINDOW = 20` has the same shape — four
  weeks of an equity calendar, twenty days of a 24/7 one.
- **`halt_cooldown_bars` is a count, not a duration** — the unit mistake ADR-0049
  named for the live silence tolerance, sitting in `RiskConfig`. 30 bars is six weeks
  of an equity calendar, 30 days of a 24/7 one, and 2.5 hours at `5m`. Converting it
  needs the same calendar as the point above, so it is recorded as the seam that card
  joins rather than fixed twice.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Scale `max_drawdown_pct` with volatility (0.20 → 0.80) | Measured: the same tail rank is 78%, past a 2022-style crypto drawdown, and 0.50 already produced **zero** halts. That is the guardrail switched off, which is what the card forbids. |
| A middle level, 0.35, still latching | Measured: -13.41% against +539.93% neutralized, 319 rejections, 3,319 of 3,650 calendar days halted. Widening the level does not fix a latch; it just delays it by 42 bars. |
| A middle level, 0.35, with recovery | Halted **once** in ten years. A threshold that fires once a decade on an asset whose drawdowns are routine is decoration, and it is a number with no evidence behind it. |
| Recovery via `halt_recovery_drawdown_pct` instead of a cooldown | Measured: 1 halt, never resumed, +11.72%. The halted book drains to cash and its drawdown freezes above the threshold (ADR-0031 §2). It is the latch wearing a recovery knob. |
| Make recovery the default for *every* market | Silently changes every prior equity result and reverses ADR-0031's deliberate "recovery on by default is the wrong thing to relax without being asked". The equity defaults are byte-identical here, and that is load-bearing. |
| An `asset_class` / `market` field on `RiskConfig` | Duplicates the market-calendar type another lane owns, and would make the config carry a fact it never reads. A preset is a config, not a mode. |
| Also add a crypto `max_daily_loss_pct` | No evidence at hand can size it (worst measured single-bar portfolio loss 9.29%, no fat tails in GBM), and a dead knob is worse than an absent one because it reads as protection. |
| Ship a `--market crypto` CLI flag now | `cli.py` is reserved for the epic's integration PR so three parallel lanes cannot collide on it. The seam is the deliverable; the flag follows. |

## Consequences

- Buys: a crypto run can be *protected* and still finish honestly, the same trade
  ADR-0031 made for long equity backtests, now mandatory where the latch is unanimous.
  The equity path is untouched — same defaults, same hashes. Two named postures make
  the market assumption explicit where it was previously invisible.
- **Costs, stated plainly. Bounding the halt is not a return improvement.** Against
  the latch the posture wins on every seed measured (10/10) — that is the defect being
  fixed. Against *no* drawdown halt at all it is a coin flip: 4 of 20 seeds over
  2015-2025, 4 of 10 over 2015-2020. A cooldown re-arms on the calendar, not on
  evidence that anything improved, so it will resume into a continuing decline; that is
  ADR-0031's stated cost, and it arrives more often here because the switch fires more
  often. A test asserts the coin flip in both directions, so if it ever became
  unanimous the honest reading would be that the halt had stopped costing anything.
- The posture is reachable only from Python until the epic's CLI card lands. A
  `trading backtest --source csv` run over real crypto bars today gets the **equity**
  posture and will latch in its first year — which is the pre-existing behaviour, now
  documented rather than surprising.
- Nothing is added to `result.json` and `RESULT_SCHEMA_VERSION` stays **1**; the halt
  episodes ADR-0031 already emits are exactly the observability this posture needs, and
  they now describe a run that halted seven times instead of a boolean that says the
  run ended.
- Forecloses nothing: a per-market calendar (KAN-687/KAN-705) can later drive
  `_TRADING_DAYS`, `_VOL_WINDOW` and a duration-based cooldown from one place, and a
  fat-tail single-bar breaker remains additive on the same seam once real returns exist
  to size it.
