# ADR-0041: The venue refuses orders too — and a refusal is a rejection, not a crash

- Status: Accepted
- Date: 2026-08-08
- Deciders: strategy developer (project owner)

## Context

ADR-0036's amendment (PR #44) shipped the duplicate-order guard with a live test
attached and a note admitting the test had never run: "the worktree this landed
from had no credentials, so unlike the rest of ADR-0036 the live half of this
amendment is written but unwitnessed."

It was run on 2026-08-08 with the venue shut (Saturday; next open Mon 09:30 ET),
against the paper account. Most of it held. Two things did not, and the second one
was a session-killing bug.

**1. The venue is no backstop against duplicates — confirmed, not assumed.** Two
identical `BUY 0.01 AAPL` orders submitted straight at the client both came back
`accepted`, with two distinct ids, both working at the account:

```
BUY #1: AlpacaOrder(id='c3c85ca5-…', symbol='AAPL', qty=0.01, side=Side.BUY,
                    status='accepted', filled_qty=0.0, filled_avg_price=None)
BUY #2: AlpacaOrder(id='a182da86-…', symbol='AAPL', qty=0.01, side=Side.BUY,
                    status='accepted', filled_qty=0.0, filled_avg_price=None)
```

ADR-0036 argued the guard had to exist because nothing else would stop the stack.
That was the right call for the right reason, and it is now checked rather than
reasoned.

**2. The venue refuses the opposite side while an order is working, and the
refusal killed the run.** The live test's last assertion — "an exit is never
blocked, even with that buy still working" — did not merely fail, it **raised**:

```
alpaca.common.exceptions.APIError: {"code":40310000,
  "existing_order_id":"a182da86-7d00-4ee9-b359-02086cfe676e",
  "message":"potential wash trade detected. use complex orders",
  "reject_reason":"opposite side market/stop order exists"}
```

Two separate problems live in that one line.

The **factual** one: ADR-0036's amendment says "a working BUY can never block a
SELL". That is true of *this bench* — the guard is keyed on symbol **and** side,
so an exit is never even compared against a working entry — but it is not true of
the **system**, because Alpaca has a wash-trade rule of its own. The exact
situation the guard was built for (an entry parked over a weekend, the account
reading flat) is also the situation in which an exit cannot reach the book. The
amendment's promise was about our layer and read like a promise about the outcome.

The **structural** one, and the reason this ADR exists: nothing caught the
`APIError`. It travelled out of `RealAlpacaClient.submit_order`, out of
`AlpacaBroker.submit`, through `Engine._step`, and out of `PaperSession.run` —
taking the equity CSV, `result.json`, and the printed summary with it. That is the
same loss ADR-0033 fixed for Ctrl-C, arriving through a different door, and it is
routine rather than exotic: the refusal *only* happens while an order is parked,
which is the normal state of every overnight and weekend session. ADR-0020 and
ADR-0033 both promised "one bad order never aborts a run" — and delivered it for
every status the venue assigns to an order it *accepted*, while leaving the
submit-time refusal wide open.

It also broke ADR-0017's seam rule outright. `RealAlpacaClient` exists so "no SDK
type escapes this class", and an `alpaca.common.exceptions.APIError` was escaping
it on the most-used call in the module.

## Decision

**A venue refusal at submit time is an `OrderRejectedError`, and `AlpacaBroker`
records it on `rejections`.** Two halves.

`RealAlpacaClient.submit_order` wraps its SDK call and classifies the failure
through `_classify_order_error`. A refusal becomes our own
`OrderRejectedError` — a plain `RuntimeError` subclass in
`trading.data.alpaca_client`, carrying the venue's body **verbatim** — so nothing
downstream imports an SDK type, and the reason string an operator reads is the
one Alpaca wrote.

`AlpacaBroker.submit` catches exactly that type and appends `(order, reason)` to
`rejections`, the same `(Order, reason)` shape ADR-0036 settled on and
`SimulatedBroker` uses, so it reaches `result.json` and the summary through
machinery that already exists. No id came back, so nothing goes into `_pending`,
nothing is polled, and — deliberately — the next bar is free to try the same
intent again. A refusal is a fact about one order, not a latch.

**The classifier turns on Alpaca's own error taxonomy, read off the wire.** The
discriminator is *not* the HTTP status alone and *not* a substring of the message.
Every refusal of a specific order carries an eight-digit numeric `code` in the
body; a request we were not allowed to make does not. Recorded on 2026-08-08
against the paper account:

| what we did | HTTP | body |
|---|---|---|
| SELL while a BUY works | 403 | `{"code":40310000,…"potential wash trade detected. use complex orders","reject_reason":"opposite side market/stop order exists"}` |
| BUY 100000 shares | 403 | `{"buying_power":"400000.24","code":40310000,"cost_basis":"31319000.3","message":"insufficient buying power"}` |
| BUY an unknown ticker | 422 | `{"code":42210000,"message":"asset \"ZZZZNOTREAL\" not found"}` |
| SELL from a flat book | 422 | `{"code":42210000,"message":"fractional orders cannot be sold short"}` |
| bad credentials | 401 | `{"message": "unauthorized."}` — **no code** |

So the rule is: a 4xx that is not 401/429, carrying a numeric error code, is a
refusal; everything else propagates. This is the same distinction ADR-0028 draws
for asset lookups — "the broker said no" is not "we could not ask" — and it is
deliberately biased toward propagating. Swallowing an outage would leave a session
running forever, recording rejections and never trading, which is worse than a
loud stop.

`APIError.code` is a property that **raises** `KeyError` when the body has no
code (alpaca-py implements it as `json.loads(self._error)["code"]`), so
`_order_error_code` cannot be a `getattr` with a default. That is pinned by a test
against the real SDK class rather than trusted.

**`FakeAlpacaClient` learns to refuse.** `set_submit_refusal(symbol, message,
side=…)` and `set_submit_failure(symbol, error)` script both arms, side-scoped
because the venue's is: a parked BUY makes the *SELL* the wash trade while the BUY
itself is still accepted. The fake accepting every order is precisely why the live
test asserted an exit the venue refuses; a fake that cannot say no cannot catch
this class of bug.

**The live test now asserts what the venue does.** The exit assertion is inverted
and split in two: our guard did *not* refuse the SELL (checked by asserting the
recorded reason is **not** the duplicate guard's), and the venue did — with the
parked BUY untouched and the session alive. The venue's own duplicate tolerance
gets its own test, so the day Alpaca starts deduplicating for us, we find out.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Let the `APIError` propagate and tell operators to catch it | It reaches them as a dead session with no artifacts. ADR-0033 already decided this exact question for Ctrl-C: a stop is not a failure, and the run's information is the point (ADR-0035). |
| Catch every exception in `AlpacaBroker.submit` | Turns an expired key or a dead socket into a silent stream of rejections. A run that cannot trade must stop, not narrate. |
| Classify by matching the message text ("wash trade", "buying power") | Enumerates the refusals we happened to see and mislabels the next one as a transport failure. ADR-0040's lesson: classify by structure, not by string. The numeric `code` *is* the structure, and Alpaca publishes it. |
| Classify on HTTP status alone (any 4xx is a refusal) | 401 and 429 are 4xx and are not decisions about the order. The code check is what keeps them out without a hand-maintained status list. |
| Cancel the parked entry and resubmit the exit | ADR-0036 already rejected cancel-then-resubmit for duplicates, and the reason is stronger here: cancellation is asynchronous, so between the request and `canceled` the venue may fill the entry — and now an exit is racing a fill it was trying to escape. It also decides, inside the broker, that the exit matters more than the entry, which is a strategy-level judgement. |
| Convert the wash-trade refusal into a limit order ("use complex orders", as Alpaca suggests) | The bench places market orders by construction (ADR-0020). Introducing an order type on one error path forks the execution model for the case that is hardest to test. |
| Retry the refused order later in the same bar | Nothing changes within a bar — the entry is still parked. The next bar's re-emission already is the retry, and it costs nothing. |
| Widen `Broker` with a `rejections` member so this is protocol-visible | Same follow-up ADR-0036 recorded and did not take: `Engine` reads it through `getattr` with a default, and widening the engine-facing protocol touches files this slice does not own. |

## Consequences

- A live paper session survives a refused order. Every refusal is one row in
  `result.json` and the summary, carrying Alpaca's exact words and error code,
  next to the venue rejections ADR-0033 records and the duplicate refusals
  ADR-0036 records — one field, three kinds of "this order did not happen".
- **ADR-0036's exit promise is narrowed to what it actually covers.** The guard
  never blocks an exit; the venue can, and does, while an entry is parked. That is
  amended into ADR-0036 rather than left to be rediscovered.
- **A parked entry makes the symbol un-exitable until it settles.** Nothing here
  fixes that, and it is worth stating plainly: with the venue shut and a long-or-flat
  book, an entry queued for the next open blocks the exit until the entry itself
  fills or is cancelled — at which point the position exists and the exit goes
  through normally. It matters least where it sounds worst: while the venue is
  closed nothing can be sold anyway. It is a real constraint on an *intraday*
  session, where an entry can sit working for minutes during the day, and it is a
  further argument for KAN-678 (a sizer that nets in-flight quantity would stop
  emitting the contradictory pair in the first place).
- 8 new fast tests on the classifier and the fake's refusal, 8 on the broker
  (recorded not raised, nothing pending, retry next bar, transport still
  propagates, the wash-trade shape offline, survival through `result_to_dict`, and
  a full `Engine` run that keeps its bars), and 5 new live tests. The live ones are
  double-gated on credentials **and** the SDK and skip when the market is open.
- The account was left flat and checked, not assumed: no positions, no working
  orders, $100,000.06 cash.

### Unrelated, found in the same run, and NOT fixed here

**Alpaca stopped applying split adjustments.** `TestRealBars`'s two split tests
were green on 2026-08-04 (ADR-0018's amendment records 499.30 raw vs **121.08**
adjusted for AAPL on 2020-08-25) and are red now: the same call returns 499.30 raw
vs **484.31** adjusted, a ratio of 1.031 — dividends only. A window spanning the
2020-08-31 4:1 split shows the *adjusted* series still carrying a bare price cliff:

```
  2020-08-28  adj=484.2400  raw=499.2300
  2020-08-31  adj=125.1700  raw=129.0400
```

That is ADR-0008's phantom-split hazard reaching a backtest through `--source
alpaca` while the API still answers `adjustment=all` without complaint — the
ADR-0040 lesson again, that a provider can regress behind a green-looking
contract. The two tests are **left failing on purpose**: weakening them would hide
an honesty regression, and they skip in CI (no credentials), so they gate nothing.
Fixing it needs its own slice — it touches `alpaca_adapter.py` and probably wants
a committed fixture and a cross-provider check, neither of which belongs in a
broker-refusal change.
