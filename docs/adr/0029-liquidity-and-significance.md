# ADR-0029: An ADV liquidity floor on the universe, and trades-per-parameter in the report

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

Two honesty gaps sat side by side in the bench, both about whether a reported
number means anything.

**Liquidity was parsed and then ignored.** Every adapter — yfinance, CSV,
synthetic, Alpaca — fills in `Bar.volume`, and nothing in `src/` ever read it.
That let a run hold a symbol the account could never have traded at the modelled
price. The `SimulatedBroker` fills at the next open plus a fixed slippage in basis
points (ADR-0004, Q14); that model is defensible for a mega-cap and fantasy for a
thin one, where a small-account order is a visible share of the day's flow and the
fill walks away from you. `blue20` (ADR-0024) sidesteps the problem by being all
mega-caps, but the bench accepts arbitrary `--symbols`, so the guard has to live
in code rather than in the curation.

**Sample size was invisible.** The report showed return, Sharpe, drawdown,
turnover, and win rate, but never how many trades produced them. `cross_sectional`
has four tunable parameters and `trading sweep` searches them; a Sharpe of 1.5
computed from eleven trades over four knobs is not a finding, and nothing on the
screen said so. The common practitioner floor is 30–50 trades per free parameter,
and the bench was silent about where a given run sat.

Both gaps share a shape: the machinery to check was already present (volume on
every bar; the fill blotter on every result), and only the check was missing.

## Decision

**Screen the candidate universe by average dollar volume, in a formation window
that ends before the backtest starts.** A new `trading.liquidity` module computes
`average_dollar_volume` (mean `close x volume` — dollar, not share, volume, since
a million shares of a $3 stock and a million shares of a $300 stock are different
markets) and `screen_by_adv` keeps the symbols at or above a floor
(`DEFAULT_MIN_ADV = $20M/day`, overridable per run).

The load-bearing detail is **`formation_window`**: the screen reads bars only from
`[backtest_start - 1d - formation_days, backtest_start - 1d]`. Computing ADV over
the backtest range itself would select symbols using volume from days the strategy
has not reached — look-ahead bias, the exact thing ADR-0001 forbids everywhere
else. Excluding the whole calendar day rather than shaving a microsecond keeps the
guarantee independent of how a given adapter stamps its bars. A test records every
range the screen asks the adapter for and asserts each ends before the start line,
so the guarantee is proven at the seam rather than inferred from the numbers.

**Report what was dropped, never filter silently.** `screen_by_adv` returns a
`LiquidityScreen` of per-symbol `LiquidityVerdict`s carrying the ADV each symbol
was judged on and the reason it failed, plus a `describe()` renderer. A silently
shrunk universe is indistinguishable from a typo in `--symbols`.

**A symbol with no data is "unverified", not "illiquid".** No bars in the window
(or an adapter that raises for that ticker) yields `adv = None`, a distinct
reason string, and a separate `unverified` bucket. It is still **dropped** — an
unverifiable symbol is one you cannot size a real order in — but "the data source
had nothing" and "this is too thin to trade" are different facts and are never
conflated. One bad ticker never aborts the screen.

**Count position-opening entries, not fills, and divide by the strategy's
constructor arity.** `metrics.entry_count` reconstructs the running position per
symbol from the blotter and counts only the fills that take a symbol from flat to
held. Counting raw fills would inflate the count several-fold (a round trip is two
fills, a rebalance more), and since trade count is the *denominator* of a
significance claim, over-counting is the flattering direction.
`strategies.free_parameter_count` counts the named arguments of a strategy's
`__init__` — precisely the values `trading sweep --param` can search over, which
is what makes them free in the overfitting sense.

**An unknown ratio is `None`, not `0.0`.** `PerformanceMetrics.trades_per_parameter`
is `None` when the caller supplied no parameter count, and also when the strategy
has none (`buy_and_hold` cannot be curve-fitted by parameter search). A stand-in
`0.0` would render as a *failed* check rather than an *absent* one.
`PerformanceMetrics.underpowered` is therefore `True` only when a ratio is known
*and* below `MIN_TRADES_PER_PARAMETER = 30`, and the report prints an explicit
warning in that case.

**Both additions are opt-in and backward compatible.** The new metric fields carry
defaults, `compute`'s `free_parameters` is keyword-only and defaults to `None`, and
`summarize` renders exactly the previous block when it is omitted — so every
existing caller and test is unaffected. `result_to_dict` already serializes metrics
generically via `dataclasses.asdict`, so the new fields flow into `result.json`
without a `RESULT_SCHEMA_VERSION` bump; the dashboard renders unknown keys and now
shows `n/a` for a `None` metric instead of the string `"None"`.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Compute ADV over the backtest range | Look-ahead bias: selects the universe using volume the strategy has not seen yet, contradicting ADR-0001. The entire point of the formation window. |
| Re-screen liquidity per bar (a rolling universe) | More faithful — a symbol whose liquidity dries up mid-run would be dropped — but it needs a per-bar mutable universe the engine does not model. Recorded as a known limit instead of half-built. |
| Model liquidity in the fill instead (volume-participation slippage) | Strictly better realism and a good later slice, but it changes `SimulatedBroker`'s fill model and therefore every existing number. Screening the universe is the cheap, non-invasive first cut. |
| Filter thin symbols out silently | A shrunk universe would look identical to a mistyped `--symbols`. Every drop is reported with its reason. |
| Treat a data-less symbol as illiquid | Conflates "we don't know" with "we checked and it's bad". The `unverified` bucket keeps a network hiccup from reading as a delisted stock. |
| Share volume instead of dollar volume | Not comparable across price levels; a share-count floor is a hidden bias toward low-priced stocks. |
| Count raw fills as "trades" | Inflates the significance denominator several-fold, in the flattering direction. Entries are the decisions the strategy actually made. |
| Declare each strategy's parameter count by hand in the registry | A second source of truth that silently drifts when a constructor gains an argument. `inspect.signature` cannot drift. |
| Hard-fail a run that is underpowered | Too blunt: a small sample is a caveat on the result, not an invalid run. The bench warns loudly and lets the developer judge. |

## Consequences

- Volume data is finally load-bearing, and a run over an arbitrary `--symbols`
  list can be screened to what the account could plausibly have traded.
- The report can no longer show a flattering Sharpe without also showing how few
  trades produced it, whenever the caller passes the parameter count.
- The screen is **point-in-time**: liquidity is judged once, before the run. A
  symbol that becomes illiquid mid-run stays in the universe. Accepted limit.
- `DEFAULT_MIN_ADV` and `MIN_TRADES_PER_PARAMETER` are judgement calls, not laws.
  $20M/day and 30 trades/parameter are defensible starting points for a small
  account; both are parameters, and a different account size warrants different
  ones.
- The screen costs one extra `get_bars` call per candidate symbol over the
  formation window. For the cached yfinance adapter that is a one-time fetch; for
  a large candidate list it is not free.
- `free_parameter_count` counts *declared* arguments, not *searched* ones. A
  sweep that varies only one of `cross_sectional`'s four parameters still divides
  by four, which is the conservative reading — the other three were still chosen
  by someone.
- Trades-per-parameter measures sample size, not out-of-sample validity. It is a
  necessary condition, not a sufficient one; walk-forward validation (ADR-0026)
  is the other half, and survivorship bias (ADR-0027) still inflates the
  underlying returns regardless of how many trades produced them.
