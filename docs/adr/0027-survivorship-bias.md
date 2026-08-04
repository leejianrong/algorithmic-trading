# ADR-0027: Survivorship bias in curated backtest universes — accepted and documented, not fixed

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

The bench runs strategies over a symbol universe the operator names: a comma list,
or since ADR-0024 a curated basket such as `@blue20`. `blue20` is 20 mega-cap US
names — it contains NVDA, TSLA, and LLY. Those names are in the list *because we
already know how they turned out*.

That is survivorship bias, and it is baked in twice over:

1. **The universe is chosen with hindsight.** A basket assembled in 2026 is a list
   of winners. Backtesting it from 2018 does not ask "how would this strategy have
   done?" — it asks "how would this strategy have done if I had known in 2018 which
   twenty stocks would be giants in 2026?" The mega-caps of 2018 that stagnated,
   were acquired, or blew up are simply not in the candidate set, so the strategy
   is never given the chance to buy them and lose.
2. **The data source cannot supply the missing names.** yfinance (ADR-0003) serves
   history for tickers that exist *today*. It has no delisted symbols and no
   point-in-time index constituents, so even a well-intentioned "S&P 500 in 2018"
   run would silently resolve to today's members. Bankruptcies and delistings —
   the exact tail the bias hides — are unreachable from this data path.

The compounding factor is that the bias is *invisible in the output*. A biased run
produces a perfectly plausible equity curve, Sharpe, and drawdown; nothing in the
report looks wrong. Meanwhile `universe.py` already documents the far *smaller*
fractionability caveat in its module docstring, and before this ADR the repo
mentioned survivorship bias nowhere in `src/` or in any of the 25 preceding ADRs.
A bench whose stated purpose is honest numbers over flattering ones cannot leave
its single largest known source of flattery undocumented.

## Decision

**Accept the bias, document it prominently, and do not pretend it is fixed.**

- **Name it where a reader meets the universe.** `src/trading/universe.py`'s module
  docstring carries a second honesty caveat, next to the existing tradability one,
  stating that `blue20` is today's winners and that backtests over it are
  survivorship-biased. It cross-references this ADR and keeps the existing blunt
  tone.
- **State the direction and rough size of the distortion honestly.** The bias
  inflates results, and the mechanism is understood: removing losers from the
  candidate set raises mean return and cuts realized drawdowns and tail risk. The
  effect is commonly discussed as being on the order of **one to a few percentage
  points of annualized return** for broad equity universes, and larger for
  concentrated, high-turnover, or small-cap strategies — for a top-K basket of
  today's mega-caps, plausibly *more*. **This repo has not measured it.** We
  deliberately do not cite a precise number or a specific study, because we cannot
  verify one here; treat the magnitude as an order-of-magnitude expectation, not a
  correction factor. Do not "adjust" a backtest result by any of these figures.
- **Scope which results are affected**, so the caveat is actionable rather than a
  blanket disclaimer:
  - **Affected:** every `trading backtest` over a curated basket or any
    hand-typed present-day symbol list, and every `trading sweep` /
    walk-forward result (ADR-0016) computed on such a universe — the sweep
    inherits the bias from the universe and *concentrates* it, since ranking by
    Sharpe over survivors selects parameters tuned to known winners.
  - **Not affected:** forward `trading paper` runs (V5) and live Alpaca paper
    runs (ADR-0020). Forward trading is survivorship-free by construction — at
    each bar it can only trade what exists at that moment, and a name that later
    delists hurts the account exactly as it would in reality. Synthetic-data runs
    (ADR-0012) carry no survivorship bias either, because their symbols are
    generated, not selected — but they carry no real information about returns
    either, so that is not a virtue to lean on.
  - Guardrails, costs, and fill modelling are orthogonal: they do not create or
    remove this bias.
- **Record the interim mitigation as a rule of interpretation.** Backtest numbers
  on a curated basket are an **upper bound**, not an estimate. Weight forward
  paper results and genuine out-of-sample validation far more heavily than
  in-sample backtests; a strategy that only looks good on `@blue20` history has
  not been validated.
- **Name the real fix and leave it as a future slice.** Fixing this requires a
  **point-in-time, survivorship-bias-free constituent database** — for each
  historical date, the universe as it was known then, including names that later
  died, with their price history. The hook already exists: `--source csv`
  (bring-your-own-data) can feed such a dataset in without an engine change, and
  a point-in-time universe provider would sit next to `universe.py` as a second
  `Basket` source. That work is **not done** and is not scheduled by this ADR.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Fix it now with a point-in-time constituent dataset | The data is the whole cost: free sources (yfinance) have no delisted names, and a survivorship-free vendor feed is a paid, licensed dataset plus an ingestion slice. Real work, own slice — not something to half-do inside a documentation task. |
| Say nothing (status quo) | The bias silently inflates every curated-basket backtest and the numbers look fine. This is the single most misleading thing about the bench's output; leaving it implicit while carefully documenting the smaller fractionability caveat is indefensible. |
| Apply a fixed haircut (e.g. subtract 2% annualized) to reported returns | Manufactures false precision: the true magnitude depends on universe, period, turnover, and concentration, and none of that is measured here. A fabricated correction is worse than an acknowledged unknown, because it looks like a fix. |
| Drop curated baskets entirely and require explicit symbol lists | A hand-typed present-day list is *equally* survivorship-biased and additionally drifts against the sector map (ADR-0024). Removing the convenience would remove none of the bias. |
| Randomize or synthesize the universe to dodge the issue | Synthetic data (ADR-0012) already exists for mechanism testing and says nothing about real returns. Dodging the question is not answering it. |
| Only ever trust forward paper results and stop backtesting | Backtests are still the cheap way to reject bad mechanisms and catch look-ahead or sizing bugs. The answer is to read them as an upper bound, not to discard the tool. |

## Consequences

- Every reader of `universe.py` meets the caveat before using a basket, and this
  ADR is the one place the mechanism, scope, and non-fix are written down.
- Reported backtest and sweep metrics on curated baskets are now explicitly
  labelled *upper bounds*. Any decision about real capital must lean on forward
  paper results, not on `@blue20` history.
- Sweep/walk-forward rankings inherit and concentrate the bias; a "best" parameter
  set from a curated-basket sweep is a hypothesis, not a finding.
- The bias is not measured, so we cannot quantify how much of any backtest edge is
  real. That is an accepted, stated limitation of the bench today.
- The `--source csv` path is the designated integration point for a
  survivorship-free dataset, so this decision forecloses nothing: a future slice
  can add a point-in-time universe provider and, for the first time, *compare* a
  biased and an unbiased run on the same strategy.
