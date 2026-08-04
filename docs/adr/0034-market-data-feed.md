# ADR-0034: The market-data feed is a per-mode choice, and a plan refusal is its own error

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

`trading paper --broker alpaca --live --source alpaca` failed on its very first
poll, before a single bar reached the strategy:

```
APIError: {"message":"subscription does not permit querying recent SIP data"}
```

The cause is structural, not a misconfiguration. `RecentWindowFeed.poll` asks the
adapter for bars over `[far_past, now]` — it *must* ask up to `now`, since its whole
job is to find the bar that just completed. Alpaca serves bars from the consolidated
**SIP** tape by default, and a free data plan does not include SIP data inside the
last ~15 minutes. So the live feed's defining request is precisely the request the
plan refuses. Measured against this account:

| Request | Result |
|---------|--------|
| 1m bars, `end=now`, default feed (SIP) | **HTTP 403** "subscription does not permit querying recent SIP data" |
| 1m bars, `end=now`, `feed=iex` | **OK** — 179 bars, latest one minute old |
| 1m bars, `end=now-16m`, default feed | OK — SIP is fine once it is not "recent" |
| daily bars, 2020 window, default feed | OK — historical SIP was never the problem |

Two things needed deciding: which tape each mode asks for, and how a plan refusal
should surface. The refusal arrived as a 40-line SDK traceback ending in a JSON
blob, from a code path (`RecentWindowFeed` → `AlpacaAdapter` → SDK) that gives no
hint about what to do next.

Note the shape of this problem: it is the same shape as ADR-0021. There, the *price
notion* (adjusted vs raw) turned out to be a per-mode policy rather than a global
one — backtests want total-return prices, paper/live wants the actual quotes the
account is marked in. Here the *tape* is per-mode for a directly analogous reason.

## Decision

**The feed is a construction property of the client, defaulting to the SDK's tape.**
`RealAlpacaClient(feed=...)` selects it; `AlpacaAdapter(feed=...)` forwards it to a
client it builds itself. `None` — the default — omits the `feed` keyword from the
request entirely, so a default-constructed client's requests are byte-identical to
before this parameter existed and **historical/backtest behaviour is unchanged**.

This mirrors the interval (ADR-0022): one client serves one tape, and neither the
`DataAdapter` protocol nor `Engine._step` learns that a tape exists. Passing `feed`
alongside an injected `client` is an error rather than a silent no-op — an injected
client carries its own feed, and quietly ignoring the argument is how a live run
ends up on a tape the operator did not pick.

**Live Alpaca paper defaults to IEX; everything else keeps SIP.** `--data-feed`
exposes the choice on `trading paper`, and when it is omitted the CLI selects `iex`
if and only if `--live` and `--source alpaca`. Backtests, `--once` replays, and
every non-Alpaca source are untouched.

Why not IEX everywhere: IEX is a single venue at a few percent of consolidated
volume, so its historical bars are thinner and less representative than SIP's.
Making a backtest quietly worse to fix a live-feed problem is the wrong trade — and
it would silently change every existing result. Why not delay `end` by 15 minutes
instead: that makes every live decision knowingly stale, on every plan, forever, to
work around one plan's limits; IEX gives real-time bars now and a paid plan needs no
workaround at all. `--data-feed sip` remains available for an account whose plan
covers recent SIP.

**A data-plan refusal is a distinct error type.** `DataSubscriptionError` is raised
when a bar fetch fails with `"subscription does not permit"` in the body, carrying
the symbol, the feed used, the venue's message, and the flag that fixes it. Every
other failure — auth, rate limit, transport — propagates **unchanged**.

That split is the same principle ADR-0028 chose for `get_asset`: "the broker said
no" and "we could not ask" are different facts and must not be conflated. A plan
refusal is not a transient failure to retry and not a bug to fix in code; it is a
well-formed, authenticated request for data the account does not have, and the only
useful response is to tell the operator which flag to pass. Detection is by message
substring because the SDK exposes no error code for it; the classifier is
deliberately narrow, so an unrecognised message stays exactly what it was.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Default every request to IEX | Silently degrades every historical backtest to one venue's thin tape, and changes existing results for a live-only problem. |
| Push the live feed's `end` back ~15 minutes | Makes every live decision permanently stale on every plan, to accommodate one plan. IEX is real-time and costs nothing. |
| Make the feed a `get_bars` argument | Breaks the ADR-0022 invariant that cadence-and-tape are adapter properties and the protocol stays daily-shaped; every caller would have to care. |
| Read the feed from an env var | Invisible in the run record. A run's tape belongs on the command line next to `--source`, where the report can see it. |
| Detect the plan's capabilities at startup and pick a feed | An extra probe request, a new failure mode, and a guess that changes silently with the account. An explicit default the operator can override is more predictable. |
| Let the `APIError` propagate as-is | A 40-line traceback ending in a JSON blob, from three layers down, with no indication that a one-flag fix exists. |
| Retry on the refusal | It is not transient. Retrying a plan limit just fails slower. |
| Match on HTTP 403 instead of the message | 403 also covers a revoked or wrong-endpoint key, which needs a completely different response. The message is what actually identifies a plan limit. |

## Consequences

- `paper --broker alpaca --live` works. Verified end to end on 2026-08-04: 377
  one-minute bars, a real clamped order filling at the next bar, equity reconciled
  from the account throughout.
- Backtests are bit-for-bit unaffected: with `feed=None` the request is unchanged,
  and `--data-feed` is rejected for non-Alpaca sources rather than ignored.
- An operator on a free plan who points a backtest at recent dates now gets one
  actionable sentence instead of a traceback. An operator on a paid plan can pass
  `--data-feed sip` and never think about this.
- The IEX default is an honesty trade-off worth stating plainly: **live paper
  decisions are made on one venue's bars while a backtest of the same strategy uses
  the consolidated tape.** IEX prints can differ from the consolidated print,
  especially for thin names, so this is one more reason paper results are not
  byte-comparable to a backtest (ADR-0020 already accepted that the account, not our
  simulation, is authoritative). A paid plan closes the gap with `--data-feed sip`.
- Not addressed here: the live feed still requests `[datetime.min, now]` on every
  poll and relies on the API tolerating an absurd `start`. It works and is cheap
  enough at the API's paging limit, but a bounded lookback window would be tidier —
  a separate change, since it touches `RecentWindowFeed` for every source.
