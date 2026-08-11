# ADR-0057: Selecting a market is one explicit flag, plus a guard for the day someone forgets it

- Status: Accepted
- Date: 2026-08-12
- Deciders: strategy developer (project owner)
- Card: KAN-835 (EPIC-87, "Crypto: a 24/7 market")

## Context

EPIC-87's phase 1 landed three decisions as library seams and deliberately kept all
three out of `cli.py`, so that three parallel lanes could not collide on one file:

- **ADR-0054** — annualization comes from a `MarketCalendar`; `Frequency.parse(label,
  *, calendar=…)` is keyword-only, so `cli.py` needed no change.
- **ADR-0053** — a market that never closes drops the daily session rule; the policy
  is a `RecentWindowFeed` constructor argument, and no `continuous_is_complete` was
  added because `interval_is_complete(1d)` already *is* the rule.
- **ADR-0055** — `RiskConfig.equity()` / `RiskConfig.crypto()`, differing in one
  field (`halt_cooldown_bars = 30`), with `crypto(halt_cooldown_bars=None)` a
  `ValueError`.

Verified against the code rather than the prose, because all three ADRs describe
their own gap and one of them describes it slightly differently from the source:
`get_calendar` raises on an unknown name; `MarketCalendar.is_continuous` exists and
is the only predicate needed for the completeness choice; `RiskConfig.crypto()`
differs from `RiskConfig()` in exactly `halt_cooldown_bars`; and `cli.py` at
`a157123` did read
`interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete`.

The consequence, which is what this card exists to remove: **nothing selected a
market**. A real-crypto run through `--source csv` annualized 365 days of bars on a
252-day calendar, inherited the equity session's completeness rule, and latched its
kill switch in the first year — while printing a confident report. Every seam was
reachable only from Python.

One more thing phase 1 recorded against itself (ADR-0054's Consequences):
`result.json` carried a bare interval label and no market, so
`report._resolve_periods_per_year` parsed `"5m"` on the equity calendar and fed that
to the benchmark-relative block (ADR-0037).

## Decision

### 1. `--market` is an explicit flag, defaulting to `us_equity`

`trading backtest`, `trading paper` and `trading sweep` take
`--market us_equity|crypto_24_7` (aliases `equity`, `us-equity`, `crypto`,
`crypto-24-7`; case- and whitespace-insensitive). The values *are* the calendar
registry's names, so a market has one spelling in the codebase and the alias table is
pure input normalization — exactly the shape of the `@basket` sigil (ADR-0024). The
canonical name is what is printed, logged, and written to `result.json`.

An unknown market is exit 2 naming the known ones, for the reason `get_calendar`
raises (ADR-0054): the fallback *is* the bug.

The same refusal applies one layer up. `_MARKET_POSTURES` maps calendar name ->
posture, and a calendar with no entry **cannot be selected** — it exits 2 saying so
rather than inheriting the equity limits. So a fourth calendar added to `CALENDARS`
later fails loudly until somebody decides what risk it trades under. That is pinned
by a test that registers a `cme_futures` calendar and asserts the refusal.

### 2. Explicit, not derived — and the reasoning, because both options can be wrong

Deriving the market from the symbol format (`BTC/USD` implies crypto) was rejected as
the *primary* mechanism:

- The premise is unverified. Alpaca's crypto symbol format is **KAN-708 and not
  measured yet**; a heuristic shipped today would rest on our guess about a provider's
  strings, and this repo has been bitten repeatedly by inferring instead of measuring
  (ADR-0045's phantom split, ADR-0047's `datetime.min`, ADR-0040's forgiving
  stand-ins).
- Derivation makes the annualization basis a property of a *string*. `--source csv`
  means the operator names the file, so a rename would silently re-annualize a run,
  and the same bars under two filenames would report two different Sharpes.
- It cannot express a market the shape does not encode: a bare `BTC`, an index, a
  future.

The cost of explicit is real and named: an operator can forget the flag, and
forgetting produces the *silent, plausible, flattering-or-incoherent* number this
whole epic was sequenced to prevent — ADR-0054 measured that the equity basis
understates a winner and **flatters a loser** (a -3.73% 5m month scores Sharpe -8.34
instead of -19.28), and because total return and max drawdown do not scale at all, the
report pairs an honest drawdown with a foreign Sharpe.

So the flag does not stand alone.

### 3. A shape guard refuses crypto-shaped symbols on a market that closes

Belt and braces, the way ADR-0036 and KAN-678 are defence in depth rather than
alternatives. Under a non-continuous market, a symbol whose segment after a `/`, `-`
or `_` is a known quote currency (`USD`, `USDT`, `USDC`, `USDP`, `BUSD`, `DAI`, `EUR`,
`GBP`, `JPY`, `BTC`, `ETH`) is **refused with exit 2** before anything is fetched or
written, naming each symbol and pointing at `--market crypto`.

Four things about that rule are deliberate:

- **It is a shape test, not a lookup**, and narrow on purpose. Requiring a separator
  *and* a known quote code is what keeps real share-class tickers safe: `BRK-B`,
  `BF-B` and even a Bloomberg-style `BRK/B` are untouched, and so are all 30 symbols
  in the curated baskets (asserted by a test, not assumed — every one is pure alpha).
  A slash alone was considered and rejected precisely because `BRK/B` exists.
- **It is one-directional.** An equity-looking ticker under `--market crypto` is *not*
  flagged: the operator typed the market, and the signal is weak that way round
  because a legitimate continuous symbol can be a bare `BTC` with no separator, so the
  check would fire on correct usage. A guard that cries wolf on the working path is
  worse than the documented hole (ADR-0053 refused a warning for the same reason).
- **It refuses rather than warns.** A warning scrolls past; the failure it guards
  against is a number that looks right. This is ADR-0028's "the broker said no" side
  of the split — a shape we recognise — not the "we could not ask" side.
- **There is no override flag.** If a false positive is ever found, the fix is to
  narrow the rule (or extend the quote-code set for a shape it misses), because an
  override is a thing an operator sets once and then forgets is set. The escape route
  today is `--market crypto` if it really is crypto, or renaming the symbol.

### 4. A preset composes with explicit risk flags, and the precedence is stated

**An explicitly-passed flag always wins; every limit not passed comes from the
market's posture.** `--no-guardrails` still beats both, since it is an explicit
opt-out from enforcement and therefore from the posture too.

Mechanically, `--max-position` / `--max-gross` / `--max-drawdown` now default to
`None` — "not chosen here", the idiom `--target-vol` and `--halt-cooldown-bars`
already use — and `_build_risk` resolves each field from the posture when the flag is
absent. On `us_equity` the resolved values are exactly the old literals (0.25 / 1.0 /
0.20, cooldown `None`), so no existing invocation moves; the help text names both the
posture source and the equity value, so the numbers did not disappear from `--help`.

Reading the parameter source out of Click (`get_parameter_source`) was the
alternative. Rejected: it makes "was this passed?" invisible in the signature, where
`None` says it in the type, and it would leave the defaults as two numbers in two
places (the option and the posture) that can drift apart.

One consequence worth stating plainly: **a crypto run cannot be talked into a
latching halt from the CLI.** There is no flag spelling "cooldown `None`" — unset
means the posture's 30, and `--halt-cooldown-bars 0` is rejected by `RiskConfig`'s own
validation. That is the CLI-level mirror of `crypto(halt_cooldown_bars=None)` raising
(ADR-0055), and it has a test.

### 5. Completeness: a continuous market has no arms on the expression

```python
def _completeness_policy(market, freq):
    if market.calendar.is_continuous:
        return interval_is_complete(freq.delta)  # daily included
    return interval_is_complete(freq.delta) if freq.is_intraday else default_is_complete
```

Exactly what ADR-0053 decided, and nothing more: no new policy function, no market
enum inside `recent_window.py`, which is untouched. The equity arm is unchanged, and
must be — for a daily bar stamped at the session open the interval rule would withhold
it 13.5 hours past the real close.

### 6. `result.json` records the market — one additive key

```json
"frequency": "1d",
"market": "us_equity",
```

`result_to_dict` / `write_result_json` take `market=` (default `"us_equity"`), emit it
at the top level, and `_resolve_periods_per_year(frequency, override, market)` resolves
the interval label **on that market's calendar**. An unknown market name raises there
too, rather than defaulting to equity; an unknown *label* still falls back, now to that
market's daily basis (252 on equity — unchanged — and 365 on 24/7), because a label is
a reporting detail and a market is not.

This is what closes ADR-0054's recorded gap properly. The alternative it suggested —
having `cli.py` pass `periods_per_year=` explicitly — would have left the document
still unable to say which market produced it, so a reader (or the dashboard) would
have to remember. Additive, so `RESULT_SCHEMA_VERSION` stays **1**, following ADR-0031's
`episodes` and ADR-0039's `significance`.

### 7. Which commands the market reaches

| command | `--market`? | why |
|---|---|---|
| `backtest` | yes | all three seams apply |
| `paper` | yes | all three, including the completeness policy |
| `sweep` | yes | it runs the engine N times; an equity latch would hit every trial |
| `gen-data` | **no — errors** | none of the three seams applies to writing bars; see the gap below |
| `dashboard` | **no — errors** | the market is a property of the `result.json` it reads |
| `verify-universe` | **no — errors** | crypto asset lookup is KAN-708 |

Typer refuses an unknown option with exit 2 ("No such option"), so an unsupported
combination fails loudly rather than being accepted and dropped. Tested for both
`gen-data --market` and `dashboard --market`.

**`sweep` is wired with one honest hole, printed rather than hidden.** `sweep.py`'s
per-run metrics come from `metrics.compute(result)` at the default 252 basis, so a
sweep's `sharpe` / `annualized_return` columns are the equity daily year whatever
`--market` (or `--interval`!) says. That is pre-existing and belongs to that module;
what must not happen is a crypto sweep printing an equity-basis Sharpe with nothing
saying so, so a non-equity sweep prints a caveat naming the basis and stating that
ranking is unaffected (Sharpe ordering multiplies every candidate by the same root).
Demonstrated in one run: table `sharpe 1.196`, deflation block `observed +1.44` —
the same number on the two different bases, 1.2035x apart.

### 8. A non-equity run announces its basis; an equity run's output does not move

`Market: crypto_24_7 (365 days x 1440 min/day) — 1d annualizes at 365 bars/year; risk
posture: halt re-arms after 30 bar(s)` prints above the summary, and only when the
market is not the default — the same "print it when there is something to say" rule
the absent-symbol (ADR-0032) and never-invested-benchmark (ADR-0037) caveats follow,
and what keeps every equity run's stdout untouched.

## What was measured

- **Equity is byte-identical, with one disclosed exception.** The three baselines from
  `cfb4d85` (still valid at `a157123`) were re-run and hashed:
  `equity_curve.csv` matches on all three (`220e0bb8…`, `4ba021e1…`, `9608600b…`) and
  `paper_state.json` matches (`daa33064…`). All three `result.json` differ by **exactly
  one line**:

  ```diff
  4a5
  >   "market": "us_equity",
  ```

  Every metric value, fill, clamp, rejection and equity point is unchanged — proved,
  not asserted: `tests/unit/test_engine_bar_bookkeeping.py` now removes that one key
  and reproduces the **pre-ADR-0057 digest** `c5a97cfc…` exactly, so the ADR-0044
  golden is still pinning `Engine.run` rather than having been re-blessed. The new
  digest for the document *with* the key is `9395b4f2…`. `diff -r` on the paper `--out`
  shows that one line and nothing else; stdout differs only in the embedded `--out`
  path. New hashes: `a/result.json` `01786310…`, `b/result.json` `c72a884d…`,
  `c/result.json` `62418717…`.
- **The calendar seam really moves, and only the right figures.** Measured on bars that
  are identical on both markets by construction — a written CSV through `--source csv`,
  `buy_and_hold`, 60 daily rows: the equity curve is byte-identical, total return and
  max drawdown are unchanged to the last digit, and Sharpe scales by exactly
  sqrt(365/252) = 1.2035x. The 5m factors (19,656 -> 105,120, i.e. 5.3480x and
  2.3126x in Sharpe) are asserted as arithmetic on the two calendars, because no
  offline source serves *identical* sub-daily bars on both markets (see the next
  point).
- **`--market crypto` now reaches a genuinely continuous series, end to end.** ADR-0056
  (KAN-830) landed mid-lane — this branch was rebased onto it — and it reads the
  calendar off the construction-time `Frequency`, which `cli.py` already passed to
  `SyntheticAdapter`. So the flag reaches the generator with **no CLI change of its
  own**: `backtest --market crypto --source synthetic --symbols BTC/USD,ETH/USD
  --from 2021-01-01 --to 2021-06-30` produces **181 bars including 52 weekend bars**
  (the equity run over the same range produces the ~124 weekday ones), and
  `paper --once --market crypto` processes 90 consecutive calendar days.
  That also *removed* the earlier control: a synthetic crypto run now differs from its
  equity twin in the series *and* the basis, which is why the rescaling claim above
  moved to a CSV. Two of this lane's tests were written against the pre-ADR-0056
  behaviour, passed locally, and went red on CI's merge with the new `main` — the
  correct signal, and worth recording as the lane's one real friction.
- **The completeness seam is checked at the discriminating instant**, not by inspecting
  a branch: the policy the CLI hands `RecentWindowFeed` is captured, then evaluated on
  a daily bar stamped 13:00 UTC at 00:30 the next day — the session rule says complete,
  the rolling-day rule says not yet. Crypto gets the latter; equity keeps the former;
  a fixture guard asserts the two rules really do disagree there, so a test whose
  branches agreed could not pass silently.
- **The posture seam is checked where it is consumed** — off the `Guardrails` the CLI
  builds — for `backtest`, `paper` and (via `run_sweep`'s `risk=`) `sweep`. Default
  market gives exactly `RiskConfig()`; crypto gives exactly `RiskConfig.crypto()`.
- **The guard was watched firing.** `--symbols BTC/USD,ETH-USD,AAA` with no `--market`
  exits 2 naming both pairs and not `AAA`, writes nothing, and names the fix; the same
  symbols under `--market crypto` run to completion. `BRK-B`, `BRK/B`, `BF-B`, `SPY`,
  `USD` and all 30 curated basket symbols are never flagged.
- **Six mutations, each reverted non-destructively** (fast layer green at 1,241 tests
  before each):

  | mutation | red |
  |---|---|
  | drop the continuous arm of `_completeness_policy` | **1** |
  | parse the interval on the equity calendar in all three commands | **3** |
  | build risk from `RiskConfig.equity()` regardless of market | **4** |
  | make `_check_symbol_shapes` return immediately | **3** |
  | resolve `report`'s basis on `US_EQUITY` regardless of market | **2** |
  | remove the `market` key from `result_to_dict` | **11** (9 + both goldens) |

- **Gates:** `make check` green (1,241 passed, 2 skipped, 58 deselected) and
  `make test-integration` green (4 passed, 43 skipped), on the tree rebased onto
  ADR-0056. 53 new tests, one existing golden deliberately updated (and pinned to its
  predecessor).

## What would change the decision

- **KAN-708 measuring Alpaca's crypto symbol format.** If pairs come back in a shape
  the guard does not recognise (say `BTCUSD`, concatenated with no separator), the
  guard is blind to the very provider the epic targets — that is a reason to extend
  the rule, and if the shape turns out to be unambiguous *and* verified, a reason to
  reconsider deriving the market as a **default** the flag overrides.
- **A false positive on a real equity ticker.** That would say the rule is too wide;
  the response is to narrow it (drop `-`/`_`, keep `/`), not to add an override flag.
- **An operator actually forgetting `--market` in practice** and the guard not catching
  it — e.g. a `--source csv` crypto file named `BTC.csv`. The guard cannot see that
  shape, and it is the most likely real miss. If it happens, the honest next step is a
  data-side signal (a source that *knows* it serves a continuous market and says so),
  not a cleverer string rule.
- **A second continuous market with different risk numbers** would break the current
  one-posture-per-calendar table into something needing its own selection axis.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Derive the market from symbol shape only | Rests on an unverified premise (KAN-708), makes annualization a property of a filename under `--source csv`, and cannot express a market the shape does not encode. Kept as the *guard*, not the mechanism. |
| Explicit flag with no guard | Forgetting it is the silent flattering number the epic was sequenced to prevent; the flag alone puts the whole weight on memory. |
| Warn instead of refusing on a shape mismatch | The output being guarded is a *plausible* report; a warning scrolls past it. ADR-0028's "the broker said no" bucket is a refusal. |
| Refuse on any `/` in a symbol | `BRK/B` is a real Bloomberg-style share class. The rule needs the quote code too. |
| An override flag for the guard | A flag set once and forgotten is exactly the failure mode. Narrow the rule instead. |
| `--market` on `gen-data` / `dashboard` | None of the three seams applies to writing bars, and the dashboard reads the market out of the file it is given. An unsupported flag errors rather than being silently ignored. |
| Scope `--market` to `backtest`/`paper` and reject it on `sweep` | A sweep runs the engine per trial, so the equity latch (ADR-0055: 20/20 seeds, ~90% of the run halted) would corrupt every trial. Wiring it with a printed caveat about the one un-wired figure beats refusing the command. |
| Read Click's parameter source instead of `None` defaults | Hides "was it passed?" from the signature and leaves the default numbers in two places to drift. |
| Put the market in `frequency` (`"5m@crypto_24_7"`) | Mutates a documented field every consumer parses, to avoid one additive key. |
| Bump `RESULT_SCHEMA_VERSION` for the new key | Additive keys leave a v1 reader working, and the dashboard's check is exact equality — a gratuitous bump rejects every `result.json` already on disk. |
| A new `market.py` module for the selection | The selection *is* the CLI surface; the vocabulary already lives in `calendar.py` and the postures in `config.py`. A third module would be a name with no behaviour. |

## Consequences

- One choice reaches all three phase-1 seams. `trading backtest --market crypto
  --source csv --symbols BTC/USD` now annualizes on 365 x 1440, runs the bounded-halt
  posture, and (in `paper`) treats a daily bar as a rolling 24-hour window.
- `result.json` is self-describing: `frequency` + `market` are enough to reproduce
  every annualized figure in the document.
- `recent_window.py`, `engine.py`, `frequency.py`, `calendar.py`, `risk.py` and
  `config.py` are **untouched**. The whole change is `cli.py`, `report.py`, and tests.
- **Known gaps, deliberately not closed here.**
  - **No crypto data source exists.** Offline continuous bars come from
    `SyntheticAdapter` (ADR-0056) and real ones only from `--source csv`; the live
    adapter and broker are **KAN-708**. So the flag is fully exercised offline and has
    never been pointed at a real continuous venue.
  - **The completeness rule is still proved against a constructed bar** and an explicit
    clock state rather than a live feed, because it is a statement about a policy at an
    instant. A `--once` replay parks its `FakeClock` past the range, where both rules
    agree, so a replay cannot discriminate them however continuous its bars are.
  - **`gen-data` cannot write 24/7 bars**, even though `SyntheticAdapter` can now
    generate them, so the obvious way to produce an offline crypto CSV for
    `--source csv` is still Python. Deliberately out of scope: `gen-data` has no
    `--interval` either, and a market without an interval is half a surface. Its own
    small card.
  - **A sweep's per-run metrics still annualize at 252** (`sweep.py`'s
    `compute(result)`), printed as a caveat rather than fixed. Also true of
    `sweep --interval 5m` today, which is a pre-existing defect this card found and did
    not own.
  - **The dashboard does not render the market.** It carries it through verbatim
    (`payload["document"]["market"]`); showing it is one row in
    `static_export.py`'s summary table, additive.
  - **`fetch_span` and the live silence tolerance are still equity-shaped.** ADR-0053
    assessed `fetch_span` (over-asks a 24/7 source by 5.79x daily / 21.39x sub-daily —
    the safe direction) and ADR-0049's `MIN_LIVE_EMPTY_POLLS = 4` was calibrated
    against weekends a 24/7 market does not have. Neither is wrong for crypto in a way
    that loses data; both are now selectable-market-adjacent and unconverted.
  - **`halt_cooldown_bars` is still a count, not a duration** (ADR-0055's own note):
    the posture's 30 is 30 days at `1d` and 2.5 hours at `5m`, and `--market crypto
    --interval 5m` gets the same 30 with no conversion and no warning.
  - **`risk.py`'s `_TRADING_DAYS = 252`** still annualizes vol-target realized
    volatility, latent only because `target_volatility` is off in both postures.
    `--target-vol` was always CLI-reachable; what is new is that it can now be combined
    with a 365-bar-per-year market, i.e. `--market crypto --target-vol 0.5` would allow
    a vol-targeted 24/7 book ~20.4% more gross than it asked for. Not fixed here
    because a market's
    periods-per-year belongs to the calendar (KAN-687/705) and answering it twice is how
    two answers drift.
