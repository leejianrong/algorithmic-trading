# ADR-0028: Broker asset metadata (`get_asset`) and opt-in universe verification

- Status: Accepted
- Date: 2026-08-05
- Deciders: strategy developer (project owner)

## Context

ADR-0024 shipped curated baskets (`@blue20`) with an explicit debt: whether a
symbol is **tradable** at all, and **fractionable** enough for our fractional-share
sizing (ADR-0011), is a per-asset fact the broker owns, and the `AlpacaClient` seam
(ADR-0017/0018) exposed no way to ask. So the basket's usability was a human
judgement call — the module docstring said as much, and pointed at a deferred
`get_asset` extension.

That debt matters beyond tidiness. If a backtest universe contains a name the
broker will not trade fractionally, the backtest holds a position paper/live
cannot, and the two modes diverge for a reason that has nothing to do with the
strategy — which cuts against the single-execution-path intent of ADR-0002 and the
bench's preference for honest numbers. The failure is also quiet: the backtest
looks fine and the divergence only shows up as a rejected order in paper.

Two things need settling: how the seam exposes asset metadata, and what a universe
check does with the answer — in particular, what "the lookup failed" means, since a
network blip and a delisted stock must not be treated alike.

## Decision

**Widen the `AlpacaClient` seam by exactly one call, returning our own DTO.**

- `AssetInfo` — a frozen, slotted dataclass (`symbol`, `tradable`, `fractionable`,
  plus descriptive `exchange`, `name`, `shortable`) in
  `trading.data.alpaca_client`, alongside the existing DTOs. `__post_init__`
  validates the symbol (non-empty, no whitespace) the way the neighbouring value
  types validate theirs. No SDK type crosses the boundary (ADR-0017).
- `AlpacaClient.get_asset(symbol) -> AssetInfo` on the protocol. An unknown ticker
  raises `LookupError`; other failures (auth, rate limit, transport) propagate
  unchanged.
- `FakeAlpacaClient` answers "tradable + fractionable" for any symbol by default,
  with `set_asset(symbol, *, tradable=…, fractionable=…, …)` to script a specific
  answer and `set_asset_failure(symbol, message)` to script a lookup that raises —
  following the fake's existing `set_price` idiom, so the whole verification path
  is testable offline with no key.
- `RealAlpacaClient.get_asset` keeps the SDK import lazy and inside the method
  (ADR-0018), reads every field defensively via `getattr` (alpaca-py ships no stubs
  and field presence varies by version), strips the `AssetExchange.` enum prefix,
  and maps a 404 to `LookupError`. **Missing `tradable`/`fractionable` default to
  `False`: absent permission is not permission.**
- `AssetInfo` reports the broker's flags **verbatim**. We do not "repair" an odd
  combination (e.g. untradable-but-fractionable) into a tidier one; the broker's
  answer is the fact, and interpreting it is the validator's job.

**Verification lives in `universe.py`, stays decoupled, and reports everything.**

- `validate_universe(symbols, client) -> UniverseValidation`. A symbol is usable
  only when the broker says **tradable and fractionable**. Input order is
  preserved; duplicates collapse to their first occurrence so each symbol costs one
  broker call.
- `UniverseValidation` is frozen and carries three disjoint buckets covering every
  requested symbol: `usable`, `unusable` (the broker answered no), and
  `unverified` (the lookup failed). `dropped` is the union, `is_clean` is the
  all-good predicate, and `report_lines()` renders a human summary so a CLI stays
  thin. Each exclusion is a frozen `DroppedSymbol(symbol, reason, detail)` whose
  `reason` is validated against the `REASON_NOT_TRADABLE` /
  `REASON_NOT_FRACTIONABLE` / `REASON_UNVERIFIED` codes.
- **A failed lookup is "unverified", never "unusable".** If `get_asset` raises
  anything (`Exception`, never `BaseException`), the symbol is reported in
  `unverified` with the exception text and excluded from `usable`. So a five-second
  network hiccup can never be mistaken for a delisting, and we still refuse to
  trade a name we could not confirm. This choice is stated in the function
  docstring, not only here.
- **Nothing is ever silently filtered.** Dropping a symbol without reporting it is
  precisely the dishonesty this repo forbids; the three-bucket result makes
  "silently shrink the universe" impossible to express.
- **`universe.py` gains no runtime dependency on `trading.data`.** The client is
  typed structurally by a local `AssetSource` protocol whose `AssetInfo`
  annotation is `TYPE_CHECKING`-only, so a curated basket still works with no
  broker module imported.

**Verification is opt-in, never automatic.** It needs credentials and a network
that the offline bench deliberately does without, so no run path calls it
implicitly; the operator runs it before trusting a universe. `universe.py`'s
honesty caveat is updated accordingly — from "cannot be verified" to "here is how
to verify, and until you do, this is still a curation" — and now sits next to a
second caveat for survivorship bias (ADR-0027), which this ADR does **not** fix.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Auto-filter the universe at run start | Silently changes what a run traded, needs credentials on every run (killing offline/synthetic backtests), and hides the drop in a place nobody reads. Verification is a deliberate, reported step. |
| Return the SDK's `Asset` model directly | Breaks ADR-0017's rule that only our own types cross the seam, drags an un-stubbed import into `universe.py`'s callers, and couples us to alpaca-py's field names. |
| Have `validate_universe` raise on the first unusable symbol | Reports one problem per run and hides the rest; the operator wants the whole picture in one pass to decide whether to edit the basket. |
| Treat a lookup failure as unusable (merge the buckets) | Conflates "the broker says no" with "we could not ask", so a transient API error looks like a delisting and could quietly shrink the universe on every flaky day. The distinction is the honest part of this decision. |
| Treat a lookup failure as usable (fail open) | Would let an unverified — possibly untradable — name into the traded set, which is the exact failure this ADR exists to prevent. |
| Put the validator in `trading/data/` next to the client | `universe.py` is where the curated list and its caveat live, and the check is about the universe, not about Alpaca. A structural protocol keeps the dependency pointing the safe way. |
| Cache verification results to disk | Broker facts change (halts, delistings, fractionability changes); a stale cache would reintroduce exactly the false confidence this closes. Cheap enough to re-run when it matters. |
| Also verify liquidity / minimum volume | Not a broker flag — it needs bar data and a policy threshold, which is a separate decision. `tradable`/`fractionable` are the facts the venue asserts. |

## Consequences

- `blue20`'s tradability claim is now checkable against a real account, and the
  module docstring tells the operator the exact three lines to run. The ADR-0024
  debt is closed as *available and opt-in*, not as *automatically enforced*.
- The seam grew from five calls to six — a deliberate, reviewed widening (as
  ADR-0017 anticipated) with a fake update, so the dependency surface stays visible.
- The fast gate covers the whole verification path offline: a clean basket, a
  scripted non-fractionable name, an untradable name, and a scripted lookup failure
  reported as unverified. `RealAlpacaClient.get_asset` stays inspection-only
  (ADR-0018), verified by types rather than by the fast layer.
- The fractionability caveat is now the *smaller* of the two documented caveats on
  a curated universe; survivorship bias (ADR-0027) remains unfixed and is the
  honest headline limitation of any curated-basket backtest.
- A future universe *builder* (filter a candidate basket to
  `tradable & fractionable & liquid`) can be written on top of
  `validate_universe` without changing the seam again.

## Amendment (2026-08-04): first real run — the seam works, unchanged

`RealAlpacaClient.get_asset` was written blind and this ADR left it
"inspection-only (ADR-0018), verified by types rather than by the fast layer".
It has now been executed against a live Alpaca paper account. Both of its
guessed-at details held:

- **The 404 → `LookupError` mapping fires.** Two genuinely nonexistent tickers
  (`ZZZZNOTREAL`, `NOTATICKER9`) each produced a clean `LookupError`. The
  mechanism is as assumed: alpaca-py raises `APIError` wrapping a
  `requests.HTTPError`, and `APIError.status_code` reads `404` off the wrapped
  response, so `getattr(exc, "status_code", None) == 404` matches. This matters
  because the whole `unverified` vs `unusable` distinction depends on it — a miss
  would surface an unknown ticker as an opaque `APIError` instead. Now pinned by an
  integration test that asserts the raw SDK call really carries `status_code ==
  404`, not merely that *something* raised.
- **The `AssetExchange.` prefix strip is required.** `str(AssetExchange.NASDAQ)`
  really is `"AssetExchange.NASDAQ"` (these are `(str, Enum)` members, not
  `StrEnum`), so `exchange.split(".")[-1]` is doing real work; live `AAPL` returns
  `exchange="NASDAQ"`.

One finding worth recording against the decision text: this ADR says missing
`tradable` / `fractionable` "default to `False`: absent permission is not
permission". In alpaca-py 0.43.5 both fields are **required** on the `Asset` model,
so that default never fires in practice. It stays as a version-tolerance guard, but
it is belt-and-braces, not the normal path — and `_require_model` now rejects the
SDK's raw-dict return arm outright, because reading these flags off a dict with
`getattr` would silently default *every* asset to untradable (ADR-0033).

The verification itself ran clean on both curated baskets — see the ADR-0024
amendment for the result and for why one clean run is a snapshot, not a fact.
