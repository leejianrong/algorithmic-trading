# ADR-0017: A thin Alpaca client seam with our own DTOs and a fake

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

The next milestone wires Alpaca in behind the existing seams: an Alpaca-backed
`DataAdapter` (daily bars) and an Alpaca paper `Broker` (`submit` / `on_bar`).
Both talk to the same vendor over two `alpaca-py` sub-clients (a historical data
client and a trading client) with the vendor's own request/response models
(`Order`, `TradeAccount`, `Position`, `Bar`, and a fistful of enums).

If the adapter and broker each reach into `alpaca-py` directly, the SDK's types
leak across two lanes at once: every module that touches a fill would import a
vendor model, the fast test layer would need the SDK installed (it is not, and it
ships no type stubs), and swapping vendors later would mean surgery in several
files. We already keep the network optional elsewhere (yfinance behind an
injectable fetcher, matplotlib behind a lazy import); Alpaca should be no
different. The question this slice settles is the *shape of the boundary* the
adapter and broker share.

## Decision

**One narrow `AlpacaClient` protocol, returning only our own types.** A single
`@runtime_checkable` protocol in `trading.data.alpaca_client` declares exactly the
five calls the adapter and broker need and nothing more:

- `get_daily_bars(symbol, start, end, *, adjusted) -> list[Bar]` (adapter; the
  `adjusted` flag honours ADR-0008)
- `submit_order(symbol, qty, side) -> AlpacaOrder` (broker; market order,
  fractional `qty` per ADR-0011, long-or-flat per the no-shorting invariant)
- `get_order(order_id) -> AlpacaOrder` (broker; submit-then-poll)
- `get_account() -> AccountSnapshot`
- `list_positions() -> list[PositionSnapshot]`

**Our own frozen DTOs, not SDK models, cross the boundary.** `AlpacaOrder`
(id, symbol, qty, side, status, filled_qty, filled_avg_price), `AccountSnapshot`
(cash, equity), and `PositionSnapshot` (symbol, qty, avg_price) are small frozen
dataclasses that reuse our `Side` and `Bar`. `status` is a plain string so a real
Alpaca lifecycle value round-trips unchanged. Nothing downstream imports
`alpaca-py`.

**A fake is the workhorse; the real wrapper is thin.** `FakeAlpacaClient` is
deterministic and in-memory (no wall clock, no RNG, monotonic order ids). It is
built from a `dict[str, list[Bar]]` plus starting cash, fills immediately at a
set price or the last bar close by default, and offers an `auto_fill=False`
pending mode (`submit_order` leaves the order `new`; `fill_order` settles it) so a
later broker lane can test submit-then-poll and timeout paths. Its accounting
mirrors `Portfolio.apply_fill` (blended average on buys, unchanged basis on
partial sells, no implicit shorting). `RealAlpacaClient` wraps the two SDK
sub-clients behind a lazy guarded import and converts every response into our
DTOs/`Bar` (see ADR-0018 for credentials and the optional dependency).

## Alternatives considered

| Option | Why not |
|--------|---------|
| Adapter and broker each import `alpaca-py` directly | Leaks vendor types across two lanes, forces the SDK (and its missing stubs) into the fast gate, and scatters the vendor coupling so a swap touches many files. |
| Reuse the SDK's own model classes as our DTOs | Ties our value types to a third party's release cadence and field names, and drags an un-stubbed import into modules that must stay offline. |
| One fat client mirroring the whole Alpaca API | Most of it is unused; a five-method seam is easier to fake, test, and reason about, and documents precisely what we depend on. |
| Skip the fake, test only against a recorded cassette | Needs the SDK and captured fixtures to run; an in-memory fake keeps the fast layer infra-free and lets the broker lane script pending/timeout states directly. |
| Fold Alpaca into the existing `DataAdapter`/`Broker` fakes | Those seams are engine-facing; the client seam is vendor-facing. Keeping them separate lets one `AlpacaClient` serve *both* the data and broker lanes without entangling them. |

## Consequences

- The adapter and broker lanes build against `AlpacaClient` and `FakeAlpacaClient`
  with no network and no SDK installed; both depend on the same seam, so the fake
  is shared rather than duplicated.
- The vendor coupling is quarantined to `RealAlpacaClient`; a different broker
  later implements the same five methods, exactly as `SimulatedBroker` and an
  Alpaca broker both satisfy `Broker` (ADR-0004, ADR-0002).
- The protocol is only as honest as the fake: `FakeAlpacaClient`'s immediate,
  frictionless fills do not model real slippage, partial fills, or queue position,
  so paper results stay labelled as estimates until the real client runs.
- Adding a needed call (e.g. cancel-order) is a deliberate widening of the seam
  plus a fake update, which keeps the dependency surface visible and reviewed.
