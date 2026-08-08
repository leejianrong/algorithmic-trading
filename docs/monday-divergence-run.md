# The Monday divergence run

A runbook for 2026-08-10, written 2026-08-09. Delete or fold into an ADR once the
run has happened and the answer is recorded.

## What we are measuring

Every backtest this bench has ever produced assumes each fill lands 0.05% worse than
the reference price. That is `CostConfig.slippage_bps = 5.0`, and the code calls it
"deliberately pessimistic". Nobody has checked it.

It matters more than it sounds. Turnover multiplies the assumption, and one run this
week showed 623% turnover, so the cost model gets applied many times over a single
result. If real slippage is 25 bps and we model 5, a high-turnover strategy looks
profitable on screen and loses money with real capital. No backtest can catch this,
because a backtest *is* the assumption.

Monday runs a live paper session with a counterfactual `SimulatedBroker` shadowing it
(ADR-0038). For each order we record what the venue actually filled at and what the
model would have filled at, both measured against the same reference price. The
difference is the answer.

## Before you start

Four things, all quick:

1. `uv sync --extra alpaca`. The SDK is an optional extra and is not in the frozen
   deps.
2. `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the environment. The code reads
   `os.environ` directly and does not load `.env`, so use `uv run --env-file .env`
   if you are relying on the file.
3. Check the paper account is flat. It should be, at about $100,000 with no
   positions and no open orders.
4. Stop the laptop sleeping. There is no supervision and no restart. If the machine
   suspends, the run is gone. (This is what EPIC-86 exists to fix, and it is not
   fixed yet.)

## The command

```bash
uv run trading paper --strategy sma_crossover --symbols @blue20 \
  --interval 5m --source alpaca --broker alpaca --live \
  --data-feed iex --divergence \
  --from 2026-08-10 --to 2026-08-10 \
  --out results/paper/2026-08-10-divergence
```

`sma_crossover` at 5m over `@blue20` was chosen by measurement, not taste. Across 25
seed and session combinations it produced 75 to 108 fills per session, median 87, and
cleared 30 every single time. Median order size is about $4,600, which is realistic
order flow rather than a fill generator. `equal_weight` would hit 30 anywhere but its
median fill is $4.66, and those measure nothing about how a real order gets filled.

Two quirks in the invocation. `--from` and `--to` are ignored under `--live` but the
parser still demands them. `--cash` is ignored under `--broker alpaca`, because the
portfolio reconciles from the account.

The per-bar lines go to stdout as always. Since ADR-0043 there is also a timestamped
log on **stderr** carrying the session's lifecycle — when it started and with what,
what it primed, why it stopped — plus anything the feed guard says about a symbol
dropping out (ADR-0035), which used to be either unformatted or invisible. If you
want that in a file, redirect stderr; `2>session.log` will not disturb the stdout
report you are watching. `--log-level` and `--log-format json` exist and neither is
needed here — the defaults are what the command above gets.

## What should happen, in order

**09:30.** The session starts and prints a warmup line. It primes about 512 bars of
history without trading them, then sleeps to the next 5-minute boundary.

**About 09:35.** First trade. Not 09:30, and that is correct: the session trades the
first bar that completes after startup. Before ADR-0042 it would have fired roughly
565 orders on week-old signals in the first few seconds, which is the bug we fixed on
Saturday.

**Through the day.** Fills accumulate, spread across the session rather than clustered
at the open. Expect somewhere near 87.

**16:00.** The close. No new bars complete after this.

**16:10 to 16:15.** The session ends on its own. Two consecutive polls with no new
bar stop it (`max_empty_polls = 2`), and it writes all five artifacts on the way out.
This looks like the program quitting unexpectedly. It is not.

## What to look out for

**Ctrl-C and `kill` are both safe now.** ADR-0033 makes `KeyboardInterrupt` write the
artifacts, and since ADR-0043 SIGTERM takes the same path, so `kill <pid>` from
another terminal — or `docker stop`, or `systemd stop` — finalizes the run instead of
destroying it. The summary names which one stopped it. Two things that are still not
safe: `kill -9`, which by definition cannot be caught, and **closing the terminal**,
which sends SIGHUP and is not handled. If you need to walk away from the session,
start it under `nohup` or `tmux`.

Once it is finalizing, a second `kill` is ignored on purpose, so that writing
`equity_curve.csv` and `result.json` cannot be interrupted half way. It takes
milliseconds. If you truly need it gone, `kill -9`.

**Check the first order's timestamp.** If anything in `fill_divergence.csv` has a
`submitted_ts` before 09:30, the warmup fix did not take and the sample is
contaminated. It should not happen, but it is a two-second check and it is the one
failure that would quietly ruin the numbers.

**A stream of rejections means orders are not filling.** The duplicate-order guard
(ADR-0036) refuses a second order for a symbol that already has one working. During
market hours fills should be fast enough that this rarely triggers. If the summary
shows many refusals, something is wrong with execution and the fills you do have are
not representative.

**A wash-trade refusal should not appear.** Alpaca rejects an opposite-side order
while one is working, with a 403 and code 40310000. That is an artefact of parked
orders, so it belongs to closed markets. If it shows up mid-session, orders are
sitting unfilled for whole bars. The run will survive it now (ADR-0041 classifies it
instead of letting it kill the session) but it is a signal worth noticing.

**Watch for the session ending early.** Two consecutive polls with nothing new will
stop it, and a data gap of ten minutes would do that as easily as the close. If it
exits at 11:00, that is a feed problem, not a finished run.

## How to read the result

The report prints realized slippage against modelled slippage, in basis points,
signed so positive is worse for us. It refuses to conclude below 30 paired fills and
says so plainly.

Treat the answer in bands:

- **3 to 8 bps.** The model is about right. Nothing downstream needs revisiting.
- **Around 40 bps.** The model is badly optimistic and every backtest result is
  overstated, most of all the high-turnover ones. This would be the most valuable
  possible outcome, and the least comfortable.
- **Anywhere near the boundary of a decision you care about.** The IEX caveat below
  becomes load-bearing, and buying one month of SIP data to settle it is reasonable.

### The IEX caveat

The reference price comes from IEX bars while the fill comes from Alpaca's execution
engine routing against consolidated quotes. IEX is one exchange carrying a few percent
of US volume, and we are on it because the free data plan refuses recent SIP data
(ADR-0034). On a $250 mega-cap a one-cent print difference is 0.4 bps against a 5 bps
model, so roughly 8% systematic error.

It is mostly noise rather than bias, and with about 87 fills it averages down. State
it on the result anyway. `@blue20` is the best case for this, since deep continuously
quoted names track consolidated closely.

### What the run cannot tell us

One account, one venue, one strategy's order flow, one day. It says nothing about
small caps, where spreads are far wider, and nothing about crypto. It is a
measurement of this venue under these conditions, which is exactly what we need
before trusting any backtest number, and no more than that.

## If it goes wrong

Run it again Tuesday. Nothing about the setup is single-use, and the only cost of a
failed run is the day. Sample size can also be pooled across sessions by hand:
concatenate the per-session `fill_divergence.csv` files (use a distinct `--out` each
time, or they overwrite) and average `realized_slippage_bps` where the live outcome
is a fill and the model outcome is a fill. Pooling across days pools across market
conditions, which widens the variance the report's `stdev_realized_bps` is meant to
express, so say so if you do it.
