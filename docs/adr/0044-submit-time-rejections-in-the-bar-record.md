# ADR-0044: A refusal belongs to the bar it happened on, at both ends of an order's life

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

ADR-0036's amendment (the duplicate-order guard) closed with two gaps recorded
against itself:

> Known cosmetic gaps, both in the shared engine's per-bar bookkeeping rather than
> the broker: a refusal reaches `BacktestResult.rejections` but not that bar's
> `BarOutcome.broker_rejections` (the engine snapshots the broker's list around
> `on_bar` only) and the refused order still appears in `BarOutcome.submitted`.

They were left alone because `engine.py` was outside that slice, and they were
called cosmetic because nothing was mis-executed and nothing was lost: `_finalize`
merges the broker's whole `rejections` list into the result, so every refusal did
reach `result.json` and the printed summary.

"Cosmetic" was the wrong word, for one reason. **A `--live` paper session has no
summary until it ends.** Its per-bar status line is the only real-time signal an
operator gets, and `cli._format_bar` has rendered `outcome.broker_rejections`
since paper mode shipped. So the field existed, the renderer existed, and the one
thing that could put a duplicate refusal into it — the engine — did not.

The shape of the miss, on `main` @ `b6399f0`:

```python
broker_rej_before = len(getattr(self._broker, "rejections", []))
fills = self._broker.on_bar(bars)  # settlement
broker_rejections = list(getattr(self._broker, "rejections", [])[broker_rej_before:])
...
self._broker.submit(checked)  # submit — unwatched
```

A broker rejects at two distinct moments, and only the first was diffed:

| moment | what rejects there | reached the bar? |
|---|---|---|
| settlement, inside `on_bar` | underfunded buy / oversell (`SimulatedBroker`); an order the venue ended `rejected`/`canceled`/`expired`/`replaced` (ADR-0033) | yes |
| submit, inside `submit` | the duplicate-order guard (ADR-0036); a venue veto — wash trade, insufficient buying power (ADR-0041) | **no** |

Which is backwards with respect to how new the two refusals are: the submit-time
pair is precisely what the last two ADRs added, and precisely what an operator has
been told to watch for. `docs/monday-divergence-run.md` sends them looking for "a
stream of rejections" in the session log during Monday's 5-minute-bar live run —
a run whose whole configuration (`--live`, an intraday interval, a venue that
parks orders) is the one that generates submit-time refusals.

The second gap compounds it. A refused order was still appended to
`BarOutcome.submitted`, so the per-bar record positively asserted that an order
the venue never received had been placed. On the duplicate-guard path that is the
difference between "we asked once and were refused four times" and "we placed five
orders" — the exact confusion ADR-0036 exists to prevent.

## Decision

**`Engine._step` diffs the broker's rejection list around `submit` as well as
around `on_bar`, per order, and only records an order as `submitted` when the
broker did not refuse it.**

```python
submit_rej_before = len(getattr(self._broker, "rejections", []))
self._broker.submit(checked)
refused = list(getattr(self._broker, "rejections", [])[submit_rej_before:])
if refused:
    broker_rejections.extend(refused)
    continue
submitted.append(checked)
```

Four things that choice fixes in place, each deliberate.

**Per order, not per bar.** A bar with one refusal and two acceptances must report
exactly that. The venue refuses a *specific* order (ADR-0041's table is five
different orders, five different codes), so a bar-level diff would have to guess
which submission the new entry belonged to. Diffing inside the loop makes the
attribution structural rather than inferred.

**Ordering: settlement first, then submit.** That is chronological within the bar —
`on_bar` runs before the submit loop by the no-look-ahead ordering (ADR-0001) — so
the list reads in the order the events happened, and the pre-existing on_bar
snapshot is untouched.

**`checked`, never `order`.** A guardrail-clamped order is submitted at the clamped
quantity, so that is what `submitted` records and that is the object the broker
attaches its reason to. The clamp itself is already reported separately in
`BarOutcome.clamps`, with both the original and the clamped order.

**Reporting only — the tally does not move.** `_finalize` already merges the
broker's entire `rejections` list into `BacktestResult.rejections`, so
`broker_rejections` is a *copy* for the bar and is never fed back into
`state.rejections`. Appending in both places would double-count every refusal in
`result.json` and the summary. A test asserts `result.rejections ==
broker.rejections` on a run with three refusals.

**The duck typing stays.** `rejections` is still read through
`getattr(self._broker, "rejections", [])`: the `Broker` protocol does not require
it, and widening the protocol is a separate decision (KAN-670) that touches
`interfaces.py` and every broker. The cost is that `mypy --strict` cannot see this
field — which is how ADR-0036's `(order_id, reason)` shape bug survived to a live
session — so the shape is pinned by tests instead, including an existing
type-equality check against `SimulatedBroker`.

### What this does not change

**Nothing about what is traded.** No order flow, sizing, guardrail, broker, or feed
behaviour moves. The engine reads a list the broker already maintains and writes
two fields it already had. `BarOutcome` gains no field, `result.json` gains no key,
`RESULT_SCHEMA_VERSION` stays **1**, and `cli.py` is not touched at all — the
status line lights up because the renderer was always there.

**The backtest is untouched, proved rather than argued.** `SimulatedBroker.submit`
only appends to a queue; it rejects exclusively inside `on_bar`, so the new diff is
empty on every backtest bar. Evidence: a 3-year, 20-symbol synthetic backtest run
before and after the change produced byte-identical artifacts —

```
2389eb0fcd7f58504ee065fedf934ce55a7ed41fb122257d8b3344cbd853d13d  equity_curve.csv
aeb91735c3c4e04de0c8ac0f105e9c0e0fc3af5199883ab1b6311ae93d1e1725  result.json
```

with stdout differing only in the two lines naming the output paths. A fast test
pins the SHA-256 over the canonical `result_to_dict` documents of two runs — one
under the default guardrails (fills, a clamp, capped-out rejections) and one
unconstrained with an unfundable order, which is the only way to reach
`SimulatedBroker`'s *own* rejection path while the caps veto first — and a sibling
test asserts the fixture really produces both, because a golden over a run that
never rejects would prove nothing here. The golden was checked against a reverted
`engine.py`, not merely recorded after the fact.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Leave it; the summary and `result.json` already carry every refusal | Both are end-of-run artifacts. A `--live` session ends by Ctrl-C hours later, and the operator's decision to intervene is made from the per-bar line. The information existing somewhere is not the same as reaching the person watching. |
| Have `AlpacaBroker.submit` return a bool (or raise) so the engine learns the outcome directly | Changes the `Broker` seam for one broker's benefit, and a raise would put a routine, expected refusal on the exception path — exactly what ADR-0041 moved it *off*. The list diff needs nothing new from any broker. |
| Diff once around the whole submit loop instead of per order | Cheaper by a few `len()` calls and wrong: it cannot say which of the bar's orders was refused, so `submitted` would have to be all-or-nothing. The per-order cost is one list length per submitted order. |
| Add a new `BarOutcome.refused` field instead of reusing `broker_rejections` | Two fields for one concept, and `cli._format_bar`, the session log, and any future dashboard panel would all need teaching about the second one. A refusal at submit and a rejection at settlement are both "the broker declined this order"; they differ only in when. |
| Also append the refusal to `state.rejections` for symmetry with the guardrail path | Double-counts. The guardrail path appends to `state.rejections` *because* nothing else records it; the broker's own list is merged wholesale at `_finalize`. |
| Widen the `Broker` protocol with `rejections` first, then fix this | KAN-670, and it would make this two-point slice a protocol change touching every broker and the divergence decorator. The `getattr` is pre-existing and unchanged here. |

## Consequences

- A duplicate-order refusal and a venue veto both appear on the paper session's
  per-bar line, as `REJECT BUY AAPL (not submitted: order … is still working at
  the venue)`, the instant they happen — in stdout and in `paper_session.log`.
  This is the state Monday's live run needed and did not have.
- `BarOutcome.submitted` now means what it says: orders the broker accepted, at the
  quantity it was handed.
- Settlement rejections keep reaching their bar exactly as before; a test pins the
  pre-existing path so the new diff cannot quietly replace it.
- 12 new fast tests, all offline (`FakeAlpacaClient` + `FakeClock`), driving the
  real `Engine._step` through a real `PaperSession` rather than calling the private
  step directly — including a bar with one refusal and two acceptances, a
  no-double-count assertion, the `cli._format_bar` round trip, and the backtest
  golden. Reverting `engine.py` to `origin/main` turns exactly 7 of them red and
  leaves the other 5 (the backtest golden, the settlement path, the `BarOutcome`
  shape) green, which is the split the change intends.
- **Still open, and worth naming.** Under `--divergence` the engine's diff reads
  `ShadowBroker.rejections`, which is the live broker's list passed through a
  property (ADR-0038), so a submit-time refusal reaches the bar record the same
  way. What it does *not* do is reach the divergence report: `ShadowBroker` diffs
  rejections around `on_bar` only — the same shape of miss, one layer out — so a
  refused order is still tracked as a submission there and shows up as `pending`
  rather than as a refusal. That is ADR-0038's to fix and this slice deliberately
  does not touch `divergence.py`; the existing divergence tests are green
  unmodified. Unrelated and equally open: `PaperSession` still does not surface
  `feed.absent` / `persistently_absent` in its summary or `result.json`
  (ADR-0035).
