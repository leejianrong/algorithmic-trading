# ADR-0070: Time-series (absolute) momentum, long-or-cash, on a liquid ETF basket

- Status: Accepted
- Date: 2026-08-23
- Deciders: strategy developer (project owner)
- Card: KAN-640 (EPIC-105, "Beat the benchmark: long strategies")

## Context

EPIC-105 needed a first entry, and KAN-640 was promoted to the top of it on
2026-08-16 when the universe narrowed to S&P 500 + liquid ETFs on free data, for
four reasons that make it the best-fitting strategy on the board: decades of
genuine out-of-sample evidence (time-series momentum / managed futures, not a
data-mined pattern); no survivorship problem when run on liquid ETFs, which
barely delist (ADR-0027's binding constraint on every stock-picking strategy in
this repo simply does not apply); full free history on yfinance; and it cashes
in the one structural edge an individual account has over a mandated fund — the
ability to sit 100% in cash, which fits the bench's existing long-or-flat design
(ADR-0011) with zero change.

Two existing strategies needed checking before deciding whether this needed a
new file. **`momentum.py`** already scores each symbol *independently* — no
cross-sectional ranking — which is the right shape. But three things about it
don't fit classic time-series momentum: its default lookback is 60 bars (~1
quarter, not the 12-month academic convention), it has no "skip the most recent
month" option (the standard 12-1 construction, which avoids the well-documented
short-term reversal effect sitting in the most recent month), and — the part
that actually matters for correctness — **it divides weight by the whole
universe, not by the count currently in trend**: `long_weight = self.weight /
len(bars)`. On a 12-symbol universe where only 2 names are trending, `momentum`
would allocate `weight/12` to each rather than `weight/2`, silently under-using
the intended capital whenever the trending set is a strict subset of the
universe — which, for absolute momentum, is the normal case, not an edge case.
**`cross_sectional.py`** has the right *mechanics* — a rebalance cadence that
controls turnover, and re-targeting every symbol to 0.0 or its share on each
rebalance — but the wrong *signal*: it ranks symbols against each other and
holds a fixed top-K, which is precisely the cross-sectional (relative-strength)
question this strategy must not ask. Time-series momentum's whole point is that
each asset's own trend decides its own weight, independent of how the other
eleven are doing; a top-K selection would force an allocation to the least-bad
trend even when *nothing* is genuinely trending, which quietly turns "the
edge is long-or-cash" into "the edge is long-or-worst-K".

## Decision

**New file, not an extension.** `src/trading/strategies/trend_following.py` —
`TrendFollowing`, registered as `trend_following`. Reusing `momentum.py` would
mean either changing its weight-normalization semantics under existing callers
and tests (a behavior change, not an addition) or growing a second mode inside
one class (`cross_sectional.py`'s exact reason for being its own file). A new
class borrowing `cross_sectional`'s rebalance-cadence *mechanics* while keeping
`momentum`'s per-asset independence is the smallest correct shape.

Behavior, parameterized (all four sweepable via `trading sweep --param`):

- **Score, per asset, independently.** On each rebalance, for **every** symbol
  present this bar: trailing total return from `lookback` bars ago to
  `skip_recent` bars ago —
  `history[-1-skip_recent].close / history[0].close - 1` off
  `context.history(symbol, lookback + skip_recent + 1)` (past+present only,
  ADR-0001). Defaults `lookback=252`, `skip_recent=21` — classic **12-1
  momentum**: a 12-month lookback with the most recent month excluded. No
  symbol is scored against any other; a name without the full window is
  skipped (still warming up).
- **In trend, or cash. No ranking, no forced allocation.** A symbol is "in
  trend" iff its own signal is positive — never relative to the others. Every
  in-trend symbol gets `weight / (count currently in trend)`; every symbol not
  in trend (including one still warming up) gets `TargetWeight(sym, 0.0)`. This
  is the one substantive difference from `momentum.py`: two names trending in a
  12-name universe get half the target capital each, not a twelfth. When
  **nothing** is trending, the entire book goes to 0.0 — pure cash — which is
  the strategy working as designed, not a degenerate case; the whole basket
  being simultaneously in a downtrend is rare but not impossible, and the ETF
  basket makes that safe (cash, never a forced short, ADR-0011).
- **Cadence.** Rebalance only every `rebalance_days` bars (default 21 ≈
  monthly), reusing `cross_sectional`'s cadence mechanism verbatim: the first
  bar the universe is warm, then every `rebalance_days` bars thereafter. This
  is the turnover control, and it is why a monthly-or-slower trend signal keeps
  turnover far inside any sensible cost budget (KAN-860) — measured below.
- **Warmup.** Until at least one symbol has the full `lookback + skip_recent +
  1` window, stay flat (return no intents).

**Weighting: equal-weight across the in-trend set, not vol-scaled.** Inverse-
volatility weighting (closer to real managed-futures practice) was considered
and deliberately **not built**. `indicators.rolling_std` computes the
population standard deviation of raw *close prices*, not of *returns* — using
it directly would weight by price level (SPY at ~$450 vs. GLD at ~$180 vs. TLT
at ~$90), not by genuine volatility, which is a subtly wrong result masquerading
as a sophistication. Building a correct return-based volatility helper is a
small addition on its own but is out of scope for this slice per the ticket's
explicit "nice to have, not a requirement — don't over-build," and shipping the
naive (wrong) version would be worse than shipping equal-weight.

**New universe: `trend_etfs` (12 symbols, `universe.py`).** SPY, QQQ, IWM, EFA,
EEM (equities: US large/tech/small, international developed/emerging), XLE,
XLF (two sector SPDRs, for intra-equity rotation), TLT, IEF (two Treasury
durations — long and 7-10yr, genuinely different rate sensitivity), GLD
(gold), and two names `core10` does not have: **DBC** (broad commodities,
Invesco DB Commodity Index Tracking Fund, 2006) and **UUP** (US Dollar Index,
Invesco DB US Dollar Index Bullish Fund, 2007) — added because time-series
momentum's edge comes from diversification *across independently trending
asset classes*, and `core10` has no commodity or currency exposure at all.
Eight of the twelve are shared with `core10` deliberately: the survivorship
reasoning in the module docstring (caveat 3) already applies to broad,
long-lived ETFs, and there is no benefit in re-litigating well-vetted,
broker-verified picks. No sector-cap flag is required to use it, but a
`sectors` map ships anyway (asset-class bucket labels, matching `core10`'s
convention) since every `Basket` carries one.

## What was measured (real yfinance data, 2007-06-01 to 2023-12-31, `--source yfinance`)

Every strategy default (`lookback=252`, `skip_recent=21`, `weight=0.9`,
`rebalance_days=21`), starting cash $1,000, `--benchmark SPY`. Exact commands
and full stdout in the PR description.

**Run 1 — default guardrails (the CLI's actual default today):**

```
Final equity:  $813.81       Total return: -18.62%   Sharpe: -0.40
Max drawdown:  21.85%        Halt: fired 2008-10-22 (drawdown 20.5% >= 20.0%)
```

The kill switch (ADR-0009/0013) halts on 2008-10-22 and **never re-arms**
because `halt_recovery_drawdown_pct`/`halt_cooldown_bars` are unset by
default — the run spends the remaining 15 years refusing entries (avg
exposure 4.50%), exactly ADR-0031's documented failure mode. This is **not a
defect in `trend_following`**: a second run over 2010-2023 (still inside the
default posture) hits the *same* wall at the March 2020 COVID crash
(halted 2020-03-18, never re-armed, avg exposure 59.25% — most of the window
already spent). A default-guardrails backtest of **any** strategy over a
decade-plus of equity history is dominated by whichever crash happens to
cross the 20% threshold first, which is a statement about the guardrail
default over a long horizon, not about this strategy's signal.

**Run 2 — `--halt-cooldown-bars 252`** (one year; a round, legible unit,
chosen only to let the drawdown latch re-arm rather than as a calibrated
value — that calibration is ADR-0055's domain for a different market, not
re-litigated here):

```
Final equity:  $1,681.56     Total return: +68.16%    Annualized: +3.19%
Sharpe:        0.36          Sortino: 0.50             Calmar: 0.15
Max drawdown:  21.85%        Win rate: 81.46%          Turnover: 240.04%
Trades:        119 entries over 4,175 bars (29.8 trades/free-parameter)
Benchmark (SPY): +326.13%
Beta: 0.32   Alpha (ann.): +0.14%   Correlation: 0.66   Info ratio: -0.46
Halt episodes: 2 (2008-10-22 -> 2009-10-22, 2020-03-18 -> 2021-03-18), both re-armed
```

This is the honest read of the signal: **positive but modest standalone
performance** (Sharpe 0.36, alpha essentially flat at +0.14% annualized) that
underperforms SPY's 2007-2023 bull-market total return by a wide margin, which
is expected and stated up front in the ticket — trend following's value is
**not** standalone return. Turnover (240% annualized on a monthly cadence, 12
names) is well inside any sensible cost budget for commission-free,
fractionable ETFs (KAN-860's sibling card). 29.8 trades per free parameter sits
just under ADR-0029's 30-trade significance floor — an honest small-sample
caveat printed by the existing `--min-adv`-adjacent significance check, not
suppressed.

**Correlation is 0.66 to SPY, higher than a "low-correlation diversifier"
headline might suggest — stated plainly, not glossed over.** Five of the
twelve names (SPY, QQQ, IWM, EFA, EEM) are equity index funds, and this is a
**long-or-cash** strategy (ADR-0011), not long/short: during an equity
downtrend it goes to cash rather than profiting from the decline the way a
short leg would, so its return stream still correlates positively with equities
whenever it is invested in them. Classic managed-futures' low-correlation
result comes from the short leg on the *losing* assets, which this bench
cannot build without revisiting ADR-0011 (explicitly out of scope — real
shorting is EPIC-106, gated on a long strategy working first). This basket's
non-equity third (TLT, IEF, GLD, DBC, UUP) is exactly what is expected to carry
whatever diversification this construction can deliver, and **that is
precisely the portfolio-level question KAN-641 exists to answer** — correlation
and drawdown contribution measured against an equity book, not standalone
Sharpe. This ADR does not attempt that measurement; it only builds the
strategy KAN-641 needs to measure.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Extend `momentum.py` in place (change its weight normalization, add `skip_recent`) | Changes existing behavior under existing callers/tests for a class whose whole point was "one signal, transition-driven, weight fixed by universe size." A behavior change disguised as an extension. |
| Extend `cross_sectional.py` with a "no ranking" mode | Would need an `if absolute_mode:` branch inside one class whose entire design (rank, pick top-K) is the wrong shape for this signal — `cross_sectional.py`'s own docstring exists to distinguish it from an absolute strategy. |
| Vol-scaled (inverse-volatility) weighting, as the ticket's nice-to-have suggested | `indicators.rolling_std` is price-level stdev, not return volatility — using it naively misweights by price level (SPY vs. GLD vs. TLT), a wrong number that reads as a feature. A correct version needs a new returns-based helper, explicitly out of scope ("nice to have, don't over-build"). |
| A skip-window of 0 (plain 12-month momentum, no reversal guard) | Classic academic construction skips the most recent month specifically to avoid the short-term reversal effect; `skip_recent` is a sweepable parameter, so this is a default choice, not a foreclosed option. |
| Re-rank/reweight every bar instead of on a cadence | Would thrash turnover exactly the way `cross_sectional.py`'s ADR-0025 already reasoned through; a monthly-or-slower cadence is the existing, proven turnover control in this codebase. |
| Widen `max_drawdown_pct` or disable guardrails to avoid the halt-latch confound in the headline number | Exactly what CLAUDE.md's domain invariants forbid ("calibrate a guardrail; never widen it until nothing trips" — ADR-0055). `--halt-cooldown-bars` re-arms the *existing* guardrail rather than weakening it, and is reported as a supplementary run with the confound named, not a substitute default. |

## Consequences

- A first strategy in EPIC-105 (0/5 -> 1/5) with a genuinely different signal
  shape from every existing entry: absolute (not relative), long-or-cash
  across a variable-size subset (not a fixed top-K or a single symbol).
- `trend_etfs` is the fourth curated basket; it shares `core10`'s
  survivorship-reduced-not-removed status (module docstring caveat 3) and adds
  two genuinely new asset-class exposures (commodities, currency) that no
  existing basket has.
- **The default-guardrails halt-latch confound is now demonstrated on a third,
  independent strategy family** (after `cross_sectional`/ADR-0031 and the
  crypto posture/ADR-0055): any long-horizon equity backtest under the
  unmodified default posture is dominated by whichever crash crosses 20%
  drawdown first, whatever the strategy. This is not new evidence about the
  *level* (ADR-0031/0055 already established the mechanism), but it is a data
  point that the failure is strategy-agnostic on the *equity* calendar too, not
  only at crypto-like volatility — worth the PM's attention if a future
  card considers an equity-side `RiskConfig.equity_long_horizon()` posture
  analogous to `RiskConfig.crypto()`.
- No engine, interface, or CLI change: `trend_following` fits the existing
  `Strategy` seam exactly like every prior strategy, reads only
  `context.history`, and is wired into the registry only.
- Explicitly leaves the portfolio-level correlation/drawdown-contribution
  measurement to KAN-641, per the ticket's scope. Also leaves EPIC-106 (real
  shorting) untouched — this strategy needed no change to ADR-0011.
