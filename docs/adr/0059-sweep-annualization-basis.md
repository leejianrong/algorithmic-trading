# ADR-0059: A sweep annualizes on the basis it was run at, and cannot mix two

- Status: Accepted
- Date: 2026-08-14
- Deciders: strategy developer (project owner)
- Tickets: KAN-840 (EPIC-78). Amends ADR-0057; applies ADR-0054 to `sweep.py`.

## Context

`sweep.py` computed every trial's metrics with

```python
return compute(result), len(result.equity_curve), curve_moments(result.equity_curve)
```

`metrics.compute`'s second parameter is `periods_per_year`, and it defaults to
`252.0`. Nothing in `sweep.py` ever passed it. So **every trial in every sweep, at
every interval, on every market, was annualized on the US-equity daily year** —
Sharpe, Sortino, Calmar, annualized return and turnover alike. `trading sweep
--interval 5m` reported a 252-day year for five-minute bars.

This is ADR-0054's defect one module along, and it is not crypto-specific: it has
been wrong for every intraday sweep since intraday shipped (ADR-0022). ADR-0054
fixed `frequency.py` and threaded the basis through `backtest` and `paper`;
`sweep.py` was never on that path, because `run_sweep` has no frequency parameter at
all — ADR-0022 deliberately makes the interval an *adapter-construction* property,
so the adapter knew it and the sweep did not.

### Magnitude, measured

The reported figure is out by `sqrt(true / 252)`. Measured against the real CLI at
`--interval 5m` (`periods_per_year = 19,656`):

| combination | reported (252) | correct (19,656) | ratio |
|---|---|---|---|
| `fast=5, slow=30` | 0.0972 | 0.8587 | 8.8318 |
| `fast=5, slow=50` | 0.1052 | 0.9291 | 8.8318 |
| `fast=10, slow=30` | 0.2690 | 2.3756 | 8.8318 |
| `fast=10, slow=50` | 0.4053 | 3.5791 | 8.8318 |

`sqrt(19656/252) = sqrt(78) = 8.8318`, so the card's predicted **8.83x at 5m** holds
exactly, as do its 2.55x / 3.61x / 19.75x at 1h / 30m / 1m. The direction is
ADR-0054's: the equity basis is the *smaller* one, so a profitable combination is
**understated** and a losing one is **flattered**. `annualized_return` inverts
visibly — a +2.52% month came out as **0.351%**, because a month of five-minute bars
was being counted as eight years of daily ones.

### The mechanism the card described is not the one that shipped

The card said trial deflation "is computed on the wrong basis" because
`deflated_winner` defaults to 252. **That is not what happened on the `sweep` command
path**, and stating it correctly matters because the wrong explanation makes the
defect look uniform and therefore harmless.

`cli._sweep_significance_block` already passed `freq.periods_per_year`. So the
deflation received the **correct** scalar — and applied it to `trial_sharpes()`,
which returns `run.metrics.sharpe`, **annualized at 252**. One calculation, two
years:

```python
root = sqrt(periods_per_year)  # correct: sqrt(19656)
per_bar = [s / root for s in trial_sharpes]  # but s was annualized at sqrt(252)
```

Measured on the same 5m sweep, the deflation block `main` printed against what it
should have printed:

| figure | main (mixed) | correct | ratio |
|---|---|---|---|
| `observed_sharpe` | 3.579118 | 3.579118 | 1.0000 |
| `null_best_sharpe` | 0.154831 | 1.367427 | 8.8318 |
| `trial_sharpe_stdev` | 0.147160 | 1.299684 | 8.8318 |
| `probability` | 0.849990 | 0.748376 | 0.8805 |

`observed_sharpe` was **already right** — it is recomputed from the winner's own
per-bar moments with the correct scalar. Only the *null it is scored against* came
from the 252-annualized population. So the winner was measured against a bar
**8.83x too low**, and its deflated probability was reported as **0.85 where the
truth is 0.75**: the correction was too weak, in the flattering direction, on the
one figure ADR-0039 exists to make un-flattering.

Uniformly wrong would at least have been monotonic and self-consistent. This was
**incoherent**, in exactly ADR-0054's sense of a report pairing an honest drawdown
with a Sharpe from another market's year — and it was visible on stdout the whole
time. A 5m sweep printed:

```
1     10    30    0       0.593   2.52%         1.47%
...
Trials:        4 scored; the luckiest skill-free one would show Sharpe +0.08 (observed +5.24)
```

The table calls the winner **0.593**; the block four lines below calls the same run
**5.24**. Nobody looked.

### Why it survived

**The ranking is genuinely unaffected.** One constant factor applied to every trial
cannot reorder them, and `total_return` / `max_drawdown` do not scale with the basis
at all — so the table was internally consistent and only its absolute figures were
wrong. A sweep is read for its ordering, and the ordering was right.

### The walk-forward path had the same defect, silently

`run_walk_forward` shares `_run_combo`, so every `IS sharpe +1.45 -> OOS sharpe
-1.08` line was on the daily year too. It is the *worse* half: a sweep at least
printed a deflation block whose observed Sharpe openly contradicted its table, while
a walk-forward prints fold Sharpes with nothing on screen to disagree with them. The
card did not mention it. Measured at 5m, `IS +1.45 -> OOS -1.08` becomes `IS +12.85
-> OOS -9.51`, and mean OOS Sharpe `-0.15` becomes `-1.34`.

(`sharpe_retention` is a *ratio* of two Sharpes on one basis, so it is basis-free and
printed `-15%` both before and after — a useful internal check that only the
annualization moved.)

## Decision

**1. The basis is threaded, not sniffed.** `_run_combo` takes a **required**
`periods_per_year` and passes it to `compute`. `run_sweep` and `run_walk_forward`
each gain a keyword `periods_per_year`, and `trading sweep` supplies
`freq.periods_per_year` — the same `Frequency` it already built for the deflation
block, so `--interval` and `--market` (ADR-0057) both reach the table.

The *interval* never arrives here. ADR-0022 keeps the bar length an
adapter-construction property and the `DataAdapter` protocol deliberately does not
expose it, so reading the frequency back off the adapter would breach that seam.
What travels is the annualization **basis** — the one number the metrics need —
never the frequency. `sweep.py` still knows nothing about bar lengths.

`_run_combo`'s parameter is **required** while the two public entry points default
theirs. Defaulting the private one would leave the identical silence one layer down;
the assumption is made once, visibly, at the surface where a caller can see it.

**2. A summary carries the basis it was scored on.** `SweepSummary.periods_per_year`
and `WalkForwardSummary.periods_per_year`, both additive with defaults so
hand-built fixtures stay valid. `metrics.sharpe` is a bare float with no unit
attached; a summary that did not say which year it used could only be read correctly
by someone who already knew.

**3. Mixing two bases is refused, not documented.** `deflated_winner` now defaults
`periods_per_year` to the summary's own, and an explicit value that *disagrees*
raises `ValueError`. The trial Sharpes are already fixed at the basis they were
computed on, so "deflate these at a different year" has no correct answer to give —
a caller bug in the same class as `deflated_sharpe` raising on an empty
`trial_sharpes`. This is ADR-0056's move: **remove the combination rather than
document it.** The bug that shipped is now unrepresentable rather than merely
unlikely.

**4. The default is the equity daily year, keyword-only.** `DEFAULT_PERIODS_PER_YEAR
= TRADING_DAYS_PER_YEAR`, spelled as the calendar view rather than a bare `252.0` so
the assumption is legible where it is made.

This deliberately does **not** follow ADR-0054's "`get_calendar` raises rather than
falling back to equity". That rule governs a *lookup that cannot resolve a name*,
where a silent equity answer is a fabrication. Here there is no lookup: a library
caller sweeping daily equity bars is asking for 252 and should get it. The precedent
is ADR-0054's own `Frequency`, which took its calendar as a **defaulted** fourth
field for exactly this reason — and the incoherence that made this defect dangerous
is closed structurally by decisions 2 and 3, not by the default.

**5. ADR-0057's caveat is deleted.** `cli._sweep_basis_caveat` existed only to
announce this defect ("a sweep's per-run metrics do not take the market's calendar …
fixing it means threading the frequency into `run_sweep`, which is that module's
change to make"). That is now a false statement, so it goes rather than being
reworded. Its two claims survive as assertions instead of prose: that the ranking is
unaffected, and that the market's basis reaches the table.

## Consequences

**A daily equity sweep is byte-identical, proved by hash.** 252 is the correct basis
there, which makes it the cleanest possible regression control. `sweep_daily.csv`
(`3cfadf24…`) and `wf_daily.csv` (`82d00dbb…`) match `e067742` exactly, and their
stdout is identical modulo the `--out` path. So are all three of the standing
baselines — daily backtest `220e0bb8…`, 5m backtest `4ba021e1…` / `c72a884d…`, and
`paper --once` `9608600b…` / `62418717…` / `daa33064…`. Only `sweep_5m.csv` and
`wf_5m.csv` moved, which is the fix.

**Intraday sweep numbers change, and every historical one was wrong.** Any recorded
`trading sweep --interval` result from before this commit understates its
risk-adjusted figures by `sqrt(periods_per_year / 252)` and overstates its deflated
probability. The ranking in those records is still valid.

**`--market crypto` now reaches a sweep's metrics**, closing the gap ADR-0057
recorded against itself. A crypto daily sweep's Sharpe scales by `sqrt(365/252)` =
1.2035x, pinned against a `--source csv` fixture whose bars are identical on both
markets by construction (ADR-0057's own lesson: the synthetic generator produces a
*different* series per calendar since ADR-0056, so it cannot isolate the basis).

**Still open, deliberately not absorbed here:**

- **KAN-677** — `--folds` prints no deflation of its own. `WalkForwardSummary` now
  carries the basis that block will need, but the block is not built.
- Nothing checks that `periods_per_year` matches the interval the `adapter` was
  actually constructed at. The CLI derives both from one `Frequency` so they cannot
  diverge there, but a library caller can still pass a 5m adapter and a daily basis.
  Closing it properly means the adapter declaring its frequency, which is the ADR-0022
  seam and a larger decision than this card.
- ADR-0039's other two gaps are untouched: `paper` has no `--bootstrap`, and there is
  no cross-invocation trial ledger.

## Verification

- 9 new tests in `test_sweep.py` fail against unmodified `sweep.py` before the fix;
  53 pass after. 4 new CLI tests in `test_cli_sweep.py`, 1 in `test_cli_market.py`.
- **Mutations**, reverting one hunk at a time over the three affected modules (128
  tests):

  | reverted hunk | red |
  |---|---|
  | `_run_combo` back to `compute(result)` — the card's defect | 9 |
  | `deflated_winner` back to a bare 252 default, guard removed | 3 |
  | `run_sweep` stops recording the basis on its summary | 8 |
  | `run_walk_forward` stops recording the basis on its summary | 1 |
  | CLI stops passing the basis to `run_sweep` | 5 |
  | CLI stops passing the basis to the walk-forward | 1 |

  The last one survived the first pass — nothing exercised `sweep --folds` at a
  non-daily interval, so the walk-forward CLI wiring was untested. A test comparing
  the CLI's fold CSV against `run_walk_forward` invoked directly on the 5m basis now
  covers it. (A first attempt at the third row only changed a default the constructor
  overrides, and correctly survived; the corrected mutation drops the constructor
  argument.)
- One golden deliberately updated:
  `test_cli_market.py::test_a_sweep_says_which_figure_the_market_does_not_reach`
  asserted the ADR-0057 caveat text. It is replaced by the positive claim, and by a
  `--source csv` test that the sweep's Sharpe scales by `sqrt(365/252)` across
  `--market`. No golden pinned a wrong *number*; the one that existed pinned a
  wrong *sentence*.
