# ADR-0050: An order the broker refused is not an order sitting at the venue

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

ADR-0044 closed by naming its own remaining gap, one layer out:

> Under `--divergence` … `ShadowBroker` diffs rejections around `on_bar` only — the
> same shape of miss, one layer out — so a refused order is still tracked as a
> submission there and shows up as `pending` rather than as a refusal. That is
> ADR-0038's to fix and this slice deliberately does not touch `divergence.py`.

The shape, on `main` @ `825e8dd`:

```python
def submit(self, order: Order) -> None:
    self._live.submit(order)  # may record a refusal on rejections
    ...
    self._tracked.append(_Tracked(...))  # tracked regardless
```

```python
before = len(self.rejections)
live_fills = self._live.on_bar(bars)  # only settlement is watched
new_rejections = list(self.rejections[before:])
```

A broker declines at two moments. `on_bar` covers one: an underfunded buy
(`SimulatedBroker`), or an order the venue ended `rejected` / `canceled` /
`expired` / `replaced` (ADR-0033). `submit` covers the other, and it is exactly the
pair the last two ADRs added — the duplicate-order guard (ADR-0036) and the venue's
own veto, the "potential wash trade" 403 (ADR-0041). Neither reached `_observe`, and
the order they refused was tracked anyway.

**What that costs, stated narrowly.** A refused order never fills on either side, so
it is never a paired fill: `comparable` does not move, and **the headline slippage
number is not biased**. What is wrong is the row. A tracked order the venue never
received can never settle, so it is emitted with `PENDING` on the live side forever —
which is precisely the rendering ADR-0038 reserves for *an order parked at the venue*,
the ADR-0036 case that is real, correct, and the one live divergence observation this
bench has so far. The report could not tell "the venue is holding this" from "the
venue never got this".

Measured on the duplicate-guard scenario (a `BUY AAA` parked at a shut venue, the
unmet target re-asked on the next two bars):

```
venue order ids : ('1',)      # one order exists
live rejections : 2           # two refusals recorded
Orders tracked:    3
Outcome mismatch:  3 (live-only fills 0, model-only 3)
  AAA    buy  1 — live pending | model filled 1 @ 100.0500
  AAA    buy  1 — live pending | model filled 1 @ 101.0505
  AAA    buy  1 — live pending | model filled 1 @ 99.0495
```

Three identical-looking pending rows for one order. **The timing is what makes this
worth a slice rather than a footnote:** the duplicate guard fires only while an
order sits unfilled, so the inflation is largest exactly when the pending count is
the number an operator is reading. `docs/monday-divergence-run.md` tells them to
treat a stream of refusals as the signal that execution is unhealthy and that the
fills they do have are unrepresentative — and this is the report that was quietly
restating those refusals as healthy-looking working orders.

## Decision

**`ShadowBroker.submit` diffs the live broker's rejection list around the live call,
per order, and does not track an order the broker refused.** The refusal is recorded
on a new `submit_refusals` list and counted in the summary.

```python
before: int | None = None
if self._enabled:
    try:
        before = len(self.rejections)
    except Exception as exc:
        self._disable("submit", exc)

self._live.submit(order)  # LIVE FIRST, UNGUARDED

if not self._enabled or before is None:
    return
try:
    refused = self.rejections[before:]
    if refused:
        self.submit_refusals.extend(...)
        return
    self._tracked.append(_Tracked(...))
except Exception as exc:
    self._disable("submit", exc)
```

Five choices, each deliberate.

**Per order, not per bar** — ADR-0044's reasoning verbatim, and it applies with more
force here because the wrapper's whole job is per-order attribution. The venue
refuses a *specific* order; a bar-level diff would have to guess which submission an
entry belonged to.

**Not tracked, rather than tracked-and-marked-rejected.** Recording the refusal as a
row with `live = REJECTED` was the tempting alternative: it looks symmetric with
`on_bar`'s rejections and it keeps the order visible. It is wrong for the duplicate
case, which is the common one. The counterfactual `SimulatedBroker` has no duplicate
guard, so it would *fill* every refused duplicate — the model would buy the same
position two, three, five times over, and the report would count each as a
`model_only_fill`. That is a statement about a broker guard we deliberately added,
dressed up as a statement about the cost model, and it would be loudest under
exactly the conditions ADR-0036 exists for. A refused order has no counterfactual
worth drawing, because the reason it was refused does not exist on the modelled side.

**Not `unmatched_live_rejections` either.** That list means something specific:
"the venue answered about an order we cannot attribute", i.e. *our attribution rule
is wrong*, and `render_report` says so — "could not be attributed to a tracked order;
the comparison is incomplete". A submit-time refusal is attributed exactly, to an
order we chose not to track. Filing it there would fire a warning about a bug that
is not there, and would hide the real one behind it.

**The count is printed; the row is not.** `fill_divergence.csv` is one row per
tracked order and stays that way, so the file changes only by losing rows it should
never have had. The count reaches the operator through the report block:

```
  Orders tracked:    1 (accepted by the live broker)
  Refused at submit: 2 — never reached the venue, so not tracked and not pending
                        (ADR-0036/0041; see the run's rejections)
```

The line is omitted when nothing was refused, so a healthy run's report is unchanged.
The refusals themselves were never at risk of being lost: the broker's own
`rejections` list is merged into `BacktestResult.rejections` by `Engine._finalize`,
reaches `result.json` and the summary, and since ADR-0044 reaches the per-bar status
line too. This report is a fourth place, not the only place — but it is the one the
operator is reading during the run it matters in.

**The pre-call read is guarded.** `len(self.rejections)` is now the only statement in
the wrapper that runs before `self._live.submit`, which is where ADR-0038's first
structural property lives. It sits inside `try/except`; a failure disables the shadow
and falls through to the live call, which is therefore still reached on every path.
A test injects a live broker whose `rejections` property raises and asserts the order
was placed and the shadow switched itself off.

### What this does not change

**Not the measurement.** No paired fill is added or removed, no reference price
moves, `comparable` is untouched, and the verdict text is unreachable from here. Any
claim that this changes the observed slippage would be false.

**Not the tally.** `submit_refusals` is a *copy* for the report, exactly as
`BarOutcome.broker_rejections` is a copy for the bar (ADR-0044). Nothing is appended
to the live broker's list and nothing is fed back into `BacktestResult`. Asserted two
ways: a run wrapped in a `ShadowBroker` produces a `BacktestResult` **equal** to the
unwrapped one, and `result.rejections == live.rejections` on a `buy_and_hold` run
against a parking venue, where the strategy re-asks its unfilled entry every bar
(ADR-0037) and the guard refuses every retry.

**Not the backtest.** `SimulatedBroker.submit` only queues; it rejects exclusively
inside `on_bar`, so the new diff is empty on every backtest bar and the refusal
branch is unreachable there.

**Not `--divergence`'s default.** Still off, and the CLI test asserting
`equity_curve.csv` and `result.json` are byte-identical with and without the flag is
unmodified and green.

**Not ADR-0048's journal.** A refused order produces no row at all, so it produces no
journal row — the file stays a byte prefix that under-reports and never misreports,
and there is nothing to retract. Journal I/O is untouched and still runs inside the
guarded region with the cursor advanced only on success. A test with a recording
journal asserts a refused order appends nothing and leaves the shadow enabled.

**No seam change.** `rejections` is still read through the same duck-typed property
ADR-0038 defined; widening the `Broker` protocol remains KAN-670. `cli.py`,
`engine.py`, `broker.py` and `brokers/alpaca.py` are untouched.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Leave it — the refusals are already in `result.json` and the summary | Same answer ADR-0044 gave: a `--live` session has no summary until it ends, and the divergence block is the artifact this feature exists to produce. Worse here than there, because the report does not merely omit the refusal, it *restates* it as a working order. |
| Track it with `live = REJECTED` so it stays a row | The model fills every refused duplicate, turning a guard we added into `model_only_fills` — noise that peaks exactly when ADR-0036 is doing its job. See above. |
| File them under `unmatched_live_rejections` | That bucket means "our attribution rule is broken" and prints a warning saying the comparison is incomplete. These are attributed exactly. |
| Have `Broker.submit` report its outcome (return a bool, or raise) | Changes the seam for one decorator's benefit, and raising would put a routine refusal back on the exception path ADR-0041 moved it off. The list diff needs nothing new from any broker. |
| Diff once around the whole bar's submits | Cannot say which order was refused, which is the wrapper's entire job. |
| Add a `refused` outcome and a CSV row for it | Doubles the row vocabulary and gives `divergence_rows`, the dashboard panel, and every reader a fourth outcome to learn, for an order with nothing to compare. A count answers the operator's question ("is execution healthy?") without pretending a comparison happened. |

## Consequences

- On the duplicate scenario above, `fill_divergence.csv` goes from three rows to
  one. The diff is exactly the two mislabelled rows; **the surviving row — the
  genuinely parked order — is byte-identical**, same `reference_price`, same
  modelled fill, same `outcome_diverged`. Nothing else in the file moves.
- The pending count now means "working at the venue" and nothing else, which is what
  the Monday runbook asks the operator to read it as.
- `DivergenceSummary` gains `submit_refusals: int` (defaulted, so existing
  constructions are unaffected) and `summarize` a matching keyword-only argument.
  `RESULT_SCHEMA_VERSION` is not involved — the divergence block is still not in
  `result.json` (that gap, and the dashboard panel, stay open from ADR-0038).
- 16 new fast tests, all offline on `FakeAlpacaClient` + `FakeClock`. Reverting
  `divergence.py` to `origin/main` turns exactly 11 of them red and leaves 5 green:
  the parked-order row that must not regress, the quiet report, the pass-through
  rejection list, the wrapped-equals-unwrapped equality, and the journal guard —
  which is the split the change intends, since those five held before and must hold
  after.
- **Still open.** `render_report` shows the refusal *count*, not the reasons; an
  operator wanting to know whether it was the duplicate guard or a wash-trade veto
  reads `paper_session.log` or `result.json`. And ADR-0041's narrowing stands
  unchanged: a parked entry still blocks the exit *at the venue*, which no amount of
  reporting fixes — that is KAN-678.
