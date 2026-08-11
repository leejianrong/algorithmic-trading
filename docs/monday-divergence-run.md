# The divergence run

> **The 2026-08-10 run has happened. The answer is in
> [ADR-0052](adr/0052-measured-fill-slippage.md): 60 paired fills, realized slippage
> 0.51 bps against the model's 5.00, so the cost model is conservative by ~4.5 bps —
> with the caveats there, of which "these are paper fills" is the one that matters.**
>
> This page said to delete itself once that was recorded. It is kept instead, because
> it acquired dependents in the meantime: `make paper-live` (ADR-0051) runs the
> command below, `scripts/paper_session.sh` and the `Makefile` cite it, and five ADRs
> reference it. It is now the **operating procedure for repeating the measurement**,
> not a one-off plan — pooling more sessions is how the interval in ADR-0052 gets
> narrower. Read the timings as "a US session", not as that specific Monday.
>
> One thing the 2026-08-10 run changed about the procedure: **a session ends holding
> its book**, and leaves working orders that fill at the next open. Flattening is a
> manual step afterwards — see "After the run".

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

## Do not start it more than ~50 minutes early

Tempting, in Singapore, to set it running and go to bed. Don't start it before
**20:40 SGT**, and prefer 21:25–21:30.

The session stops after 60 minutes of silence (ADR-0049: 12 polls of 5 minutes; it
prints its own policy at startup). A poll before the open reveals nothing new, so
silence *before* the first bar counts exactly the same as a data outage during the
day. The first completed 5m bar is the 09:30–09:35 one, which lands at 09:35 ET =
21:35 SGT — so the clock starts from launch and has to reach 21:35 before it runs out:

| launch | ET | quiet before the first bar | empty polls | outcome |
|---|---|---|---|---|
| 21:30 SGT | 09:30 | 5 min | 1 | fine — this is the plan |
| 21:00 SGT | 09:00 | 35 min | 7 | fine |
| 20:40 SGT | 08:40 | 55 min | 11 | fine, but that is the edge |
| **20:30 SGT** | 08:30 | 65 min | 13 | **dies at ~21:35, before it ever trades** |
| **20:00 SGT** | 08:00 | 95 min | 19 | **dies at ~21:00** |

It exits *cleanly* when this happens — artifacts written, exit 0 — so from the
morning it looks like a completed run that found nothing. Check the warmup line and
`Processed N completed bar(s)` before believing a quiet result.

Starting late is the safer error: the session simply trades from whenever it starts.
Starting an hour early is the one that silently costs the day.

## Before you start

Four things, all quick. The first three are what `make paper-preflight` checks:

```bash
uv sync --extra alpaca     # the SDK is an optional extra, not in the frozen deps
make paper-preflight       # read-only; exits non-zero if anything is not clean
```

1. `uv sync --extra alpaca`. The SDK is an optional extra and is not in the frozen
   deps. The preflight reports whether it imports; it does not install it for you.
2. `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in the environment. The code reads
   `os.environ` directly and does not load `.env`, so use `uv run --env-file .env`
   if you are relying on the file. The preflight target passes `--env-file .env`
   when the file exists, and reports presence only — it never prints a key.
3. Check the paper account is flat. It should be, at about $100,000 with no
   positions and no open orders. The preflight prints cash, equity, the position
   count and the working-order count, and fails on anything held or working. It
   also prints the venue's own clock — whether the market is open, and the next
   open and close — which is the quickest way to confirm the date you are about to
   pass. That last read goes straight to the SDK inside `scripts/`, because the
   `AlpacaClient` seam has no calendar call and adding one is an ADR-0017 decision
   nobody needs for a run (ADR-0051).
4. Stop the laptop sleeping. There is no supervision and no restart. If the machine
   suspends, the run is gone. Nothing checks this for you. (This is what EPIC-86
   exists to fix, and it is not fixed yet.)

Then prove the whole path works, any time, without committing to the real thing:

```bash
make paper-dryrun          # the real command, scratch --out, stops at the first quiet poll
```

With the venue shut that primes warmup, trades nothing, and exits — which *is*
success. It passes `--max-empty-polls 1` so it stops at the first quiet poll
instead of waiting out the hour ADR-0049 gives a real session, so it costs one
poll boundary (up to five minutes at `5m`). What matters in the output is
`Warmup: primed N completed bar(s)` with `N` well above zero; the target says so
before it starts and re-checks it at the end.

## The command

```bash
uv run --env-file .env trading paper --strategy sma_crossover --symbols @blue20 \
  --interval 5m --source alpaca --broker alpaca --live \
  --data-feed iex --divergence \
  --from 2026-08-10 --to 2026-08-10 \
  --out results/paper/<UTC timestamp>-divergence
```

**Run it with `make paper-live`**, which is that exact command, detached, with the
`--out` timestamped for you. Nothing about the run changes — same strategy,
symbols, interval, source, broker, feed and flags — so the block above is still
what executes, and every launch echoes it in full and copies it to
`<out>/launch.cmd`. Two reasons the target exists rather than typing this: a
closed terminal kills a foreground session (SIGHUP is not handled), and a fixed
`--out` would truncate an earlier attempt's `fill_divergence.csv` at startup
(ADR-0048), which is precisely the evidence a retry is trying not to lose. The
strategy, symbols and interval stay overridable — `make paper-live
PAPER_STRATEGY=momentum` — and everything that defines the run does not.

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

## This run happens overnight, Singapore time

The operator is in Singapore (UTC+8). A US session is 09:30–16:00 New York time,
which lands **21:30 Monday to 04:00 Tuesday** locally. Plan for that before
anything else on this page:

| what | ET | UTC (what prints) | **SGT (your clock)** |
|---|---|---|---|
| Open, session starts | Mon 09:30 | Mon 13:30 | **Mon 21:30** |
| First trade | ~Mon 09:35 | ~Mon 13:35 | **~Mon 21:35** |
| Close, last bar | Mon 16:00 | Mon 20:00 | **Tue 04:00** |
| Session ends by itself | ~Mon 17:00 | ~Mon 21:00 | **Tue 05:00** |

Three columns because all three are real: the runbook narrates in ET because that
is what a trading day *is*, the terminal prints UTC because that is what every
timestamp in this bench carries, and SGT is when you will actually be awake.
Nothing converts in code, deliberately — one clock everywhere beats two that can
disagree, and rendering market-local time needs a timezone database and a decision
about which market. So the first per-bar line you are waiting for reads
`2026-08-10 13:35`. (ET is UTC-4 in August, UTC-5 in winter.)

**You will be asleep for nearly all of it.** That is survivable — every artifact is
now written as the session goes rather than at the end (ADR-0048), and a stop signal
finalizes cleanly (ADR-0043) — but it changes what "watch for X" means everywhere
below. You are not watching; you are reading the evidence on Tuesday morning. Two
consequences worth acting on:

- **Detach the session, or a closed terminal kills it.** SIGHUP is not handled.
  `make paper-live` does this for you — `tmux` if it is installed (recommended: you
  can reattach on Tuesday with `tmux attach -t paper-<stamp>` and read the
  scrollback), `setsid` otherwise, and it prints which one it used. Launching the
  raw command in a foreground shell and then closing the lid at 22:00 loses the run.
  Do not reach for `nohup` on its own: `uv run` installs its own SIGHUP handler,
  which overrides the ignore `nohup` sets, and a HUP in uv's first second kills the
  wrapper before the python child that would have inherited the ignore even exists
  (measured — the process gone with an empty log, ADR-0051).
- **Stop the machine sleeping, and mean it.** This is a WSL2 host, so Windows
  suspending takes WSL down with it and the run is gone. Check the Windows power
  plan, not just the Linux side. An overnight run is exactly the case the
  "no supervision, no restart" warning below is about — EPIC-86 is the fix, and it is
  not built.

## What should happen, in order

**09:30.** The session starts and prints two lines: how long it will tolerate silence
(`Stops after 12 consecutive poll(s) with no new bar — 1 hour of silence at 5m`) and
a warmup line. It primes about 512 bars of history without trading them, then sleeps
to the next 5-minute boundary.

**About 09:35.** First trade. Not 09:30, and that is correct: the session trades the
first bar that completes after startup. Before ADR-0042 it would have fired roughly
565 orders on week-old signals in the first few seconds, which is the bug we fixed on
Saturday.

**Through the day.** Fills accumulate, spread across the session rather than clustered
at the open. Expect somewhere near 87.

**16:00.** The close. No new bars complete after this.

**About 17:00 to 17:05.** The session ends on its own, roughly an hour after the last
bar, and writes all five artifacts on the way out. It stops after an hour of silence:
twelve consecutive 5-minute polls with no new bar (ADR-0049). Until then it keeps
polling a shut venue, which is intended — the polls are nearly free and stopping early
would cost the measurement. Nothing is printed while it waits, so between about 16:05
and 17:00 the session is silent by design. This looks like the program quitting
unexpectedly when it finally exits. It is not.

## What to look out for

**Ctrl-C and `kill` are both safe now.** ADR-0033 makes `KeyboardInterrupt` write the
artifacts, and since ADR-0043 SIGTERM takes the same path, so `kill <pid>` from
another terminal — or `docker stop`, or `systemd stop` — finalizes the run instead of
destroying it. The summary names which one stopped it. Two things that are still not
safe: `kill -9`, which by definition cannot be caught, and **closing the terminal**,
which sends SIGHUP and is not handled. Watched directly: a SIGHUP to a running
session's process group killed it in under a second and left `equity_curve.csv` and
`result.json` unwritten — only `paper_session.log` and the incrementally journaled
`fill_divergence.csv` (ADR-0048) survived. `make paper-live` is what avoids that; it
is not optional for an overnight run.

Once it is finalizing, a second `kill` is ignored on purpose, so that writing
`equity_curve.csv` and `result.json` cannot be interrupted half way. It takes
milliseconds. If you truly need it gone, `kill -9`.

One wrinkle if you launched with `uv run`, which the command above does: **`uv run`
forks**, so there are two processes and the one you see in `ps` is the wrapper. A
plain `kill` on the wrapper is fine — uv forwards SIGTERM and the session finalizes,
verified against this exact command. `kill -9` is not: SIGKILL cannot be forwarded, so
you kill the wrapper and leave the session running orphaned. If you ever need to be
certain you are signalling the session itself, target the `.venv/bin/python` child.

`make paper-stop` is that `kill`, with the pid bookkeeping done for you: it re-reads
the live wrapper pid from tmux, refuses to signal a pid that no longer looks like the
session, sends SIGTERM and only SIGTERM, waits for the exit, and then lists the five
artifacts and their sizes. `make paper-status` is the read-only version — where the
run is writing, whether it is still alive, `paper_state.json`, and the last few
console lines. There is no dashboard for a running session (KAN-712); that is all
you get, and on Tuesday morning it is enough to tell a finished run from a dead one.

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

**Watch for the session ending early.** An exit before about 17:00 is a feed problem,
not a finished run — it means an hour passed with no new bar, which mid-session can
only be an outage. Until ADR-0049 the tolerance was ten minutes, so a brief IEX hiccup
was enough to end the day; an 11:00 exit now takes a 55-minute blackout and is worth
believing. What the run cannot survive either way is a gap that swallows the whole
hour: the session stops, writes its artifacts, and whatever fills it had are all it
gets. Rerun on Tuesday if that happens.

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

## After the run

**The session ends holding its book.** Nothing liquidates — correct for a strategy,
inconvenient for a test bench — so the account is left with open positions *and* with
any orders that were still working when it stopped. Those working orders fill at the
next open unless you cancel them. Observed on 2026-08-10: 10 positions held, plus two
BUY orders (HON, V) that would have rebuilt positions the following morning.

To flatten, with the venue shut (orders park and fill at the next open, ADR-0036):

1. Cancel every working **BUY** first, or it will re-open a position you just sold.
2. Submit a **SELL** for the full quantity of each position.
3. Expect a refusal on any symbol that already has a working sell — the venue answers
   `403 / 40310000 insufficient qty available … held_for_orders`, which arrives as a
   classified `OrderRejectedError` (ADR-0041). That is the parked-order case, not an
   error: the position is already being flattened by the earlier order.
4. Re-check with `make paper-preflight` once the market has opened and the sells have
   filled; it exits non-zero until the account is flat.

Do this *before* the next session, not after, so a run starts from a known state.

## If it goes wrong

Run it again the next trading day. Nothing about the setup is single-use, and the only
cost of a failed run is the day. If Monday shows the feed dropping out for longer than an hour,
`--max-empty-polls N` overrides the tolerance for the retry (`N` polls, so at `5m` a
value of 24 buys two hours; `make paper-live PAPER_EXTRA_ARGS="--max-empty-polls 24"`);
the default is what should be used otherwise. Sample size can also be pooled across sessions by hand:
concatenate the per-session `fill_divergence.csv` files (`make paper-live` gives each
launch its own timestamped `--out`, so nothing overwrites) and average
`realized_slippage_bps` where the live outcome
is a fill and the model outcome is a fill. Pooling across days pools across market
conditions, which widens the variance the report's `stdev_realized_bps` is meant to
express, so say so if you do it.
