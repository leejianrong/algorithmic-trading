# The crypto divergence run

> The equity version of this page is [`monday-divergence-run.md`](monday-divergence-run.md),
> and most of it still applies — the preflight, the detached launch, the SIGTERM
> stop, how to read the result. **This page is only what is different on a market
> that never closes**, plus the one measurement the divergence report structurally
> cannot make.
>
> The 2026-08-16 run has happened. The answer is in
> [ADR-0061](adr/0061-measured-crypto-fill-cost.md).

## What is different, in one list

| | US equities | Alpaca crypto |
|---|---|---|
| When you can start | 09:30–16:00 ET, and **not >50 min early** | any time |
| What ends the session | the close, then ADR-0049's hour of silence | **nothing** — you stop it by hand |
| Order time-in-force | `DAY` — an unfilled order expires at the close | **`GTC` — an unfilled order never expires** (ADR-0058 §3) |
| `--data-feed` | `iex`, and it matters (ADR-0034) | **refused at construction** — `CryptoBarsRequest` has no feed field |
| Cost model | 5 bps slippage, no commission | 5 bps slippage **+ a taker fee** (ADR-0060) |
| What the report measures | all of the modelled cost | **the slippage only** — the fee is taken in quantity |
| Bar coverage | one bar per interval, always | **holes** — see "The tape has holes" |
| A dry run | rehearses against a shut venue and trades nothing | **there is no shut venue: a dry run trades** |

## The command

```bash
make paper-live \
  PAPER_MARKET=crypto \
  PAPER_SYMBOLS=@crypto10 \
  PAPER_INTERVAL=5m \
  PAPER_DATE=$(date -u +%F) \
  PAPER_EXTRA_ARGS="--max-position 0.01"
```

Which expands to `trading paper --strategy sma_crossover --symbols @crypto10
--interval 5m --market crypto --source alpaca --broker alpaca --live --divergence`,
detached, into a timestamped `--out`. `PAPER_MARKET=crypto` is what makes the
launcher drop `--data-feed`; passing one on this venue is refused at client
construction, so before that variable existed the crypto run could not be launched
from here at all.

**`--max-position 0.01` is the sizing knob, and it is deliberate.** `sma_crossover`
targets 95% of equity split across whatever symbols have a bar, which on a $100k
account is ~$9,500 an order. The 1% cap clamps every entry to about $1,000
instead — still two orders of magnitude above the venue's $10 notional floor
(ADR-0058 §4), so these are real orders and not the $4.66 fill generator the equity
runbook rejects, and small enough that 40 round trips do not move the account into
the next **fee tier**. Every entry therefore reports a `CLAMP`, which is the
guardrail working as instructed and not a warning. Exits are never clamped
(ADR-0011/0013).

## Nothing stops this session but you

An equity session ends because the venue does. A 24/7 session has no close, so
ADR-0049's silence tolerance will never fire on a healthy tape, and the run
continues until stopped:

```bash
make paper-stop        # SIGTERM to the session, so ADR-0043 finalizes. Never kill -9.
```

**Signal the python child, not the `uv run` wrapper.** `make paper-stop` does this
for you. Stopping with `timeout`, or `kill`ing the wrapper directly, has twice been
mistaken for an ADR-0043 regression: the wrapper dies, `paper_session.log` /
`paper_state.json` / `fill_divergence.csv` survive because they are written as the
run goes, and `equity_curve.csv` and `result.json` never appear.

Stop it when you have the sample, not on a clock. `fill_divergence.csv` is readable
mid-session (ADR-0048), so the honest stop condition is a count:

```bash
awk -F, 'NR>1 && $7=="filled" && $13=="filled"' <out>/fill_divergence.csv | wc -l
```

`MIN_PAIRED_FILLS` is 30. Stop somewhere above it, not at it — the last bar can
leave an order working.

## There is no free rehearsal

`make paper-dryrun` exists because an equity dry run against a shut venue primes
warmup, trades nothing, and exits. **On crypto the venue is never shut**, so the
same command places real orders. Either accept that (it is a paper account, and
~$1,000 orders under the cap above are cheap), or rehearse with `--broker
simulated` against real crypto data, which exercises the feed, the warmup and the
session loop while submitting nothing.

What a launch is checked on is the same as ever: `Warmup: primed N completed
bar(s)` with N well above zero, within a minute of launch. If the client refused
the configuration, the process is already gone and `console.log` says why.

## The tape has holes, and they are in the measurement

Alpaca publishes a crypto bar only for an interval that **traded on Alpaca**, and
its crypto volume is its own rather than the market's. Measured over 2026-08-15,
5m bars out of a possible 288:

| pair | bars | | pair | bars |
|---|---|---|---|---|
| LINK/USD | 289 | | LTC/USD | 210 |
| BTC/USD | 284 | | SOL/USD | 168 |
| UNI/USD | 281 | | DOGE/USD | 157 |
| AVAX/USD | 256 | | ETH/USD | **137** |
| AAVE/USD | 242 | | BCH/USD | 226 |

That `ETH/USD` row is not a typo, and at `--interval 1m` it falls to **12.8%**.
It matters because ADR-0038's reference price is the open of the first bar the feed
serves for the symbol *after* submission: on a pair that skips intervals, that bar
can be many minutes late, and the "slippage" quietly absorbs however far the price
drifted in between. `fill_divergence.csv` carries `reference_lag_seconds` for
exactly this. **Read the per-pair slippage against the per-pair lag before
attributing a spread to execution.**

This is the main argument against running crypto at `1m`, which is otherwise
tempting because it produces orders about 2.6x faster.

## The fee is a second measurement, and the report cannot make it

ADR-0060 is explicit: the divergence report compares prices, the venue's fee is
taken out of the received asset, so **the largest term in the crypto cost model
moves no figure in that report**. It prints a line saying so. Measuring it is a
separate step, after the run:

```bash
uv run --env-file .env python scripts/crypto_fee_reconcile.py \
    --since <session start, ISO> --opening-cash <cash before the run>
```

It reads the venue's own closed orders against the positions and cash they left:
the buy-side fee shows up in **coin** (what was ordered versus what arrived) and
the sell-side fee in **cash**. Both readings are independent, and they should
agree. Take the opening cash from `make paper-preflight` before you launch —
there is no way to recover it afterwards.

**State the tier.** The published schedule is tiered on trailing 30-day *crypto*
notional and the account object carries no tier field, so the script reconstructs
it from closed orders. An account that has been trading is probably not on the
tier-1 rate `CostConfig.crypto()` models. Every dollar the run itself turns over
pushes that number, so re-measure it rather than quoting the last card's.

## Flattening afterwards, which is not optional

The session ends holding its book **and any orders still working** — and a crypto
order is GTC, so a working order does not expire overnight the way an equity one
does. It sits there until it fills or is cancelled.

1. Cancel every working order first, buys especially.
2. Sell the full quantity of every position.
3. A symbol with a working sell answers `403 / 40310000 … held_for_orders`, which
   arrives as a classified `OrderRejectedError` (ADR-0041). That is the
   parked-order case, not an error.
4. Re-check with `make paper-preflight`; it exits non-zero until the account is
   flat.

Note `sizing.SHARE_PRECISION = 6` rounds a full exit *up* past a 9-decimal crypto
holding (ADR-0058 §7). `AlpacaBroker` trims the dust, so an exit through the bench
works; a hand-written sell of the reported quantity may not.

## How to read the result

Everything in the equity runbook's "How to read the result" applies, with one
substitution and one addition.

- **The IEX caveat does not apply.** There is no feed choice here and no
  subscription: crypto bars come from the venue's own tape, which is also where the
  fills come from. The reference-lag caveat above takes its place, and it is
  larger.
- **"These are paper fills" is the headline, not the footnote.** On equities
  ADR-0052 could treat it as a caveat because the answer came back conservative.
  Alpaca's paper crypto venue *simulates* execution rather than routing it, so what
  is measured is our cost model against **Alpaca's crypto fill model**, and whether
  that model resembles a real crypto venue — in spread, in depth, in how a market
  order walks a book — is unestablished by anyone. If the number comes back
  optimistic, that is the first thing a reader needs, not the last.
