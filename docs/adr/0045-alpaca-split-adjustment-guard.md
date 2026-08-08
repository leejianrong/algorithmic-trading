# ADR-0045: Refuse an Alpaca adjusted series that still carries a split

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)
- Amends (does not edit): ADR-0008 (adjusted prices), ADR-0017 (client seam),
  ADR-0018 (Alpaca live verification), ADR-0021 (per-mode price policy)

## Context

ADR-0008 is the bench's oldest correctness invariant: backtests run on
split/dividend-adjusted prices so a corporate action creates no phantom move. On
2026-08-08 that invariant was found broken through `--source alpaca`, and
ADR-0041 recorded it as unfixed. This slice reproduced it independently and
found the diagnosis in that record is **wrong in mechanism**.

### Reproduced live, 2026-08-09 (paper plan, `Adjustment.ALL` vs `Adjustment.RAW`)

AAPL around its 4-for-1 split of 2020-08-31:

| date | raw close | adjusted close | raw/adjusted |
|---|---|---|---|
| 2020-08-27 | 500.04 | 485.03 | 1.0309 |
| 2020-08-28 | 499.23 | 484.24 | 1.0310 |
| **2020-08-31** | **129.04** | **125.17** | **1.0309** |
| 2020-09-01 | 134.18 | 130.15 | 1.0310 |

The adjusted series carries a **-74.15% single-day return** and the raw/adjusted
factor is a flat 1.031 (dividends only) straight *through* a 4:1 split. Alpaca's
`Adjustment.SPLIT` mode returns bytes identical to `Adjustment.RAW` for this
symbol, so no split adjustment is being applied at all.

### The card's premise — "Alpaca stopped applying split adjustments" — is false

It is **one symbol's data, not the provider's pipeline**. Measured the same day,
same account, same code path:

| split | measured adjustment factor across the ex-date | verdict |
|---|---|---|
| **AAPL 4:1 2020-08-31** | **1.0000** | **not applied** |
| TSLA 5:1 2020-08-31 | 5.0001 | applied |
| NVDA 10:1 2024-06-10 | 10.0000 | applied |
| GOOGL 20:1 2022-07-18 | 19.9988 | applied |
| CMG 50:1 2024-06-26 | 50.0006 | applied |

TSLA split on the *same session* as AAPL and is adjusted correctly, so the defect
is not date-scoped either. Six further splits (SHOP, DXCM, PANW, WMT, LRCX, AMZN)
were also correct. This matters for the shape of the fix: a blanket refusal of
`--source alpaca` would be a permanent, repo-wide tax for one ticker's bad data —
and AAPL is in `blue20`, so it is a ticker we use constantly.

### Alpaca disagrees with itself

Alpaca's **corporate-actions endpoint** — a different service from bars, and
available on this free paper plan — reports the split correctly:

```
forward_splits  symbol='AAPL' new_rate=4.0 old_rate=1.0 ex_date=2020-08-31
```

So the provider's own two surfaces contradict each other. That is what makes a
*sound* detector possible rather than a heuristic.

### Why it matters

A backtest spanning 2020-08-31 with AAPL in the universe sees a phantom -74% day.
That wrecks Sharpe and max drawdown and can trip the ADR-0013 drawdown kill
switch on a corporate action that never happened — precisely the failure ADR-0008
exists to prevent, and precisely the failure ADR-0040 already had to fix once in
the yfinance guard (where an *unadjusted* AAPL series ran -73.9% fully invested).

## Decision

**Detect and refuse the specific corrupt series. Do not refuse the provider.**

Every **adjusted** fetch through `AlpacaAdapter` is cross-checked against
Alpaca's own corporate-actions record, and a series whose split was not applied
raises a classified `UnadjustedSplitError` (`data/alpaca_adapter.py`) instead of
being returned. RAW fetches are never checked.

**The measurement is exact arithmetic, not a shape heuristic.** For any bar,
`factor = raw_close / adjusted_close` is the cumulative adjustment the provider
claims at that point. Across a split's ex-date, the *ratio of factors* equals the
split ratio when the split was applied and 1.0 when it was not:

```
applied = (raw[pre] / adj[pre]) / (raw[post] / adj[post])
```

Because raw and adjusted both contain the stock's own move that day, **the move
cancels exactly**. A -30% ex-date and a +30% ex-date give the same answer; the
two verdicts are separated by the split ratio itself (>= 1.5x by construction),
so a 2% tolerance sits nowhere near either boundary. This is the property that
makes the detector safe to act on, and it is pinned by parametrised tests that
move the ex-date bar by +/-30% in both a correct and a corrupt series.

The obvious alternative detector — "a big one-day drop in an adjusted series is
an unapplied split" — cannot tell a split from a crash. Watched failing: swapping
the exact detector for that heuristic makes a *correctly adjusted* series with a
-30% ex-date fail as a false accusation.

**Scoping, in order of what it protects:**

- **RAW is never verified and costs no extra request.** An unapplied split is not
  a defect in a raw series; it is what raw *means* (ADR-0021). This is the
  property the Monday live run depends on, and it is asserted directly.
- **One extra request per (symbol, window)**, memoised on the adapter: the
  corporate-actions lookup. The raw cross-check fetch is a *second* request paid
  only when a measurable split actually straddles bars inside the returned
  window, which for almost every backtest window is never.
- **Only a split with bars on both sides of the ex-date inside the returned
  series** is checked. A window starting at or after the ex-date is uniformly
  post-split, and a uniform rescaling changes no return.
- **Splits below 1.5x (or above 1/1.5) are skipped**, not guessed at. The phantom
  crashes that motivate the guard are all large; a false accusation is worse than
  a missed one here.

**"We could not ask" is not "the data is bad."** If the corporate-actions lookup
or the raw cross-check fails, the adapter logs a WARNING naming the symbol and
window and **returns the bars unverified**. This follows ADR-0028's third bucket
(`unverified`, distinct from `unusable`). The reasoning is asymmetric on purpose:
refusing here would be a *new* failure mode created by our own added dependency,
turning an unrelated provider outage into a total outage of `--source alpaca`,
whereas failing open leaves the caller exactly where they were before this ADR.

**The escape hatch is a constructor parameter, not a CLI flag.**
`AlpacaAdapter(verify_adjustments=False)` returns the unverified series. It is
deliberately a line of Python rather than a flag, because "give me prices I have
been told are wrong" should not be something an operator copies out of a runbook.

**The seam gains its seventh call.** `AlpacaClient.get_splits(symbol, start, end)`
returns our own frozen `SplitEvent` DTO (`symbol`, `ex_date`, `ratio`), so no SDK
type escapes the seam (ADR-0017). `FakeAlpacaClient` gains `set_splits` /
`set_splits_failure`; `RealAlpacaClient` reads `forward_splits` and
`reverse_splits` from a lazily constructed `CorporateActionsClient`. This is the
second widening ADR-0017 anticipated, after `cancel_order` (ADR-0036).

## Alternatives considered

| Option | Why not |
|--------|---------|
| **(c) Refuse adjusted Alpaca bars outright** — the steer's suggested default | Correct while the diagnosis was "the pipeline is broken". The measurements above refute that: 9 of 10 splits are adjusted correctly, so a blanket refusal is a permanent tax for one ticker, and it would keep firing after Alpaca fixed AAPL. The steer explicitly preferred sound detection over a blanket refusal, and detection here *is* sound. |
| **(a) Fall back to yfinance for adjusted history** | Silently changes what a documented `--source alpaca` run means — two providers' prices in one series, with different vendor adjustment conventions. It is also a much larger slice. The refusal message *points* at `--source yfinance`, which keeps the operator in charge of that substitution. |
| **(b) Apply the split adjustment ourselves from corporate actions** | Now genuinely feasible — the endpoint is on the free plan and carries the right rates — and **deliberately not built here**, per the steer's instruction to report rather than build it. It is a bigger change than a guard (it must compose multiple actions, agree with dividends, and stay consistent across overlapping ranges under ADR-0030), and it makes us the vendor of an adjusted series, which is a standing maintenance obligation rather than a temporary patch. Recorded as a follow-up. |
| Detect from the adjusted series alone ("a -50% day is a split") | Cannot distinguish a split from a genuine crash, so it would refuse real data. Watched failing (see above). |
| A one-time canary probe on AAPL at adapter construction | Cheap, but infers a provider-wide claim from one symbol — exactly the inference the measurements above show to be false. It would also go green while other symbols stayed broken. |
| Compare `Adjustment.SPLIT` against `Adjustment.RAW` instead of using corporate actions | Equal for a window containing no split, so it cannot discriminate "no split here" from "split not applied" without the corporate-actions record anyway. |
| Fail closed when the corporate-actions lookup fails | Hands a second Alpaca service a veto over every adjusted run, for a check that is an *addition* to prior behaviour. ADR-0028's `unverified` bucket already settled this shape. |
| A `--no-verify-adjustments` CLI flag | `cli.py` belongs to a sibling lane this batch, and a flag invites routine use. A constructor parameter documents itself at the call site. |

## Consequences

- **`--source alpaca` backtests spanning 2020-08-31 with AAPL in the universe now
  fail loudly** instead of silently reporting a wrecked Sharpe. That is the point,
  and it is a behaviour change an operator will notice.
- **`trading paper --once --source alpaca` is affected.** The `--once` replay
  materialises `[from, to]` through the adapter's default *adjusted* fetch, so a
  replay across 2020-08-31 including AAPL now raises. Accepted and stated here
  rather than discovered: replaying a corrupt series is not a better outcome.
- **`trading paper --live` is untouched.** The live feed is `RecentWindowFeed`,
  whose `adjusted` defaults to `False` (ADR-0021). Verified by driving the exact
  live object graph (5m interval, `@blue20`, IEX feed) against the real provider
  with a counting client: **20 raw bar requests, 0 adjusted requests, 0
  corporate-actions requests**. The guard cannot engage on that path.
- **The check self-heals.** When Alpaca reapplies the split, the measured factor
  becomes 4.0, nothing refuses, and the only cost left is one corporate-actions
  request per (symbol, window).
- **Cost.** One extra request per adjusted (symbol, window), memoised per adapter
  instance; a second only when a real split straddles the window. Raw and
  synthetic paths pay nothing.
- **New public surface:** `UnadjustedSplitError` and
  `AlpacaAdapter(verify_adjustments=...)` in `trading.data.alpaca_adapter`;
  `SplitEvent` and `AlpacaClient.get_splits` in `trading.data.alpaca_client`.
  Widening a `runtime_checkable` Protocol is a compatibility risk for any outside
  implementer; both in-repo implementations are updated and every test client
  subclasses `FakeAlpacaClient`, so it inherits the new method.
- **The two previously-red `TestRealBars` split assertions are not weakened.**
  They were testing ADR-0021's claim (the two price notions diverge across a
  corporate action) and were merely aimed at a broken symbol; they are retargeted
  at TSLA's 5:1 on the same 2020-08-31 session and now pass on their own terms. The
  provider's state moved to two *louder* places: a strict `xfail` in the nightly
  contract test (ADR-0046) that turns RED the day Alpaca fixes AAPL, and a live
  test asserting our own refusal that is correct in both worlds.
- **The guard depends on a provider endpoint**, so a corporate-actions outage
  silently degrades it to warn-only. The nightly contract test watches that
  endpoint for exactly this reason (ADR-0046).
- **Not fixed, and no longer claimed:** the adjusted series' *level* for AAPL is
  still wrong even in windows with no split inside them (pre-2020-08-31 windows
  are raw-scaled). Returns within such a window are unaffected — a constant scale
  factor cancels — so this is not a correctness problem for a backtest, but it is
  a reason not to compare an AAPL price level from Alpaca against one from
  another source.

## Reported upstream

Not yet filed at the time of writing; the exact report is recorded here so it can
be filed verbatim. Channel: Alpaca's community forum
(<https://forum.alpaca.markets>) or a support ticket from the paper dashboard —
the market-data adjustment pipeline is not a GitHub-issue surface, so
`alpacahq/alpaca-py` is the wrong venue (this is not an SDK bug).

> **Historical bars: AAPL's 2020-08-31 4:1 split is not applied with
> `adjustment=all`**
>
> `GET /v2/stocks/AAPL/bars?timeframe=1Day&start=2020-08-20&end=2020-09-10&adjustment=all`
> returns 2020-08-28 close **484.24** and 2020-08-31 close **125.17** — a -74.15%
> one-day move inside an *adjusted* series. `raw_close / adjusted_close` is a flat
> 1.031 on both sides of the ex-date, i.e. dividends only; `adjustment=split`
> returns bytes identical to `adjustment=raw`.
>
> This is symbol-specific, not a pipeline-wide regression: over the same account
> and the same day, TSLA 5:1 2020-08-31, NVDA 10:1 2024-06-10, GOOGL 20:1
> 2022-07-18, AMZN 20:1 2022-06-06 and CMG 50:1 2024-06-26 are all adjusted
> correctly. TSLA split on the same session as AAPL, so it is not date-scoped.
>
> Your own corporate-actions endpoint has the split:
> `GET /v1/corporate-actions?symbols=AAPL&start=2020-08-01&end=2020-09-30` returns
> `forward_splits: new_rate=4.0, old_rate=1.0, ex_date=2020-08-31`. So the bars
> service and the corporate-actions service disagree.
>
> It regressed recently: on 2026-08-04 the same 2020-08-25 bar returned 499.30 raw
> / 121.08 adjusted (correct); on 2026-08-08 and 2026-08-09 it returns 499.30 /
> 484.31.
>
> Observed on a free paper plan, alpaca-py 0.43.5, both SIP-default and IEX feeds.

## Follow-ups

- **Apply split adjustments ourselves from the corporate-actions endpoint**
  (option (b)). Now known feasible; deliberately not built in this slice. Would
  turn the refusal into a repair, and would also fix the price-level issue above.
- **Nothing re-checks a cached refusal within a run**: the split cache is
  per-adapter-instance and never expires, which is correct for a single run and
  irrelevant across runs.
- The guard is not exercised by any *synthetic* or *csv* path, because neither
  has corporate actions. That is correct, not a gap.
