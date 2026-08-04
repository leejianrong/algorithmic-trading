# ADR-0021: Raw-vs-adjusted price handling as an explicit per-mode policy

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

ADR-0008 fixed backtests on split/dividend-adjusted (total-return) prices so
reported returns are honest and corporate actions create no phantom moves. It
also foresaw that the live path would instead need *actual quotes*: an adjusted
"price" is not the dollar amount tradable on a given day. That day has arrived.

The Alpaca paper broker (ADR-0020) reconciles cash and positions wholesale from
the real account — it marks the book in RAW dollars the venue actually quotes and
fills at. If the strategy meanwhile decides on adjusted prices, its signals and
its mark-to-market drift from the account the broker reports: the same symbol
carries two different prices on the same bar. A backtest, by contrast, must stay
on adjusted prices to remain an honest multi-year measurement.

Two defects made the wrong thing happen. `AlpacaAdapter` fixed `adjusted` at
construction (default `True`) and *ignored* the per-call flag, and the paper feed
(`RecentWindowFeed`) had no notion of raw at all — so `paper --source alpaca`
would feed adjusted prices to a strategy trading against a raw account.

## Decision

The price notion is a **per-mode policy carried by the feed**, not by the
strategy or the engine — the one execution path (ADR-0002) stays intact; only the
feed differs.

- **Backtest feed → adjusted.** `Engine.run` fetches with `adjusted=True`
  (ADR-0008), unchanged.
- **Paper/live feed → raw.** `RecentWindowFeed` gains an `adjusted` constructor
  keyword defaulting to `False`, threaded into every `get_bars` call, so the paper
  loop decides and marks on the same RAW quotes the broker reconciles.
- **`AlpacaAdapter` honors the per-call flag.** `get_bars(..., adjusted=...)`
  now controls the fetch (delegating to the seam, which already serves raw via
  `Adjustment.RAW`); the constructor param only supplies the default when a caller
  omits the keyword.
- **Adjusted-only sources are backtest-only.** `YFinanceAdapter` and `CsvAdapter`
  keep raising on `adjusted=False`, now with a message that steers the user to
  `--source alpaca` for raw live quotes or `--source synthetic` for an offline
  demo. Live paper on those sources errors loudly and deliberately — honest, not a
  regression.
- **Synthetic: raw == adjusted.** Synthetic GBM has no corporate actions, so the
  flag does not change its numbers; the identical series drives both feeds.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Carry both raw and adjusted on every `Bar` | Doubles the data model and every adapter/cache path for a distinction only two feeds care about; the mode already knows which it wants. |
| Keep adjusted everywhere, including live | Dishonest live seam: the strategy would decide and mark in adjusted dollars while the broker's account is in raw ones, so signals and P&L silently diverge from reality. |
| Let the strategy or engine choose per symbol | Forks decision logic by price notion and invites look-ahead-style bugs; the feed is the single, mode-level place the choice belongs. |

## Consequences

- Paper marks and reconciles a raw account consistently: strategy signals,
  mark-to-market, and the broker's reconciled book all speak the same RAW dollars.
- `--source yfinance` / `--source csv` are backtest sources; requesting raw from
  them fails fast with guidance rather than quietly returning adjusted prices.
- Synthetic remains the offline workhorse for both modes — raw == adjusted, so
  `paper --source synthetic` and `backtest --source synthetic` see one series.
- Not byte-identical across modes by design (ADR-0020): paper on adjusted-vs-raw
  data can differ from a backtest of the "same" symbol, which is the point — each
  mode uses the price notion honest for it.
- Forecloses nothing: storing raw alongside adjusted stays an additive change if a
  future need (e.g. an adjusted mark on a live account) ever wants both at once.
- Now true: a guard test drives a paper session (raw feed) and a backtest
  (adjusted feed) over the same window on an adapter whose raw and adjusted closes
  differ, and asserts the paper path decides/marks on raw while the backtest uses
  adjusted.
