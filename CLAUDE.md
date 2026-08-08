# Agent brief — algorithmic trading test bench

A test bench where one **event-driven engine** runs a strategy over US-equity
**daily** bars two ways — `backtest` and `paper` — with the same execution,
sizing, costs, and risk limits in both. Built as a stepping stone toward real
capital, so it favors honest numbers over flattering ones. Full context:
[`PLAN.md`](PLAN.md), slices in [`SLICES.md`](SLICES.md), decisions in
[`docs/adr/`](docs/adr/), decision register in [`QUESTIONS.md`](QUESTIONS.md).

## Build status — trust the code over these docs

As of this writing:

- **Done and tested:** the scaffold and quality gates; core value types
  (`types.py`) and DI seams (`interfaces.py`); **V1 — a working backtest**: the
  event-driven `Engine` (`engine.py`), `SimulatedBroker` (`broker.py`, next-open
  fills + slippage/commission + funding rejection), the cached `YFinanceAdapter`
  (`data/yfinance_adapter.py`) and in-memory `FakeAdapter`, a minimal report
  (`report.py`), and the `trading backtest` CLI (`cli.py`); and **V2 — target-weight
  sizing** (`sizing.py`, resolves `TargetWeight` → fractional-share rebalance
  orders), the `sma_crossover` and `equal_weight` strategies plus `buy_and_hold`
  baseline (`strategies/`), and a fill blotter on `BacktestResult`. Fast tests
  green; `trading backtest` runs end to end for all three strategies.
- **Offline data:** a deterministic **synthetic** GBM adapter (`data/synthetic.py`,
  ADR-0012) drives the whole stack without a network — `trading backtest --source
  synthetic` and `trading gen-data`. All three strategies verified end to end on it.
  **Range-independent since ADR-0030:** there is one canonical series per
  `(symbol, seed, params, frequency)` anchored at a fixed `EPOCH` (1990-01-01), and a
  request is a *slice* of it — a bar is a pure function of its absolute position, drawn
  from a counter-based `blake2b` + Box-Muller stream, so overlapping ranges agree on
  every shared timestamp. Before that the adapter reseeded per call and walked from the
  requested `start`, so two different spans came back byte-identical (which made a
  synthetic walk-forward a null test) and `paper --live --source synthetic` walked from
  year 1 to price a 2022 bar at `1.5e+81`. Costs, both documented in the module: the
  price level walks from the epoch (`O(bars from the epoch)`, ~20 ms/symbol, memoized
  per instance), and bars before 1990 do not exist (a `datetime.min` request is
  clipped, which is what the paper feed does). Intraday is a Brownian bridge onto the
  daily close, so 1h/30m/5m/1m agree with 1d at every session close.
- **V3 — enforced risk guardrails:** `RiskConfig` + a stateful `Guardrails`
  (`risk.py`, ADR-0013) sit on the engine's order path, enforced by default
  (`--no-guardrails` opts out): a per-symbol position cap and a gross-exposure cap
  clamp over-cap buys (net of same-bar committed exposure), and a latching
  drawdown / daily-loss kill switch blocks new entries while still allowing exits.
  `BacktestResult` carries `clamps` + merged `rejections` + halt state; the report
  surfaces them. Fast tests green.
- **V4 — performance report, exposure & benchmark:** `metrics.py` computes total
  & annualized return, Sharpe (Q17), max drawdown, win rate, and avg/peak exposure
  (`PerformanceMetrics` via `compute`). The engine records per-bar gross exposure
  on each `EquityPoint`; the report renders the full metrics block, writes an
  `exposure` column (+ aligned `benchmark_equity` when enabled) to the CSV, and an
  optional lazy-matplotlib PNG. The CLI gains `--benchmark SYMBOL` (unconstrained
  buy-and-hold, offline-capable) and `--plot/--no-plot`. Fast tests green.
- **V5 — paper mode:** the per-bar loop body is a shared `Engine._step` (with
  `_RunState`/`_finalize`) that both backtest (`Engine.run`) and paper
  (`PaperSession`, `engine.py`) drive, so the modes can't fork (ADR-0002/0014);
  backtest stays byte-identical. A `Clock` seam (`clock.py`: `WallClock` /
  `ImmediateClock` / `FakeClock`) and a completed-bars `RecentWindowFeed`
  (`data/recent_window.py`) feed paper; the loop processes each newly completed
  bar once (idempotent), logs a `BarOutcome`, and sleeps until the next is due. The
  `trading paper` CLI mirrors `backtest` plus `--live/--once` (default `--once`
  replays offline and terminates; persists a session log, `paper_state.json`, and
  the equity CSV). Fast tests green (parity, completed-bars-only, halt parity).
- **Offline enhancement batch:** three parallel, offline-verified additions.
  **Strategies** — `momentum` (time-series trailing-return) and `mean_reversion`
  (RSI oversold/recovery) join the registry, with `rsi`/`rolling_std`/`bollinger`
  in `strategies/indicators.py`; both long-or-flat, transition-driven, no look-ahead.
  **Parameter sweep** — `sweep.py` (`run_sweep`) + the `trading sweep` CLI expand a
  param grid (optional per-window walk-forward) over `Engine.run` as a pure outer
  loop, ranked by Sharpe/total return, writing a CSV (ADR-0016). **Vol-target risk +
  richer metrics** — opt-in `RiskConfig.target_volatility` (CLI `--target-vol`) scales
  the effective gross-exposure cap by realized-vs-target vol inside `Guardrails`, off
  by default (ADR-0015); `metrics.py` adds Sortino, Calmar, and turnover to the report.
  Fast tests green.
- **Alpaca milestone — live paper trading + real data (offline-verified):** an
  `AlpacaClient` seam (`data/alpaca_client.py`, ADR-0017/0018) with our-own-types
  DTOs, a `FakeAlpacaClient` for the fast layer, and a lazy `RealAlpacaClient` over
  the optional `alpaca-py` SDK (kept out of the locked deps; a mypy override only).
  On it: `AlpacaAdapter` (`data/alpaca_adapter.py`, adjusted daily bars) and
  `AlpacaBroker` (`brokers/alpaca.py`, ADR-0020) — a submit-then-poll paper broker
  that reconciles its `Portfolio` from the Alpaca account (authoritative, not
  byte-identical to backtest). Wired into the CLI: `--source alpaca`,
  `trading paper --broker alpaca --live`. Shipped alongside a **CSV**
  bring-your-own-data source (`data/csv_adapter.py`, `--source csv`) and **per-sector
  exposure caps** (`RiskConfig.sector_map` + `max_sector_exposure`, CLI
  `--max-sector-exposure`/`--sector-map`, ADR-0019). Integration also fixed a
  pre-existing guardrail-clamp bug (a sub-precision positive allowance rounding to a
  zero-qty order now rejects; regression-tested). Fast tests green.
- **Roadmap milestone — raw/adjusted split, intraday, web dashboard (offline-verified):**
  three parallel lanes off settled foundations, integrated behind one CLI PR.
  **Raw-vs-adjusted price policy** (ADR-0021) — the price notion is now an explicit
  per-mode policy carried by the feed: backtest asks for split/dividend-adjusted
  total-return prices (ADR-0008, unchanged), the paper/live feed asks for RAW actual
  quotes so the strategy decides and marks in the same dollars `AlpacaBroker`
  reconciles from the account. `AlpacaAdapter.get_bars` honors the per-call `adjusted`
  flag; `RecentWindowFeed` defaults to raw; yfinance/csv stay adjusted-only and error
  with guidance. **Intraday / bar-frequency abstraction** (ADR-0022, `frequency.py`) —
  a `Frequency` value (label/delta/`periods_per_year`); the interval is an
  adapter-construction property, so `DataAdapter.get_bars` and `Engine._step` are
  untouched and daily runs stay byte-identical. `SyntheticAdapter` generates sub-daily
  session-window bars (START-stamped), `recent_window.interval_is_complete` gates a
  bar until `ts+interval`, `PaperSession` wakes on the interval boundary, and
  `metrics.compute` annualizes by `periods_per_year`. Real intraday via Alpaca is
  wired behind a creds/SDK-gated integration test. **Web dashboard** (ADR-0023,
  `dashboard/`) — reads a run's canonical machine-readable `result.json`
  (`report.result_to_dict`/`write_result_json`, `RESULT_SCHEMA_VERSION`) and renders
  the equity curve (+benchmark), metrics, fills, and guardrail actions two ways: a
  pure-stdlib self-contained static HTML export (`--static`, zero external refs) and a
  thin, lazily-imported FastAPI server (`--serve`). FastAPI/uvicorn are the optional
  `dashboard` extra (locked additively into `uv.lock`; mypy override), so the fast gate
  stays green without them. CLI: `--interval 1d|1h|30m|5m|1m` on backtest/paper/sweep;
  backtest & paper now emit `result.json`; new `trading dashboard --static|--serve`.
  Fast gate green; the full daily→intraday→result.json→dashboard pipeline verified end
  to end offline on synthetic data.
- **Curated universes (offline-verified):** `universe.py` (ADR-0024) holds named
  stock baskets — a frozen `Basket` + `BASKETS` registry with `get_universe` /
  `get_sector_map`, seeded by `blue20` (20 mega-cap US names across 8 sectors). The
  CLI expands a `@name` sigil on `--symbols` (unknown -> exit 2 naming the baskets)
  and `--sector-map` (unknown -> the ValueError CLI-error path); plain comma lists
  are unchanged. Fast gate green; `backtest --source synthetic --symbols @blue20
  --sector-map @blue20 --max-sector-exposure 0.30` runs end to end offline. The two
  honesty caveats in the module docstring are now both *addressed* rather than merely
  stated — see the validation batch below.
- **Cross-sectional strategy (offline-verified):** `cross_sectional`
  (`strategies/cross_sectional.py`, ADR-0025) joins the registry — the first
  *cross-equity* (relative-strength) strategy. Each rebalance it ranks the whole
  universe by trailing total return over `lookback` (default 120) from
  `context.history` (past+present only), holds the top `top_k` (default 8) at equal
  weight `weight/top_k` (weight default 0.9), and targets 0.0 for the rest (exit).
  Turnover is controlled by a `rebalance_days` cadence (default 21 ≈ monthly), not
  daily churn; long-or-flat only; warmup stays flat until `lookback` bars exist. All
  four params sweep via `trading sweep --param` (constructor kwargs). Fits the
  existing `Strategy` seam with no engine/interface change; `weight/top_k` must stay
  under the position cap (K=8 → ~11%, safe) or the guardrails clamp. Fast gate green;
  runs end to end on `--symbols @blue20 --source synthetic`.
- **Validation-honesty batch (three lanes, offline-verified):** the checks that decide
  whether a reported number means anything.
  **True walk-forward** (`sweep.run_walk_forward`, ADR-0026) — `--folds N` cuts the
  range into folds, tunes the whole grid **in-sample**, then runs the single winner
  **exactly once out-of-sample**; the summary leads with mean OOS Sharpe and the
  IS→OOS degradation. The pre-existing `--windows` is a *plain* per-window grid sweep
  (all in-sample) and is unchanged; passing both is an error. A spy adapter proves the
  OOS span is requested exactly once, and a rigged fixture proves the summary reports
  the IS winner's *worse* OOS numbers rather than the best OOS combo.
  **Broker-verified universe** (`universe.validate_universe` + `AlpacaClient.get_asset`,
  ADR-0028) — sorts a candidate basket into `usable` (tradable **and** fractionable),
  `unusable` (the broker said no), and `unverified` (the lookup failed — unknown, not
  rejected); nothing is filtered silently. CLI: `trading verify-universe --symbols
  @blue20` (needs creds + `alpaca-py`; exits 1 when not clean). `universe.py` still has
  **no runtime import** of `trading.data` — the client is typed structurally, enforced
  by a subprocess test.
  **Survivorship bias** (ADR-0027) — recorded as an accepted, documented, *unfixed*
  limitation: `blue20` is today's winners, yfinance has no delisted names, so curated
  backtests are an **upper bound**; forward paper results are survivorship-free and
  should outweigh them.
  **Liquidity + significance** (`liquidity.py`, `metrics.entry_count`,
  `strategies.free_parameter_count`, ADR-0029) — `--min-adv` screens the universe by
  average dollar volume measured in a formation window ending **before** `--from`
  (computing ADV over the backtest range would be look-ahead; a test records every
  range requested from the adapter and asserts each ends before the start line). Every
  run now reports its entry count, and below 30 trades per free parameter the summary
  warns explicitly. `volume` was parsed by every adapter and read by nothing until now.
- **Halt recovery (offline-verified):** the drawdown kill switch can **re-arm**
  instead of latching for the whole run (ADR-0031) — the latch was measured to halt
  every strategy in ~2001 on 2000-2020 real data and then block entries for 19 years
  (`cross_sectional`: -3.91% latched vs +1727% neutralized). Two opt-in `RiskConfig`
  knobs, `halt_recovery_drawdown_pct` and `halt_cooldown_bars` (CLI
  `--halt-recovery-drawdown` / `--halt-cooldown-bars` on backtest/paper/sweep), both
  `None` by default so prior runs are byte-identical. Whichever triggers **first**
  re-arms (OR, not AND: a halted long-or-flat book drains to cash and freezes its
  drawdown, so AND measurably reinstated the permanent latch). Anti-flap is enforced:
  the config rejects a recovery threshold at/above `max_drawdown_pct`, re-arming
  resets the drawdown *control* peak to the resume equity (reported drawdown still
  comes from `metrics`), and a re-arm bar never re-halts — so with a cooldown of N,
  halts cannot recur faster than every N+1 bars. `BacktestResult` keeps
  `halted`/`halt_ts`/`halt_reason` (first halt) and adds `halt_episodes`
  (`(halt_ts, reason, resume_ts)`); `result.json`'s `halt` gains `episode_count` +
  `episodes` additively, so `RESULT_SCHEMA_VERSION` stays **1**. Fast gate green.
- **Alpaca live verification — the paper path actually runs now (2026-08-04):** the
  whole Alpaca path had been merged **without ever being executed** (ADR-0018:
  "verified by inspection and types only"). It has now been driven against a real
  paper account. `alpaca-py` is locked as the optional **`alpaca` extra**
  (`uv sync --extra alpaca`; ADR-0018 amended) — and installing it immediately
  exposed **8 `mypy --strict` errors**, because CI's `typecheck` job runs
  `uv sync --frozen` (no extras) so the SDK was typed as `Any`: every client method
  returns `Model | Dict[str, Any]` (`_require_model` narrows it, failing loudly on
  the raw-dict arm) and `TradeAccount.cash`/`.equity` are `Optional[str]`
  (`_require_float` names the field instead of crashing in `float(None)`). CI now
  type-checks **twice**, the second time with the extra installed. Three real bugs
  followed, each a failing test then a fix: **(1)** only 2 of Alpaca's 5 terminal
  order statuses were recognised, so a `canceled`/`expired`/`replaced` order leaked
  into `_pending` forever and burned the full 30s poll timeout every bar
  (`TERMINAL_STATUSES`, public `pending_order_ids`, partial fills still emitted —
  ADR-0033); **(2)** `paper --broker alpaca --live` could not fetch one bar — the
  live feed polls to `now` and a free data plan answers **HTTP 403** on the SIP tape
  inside ~15 min, so the feed is now a client construction property with
  `--data-feed` (live Alpaca defaults to `iex`) and a plan refusal is a classified
  `DataSubscriptionError`, not a raw traceback (ADR-0034); **(3)** `--live` sessions
  lost their artifacts, since Ctrl-C is the only exit and it skipped past the
  equity CSV / `result.json` / summary (`PaperSession.finalize` + a CLI
  `KeyboardInterrupt` path — ADR-0033). Verified live: account/positions,
  `get_asset` (the 404→`LookupError` branch really fires; the `AssetExchange.`
  prefix really needs stripping), daily + 1h bars, raw-vs-adjusted across AAPL's
  4:1 split (499.30 raw vs 121.08 adjusted), a real fractional fill through
  `AlpacaBroker`, and a 377-bar live paper session. **Both curated baskets came back
  100% clean** — `blue20` 20/20, `core10` 10/10 tradable+fractionable (ADR-0024/0028
  amended; it is a snapshot against one account, not a permanent fact). 23 new
  integration tests, double-gated on creds **and** SDK, skip cleanly in CI.
- **A run keeps its information (2026-08-08, offline-verified):** two lanes closing
  gaps ADR-0032 had recorded against itself. **Paper feed per-symbol guard**
  (`data/recent_window.py`, ADR-0035) — `RecentWindowFeed.poll` used to fetch in an
  unguarded loop, so one bad ticker killed a whole paper poll; it now guards each
  symbol exactly the way `load_series` does, reusing `AbsentSymbol` and the same two
  reason codes rather than re-declaring them. The asymmetry is the point: a dead
  backtest is re-runnable, a dead **live session is gone** (it is the one
  survivorship-free evidence this bench has, ADR-0027), and a session polls the same
  symbol hundreds of times so a transient failure is a certainty, not an edge case.
  A symbol is **never quarantined** — every poll retries every requested symbol, so
  the traded universe cannot silently shrink mid-session; persistence changes the
  *loudness* only (`absence_streaks`, `persistently_absent` at 3 consecutive misses,
  log escalating WARNING→ERROR on state change, INFO on recovery). A still-forming
  bar is not an absence, and a poll where everything fails returns an empty feed
  rather than raising — the existing `max_empty_polls` still stops a real outage
  cleanly, with artifacts finalized. **Absent symbols and benchmarks are reported**
  (`report.py`, `cli.py`) — `summarize` prints a `Traded:` line plus a per-symbol
  `⚠ … contributed no bars` caveat *directly under* `Symbols:`, because a shrunk
  universe is a caveat on every figure below it rather than an event like a clamp;
  `result_to_dict` gains a top-level `absent` list, additive, so
  `RESULT_SCHEMA_VERSION` stays **1**. And a failing `--benchmark` symbol now costs
  one warning line instead of the whole command: `cli._run_benchmark` catches
  `EmptyUniverseError` only — after ADR-0032 every way the benchmark's *data* can
  fail arrives as that one type, while a broken guardrail or sizing crash still
  propagates, because that would make the strategy numbers suspect too. Still open
  from ADR-0035: `trading paper` does not yet surface `feed.absent` /
  `persistently_absent` in the session summary or `result.json` — a dropped symbol
  reaches the operator through the log record only.
- **Market-closed order branch verified live (2026-08-08, ADR-0036):** the one path
  PR #34 could not execute. With the venue shut, a fractional `TimeInForce.DAY`
  market order is **parked at status `accepted`** — not filled, not rejected — which
  is correctly non-terminal, so the poll times out cleanly and the id stays in
  `pending_order_ids`; cancelling moves it to `canceled` in under a second and the
  next `on_bar` settles it on the first poll and evicts it. Two real defects fell out.
  **(1)** `AlpacaBroker.rejections` recorded `(order_id, reason)` while
  `SimulatedBroker`, `BacktestResult.rejections`, and `report.result_to_dict` all use
  `(Order, reason)` — `Engine._finalize` merges them through a `getattr`, so
  `mypy --strict` never saw it, and the first order to end `canceled`/`expired`/
  `replaced` in a live session crashed `result.json` with `'str' object has no
  attribute 'symbol'`. An unfilled DAY order **expires** at the close, so that is the
  routine end of every order this branch parks. Fixed to `(Order, reason)`, pinned
  three ways in the fast layer (including a shape-match against `SimulatedBroker`).
  **(2)** the seam had no way to take a parked order back, so the live test's cleanup
  left a queued buy that would fill at the next open — `AlpacaClient.cancel_order` is
  now the seam's sixth call (the widening ADR-0017 anticipated), idempotent on an
  already-terminal order and `LookupError` on an unknown id, both observed against the
  venue rather than assumed. 5 new live tests (skip when the market is open) + 8 fast.
  Account left flat. Still open: an order that *expires* overnight (same code path as
  `canceled`, not yet watched). Duplicate order stacking, the other gap this ADR
  recorded, is now closed — see below.
- **A parked order cannot be duplicated (2026-08-08, ADR-0036 amended, offline-verified):**
  while an order sat `accepted` at the venue the portfolio reconciled from a flat
  account, so a target-weight strategy re-emitted the same order every bar and
  `AlpacaBroker` submitted it again — orders compounding for as long as the venue held
  them, then all filling at the open. And the guardrails were **no backstop**:
  `Guardrails` nets same-bar committed exposure, but that tally resets at the top of
  every bar while `current_gross` reads off a book a parked order leaves flat, so each
  bar re-authorised a fresh *full* gross allowance. Measured: five bars of an unmet 20%
  target queued **100% of equity**. `submit` now **refuses** a new order when the broker
  is already working one in the same symbol **and side**, recording `(Order, reason)` on
  `rejections` (so it reaches `result.json` and the summary, never a silent drop) and
  naming the working order's venue id. Keying on the side is what makes "never block an
  exit" structural rather than a special case: long-or-flat means a SELL is the only way
  out (ADR-0011), the same asymmetry the halt path already encodes (ADR-0013/0031), and
  an exit is never even compared against a working entry. A duplicate SELL *is*
  suppressed — the first is already working and will still fill; a second would oversell.
  Partial fills stay legitimate (ADR-0033): the key is *working*, not submitted, so a
  partially-filled-then-`canceled`/`expired` order is out of the pending set and a
  follow-up for the remainder goes through, while one still reporting
  `partially_filled` suppresses the remainder until the venue settles it. Built from the
  state the broker already had (`_pending` + `_requested`) — no new bookkeeping, no seam
  change, and `SimulatedBroker` fills within the bar so it has no working order to
  duplicate: **the backtest path is untouched**. 10 new fast tests, including the
  exposure statement asserted end to end through the real `Engine` with default
  guardrails so the cross-bar hole cannot reopen quietly. This is the **symptom-level**
  guard; the intent-level fix (the sizer netting in-flight quantity, so the duplicate is
  never generated) is deliberately deferred as **KAN-678** — defence in depth, not
  alternatives. Known cosmetic gaps, both in the shared engine's per-bar bookkeeping
  rather than the broker: a refusal reaches `BacktestResult.rejections` but not that
  bar's `BarOutcome.broker_rejections` (the engine snapshots the broker's list around
  `on_bar` only) and the refused order still appears in `BarOutcome.submitted`.
  ~~The live test is unexecuted~~ — **now run, see below.**
- **The duplicate guard, witnessed live — and the venue refuses things too
  (2026-08-08, ADR-0041):** the one half of ADR-0036 that shipped unexecuted was
  driven against the paper account with the venue shut. **The guard holds exactly as
  designed:** three bars of the same unmet `BUY 0.01 AAPL` queued **one** order at the
  venue (parked `accepted`) plus two refusals naming its id, confirmed against
  Alpaca's own order list; the whole pre-existing market-closed class still passes, so
  the guard did not disturb the parked-order path. Two things the offline fake had
  wrong. **(1)** the venue is genuinely no backstop — two identical BUYs submitted
  straight at the client both came back `accepted` with distinct ids, both working, so
  ADR-0036's *reasoned* premise is now checked (and has its own live test, so the day
  Alpaca starts deduplicating we hear about it). **(2)** the venue **refuses the
  opposite side** while an order is working: `403 {"code":40310000,"message":"potential
  wash trade detected. use complex orders","reject_reason":"opposite side market/stop
  order exists"}`. Nothing caught it, so the raw SDK `APIError` travelled out of
  `RealAlpacaClient.submit_order`, out of `AlpacaBroker.submit`, through
  `Engine._step` and out of `PaperSession.run` — the same artifact loss ADR-0033 fixed
  for Ctrl-C, through a different door, on the *routine* path (the refusal only
  happens while an order is parked, i.e. every overnight and weekend session), and a
  breach of ADR-0017's "no SDK type escapes the seam". Now classified:
  `OrderRejectedError` + `_classify_order_error`, discriminating on **Alpaca's own
  error taxonomy** rather than a message substring — every refusal of a specific order
  carries an eight-digit numeric `code` (403/`40310000` wash trade & insufficient
  buying power, 422/`42210000` unknown asset & fractional short) while a bad key
  answers `401 {"message": "unauthorized."}` with **no code**, so a 4xx-that-is-not-
  401/429-with-a-code is a refusal and everything else propagates (ADR-0028's "the
  broker said no" vs "we could not ask", biased toward propagating: a run that cannot
  trade must stop, not narrate). `AlpacaBroker.submit` records it as `(Order, reason)`
  carrying the venue's words verbatim, nothing enters `_pending`, and the next bar may
  retry. `FakeAlpacaClient` gains side-scoped `set_submit_refusal` /
  `set_submit_failure` — a fake that could never say no is exactly why the live test
  asserted an exit the venue refuses. **ADR-0036's "a working BUY can never block a
  SELL" is narrowed**: true of our guard (side-keyed, structural), false of the system
  — a parked entry blocks the exit at the venue until it settles, which costs nothing
  while the venue is shut but is real intraday, and is one more argument for KAN-678.
  16 new fast tests + 5 live (double-gated, skip when the market is open). Account left
  flat and checked: no positions, no working orders, $100,000.06.
  **Separately, and NOT fixed:** Alpaca has **stopped applying split adjustments**.
  On 2026-08-04 AAPL 2020-08-25 came back 499.30 raw / 121.08 adjusted; on 2026-08-08
  the same call returns 499.30 / **484.31** (ratio 1.031 = dividends only), and a
  window spanning the 4:1 split shows the *adjusted* series still carrying a bare
  price cliff (484.24 → 125.17). That is ADR-0008's phantom-split hazard arriving
  through `--source alpaca` while the API still answers `adjustment=all` — ADR-0040's
  lesson again. The two `TestRealBars` split tests are **left red on purpose**
  (weakening them would hide an honesty regression; they skip in CI, so they gate
  nothing). Needs its own slice.
- **Fill divergence — is the modelled 5 bps real? (offline-verified, ADR-0038):**
  `divergence.py` answers the one question no backtest can. `ShadowBroker` is a
  **`Broker` decorator** (no engine change, no `if paper:` — ADR-0002 intact) that
  forwards `portfolio`/`submit`/`on_bar`/`rejections` to the live broker verbatim and
  replays the same orders through a throwaway `SimulatedBroker` seeded from a *copy*
  of the pre-bar live book. The counterfactual is deliberate: the reference price is
  the **next bar's open** — exactly what `SimulatedBroker` fills at — so both the
  realized and the modelled fill divide by the *same* reference and `realized −
  modelled` is a statement about the cost model, not about which bar was picked. The
  reference is captured once and never re-anchored if the venue settles bars later.
  Price notion follows the feed and is printed (`--live` is RAW per ADR-0021; the
  `--once` replay materializes adjusted bars and says so) — a raw fill measured
  against an adjusted open is meaningless arithmetic that still prints a number.
  Latency is *observation* latency off the injected `Clock` (never `time.time()`), an
  upper bound because a polling broker only notices a fill when it polls. Nothing is
  dropped: a venue rejection vs a modelled fill, a modelled funding rejection vs a
  venue fill, a partial fill (ADR-0033) as one `partial` row, and an order still
  parked at the venue (ADR-0036) as `pending` are all rows. **The shadow cannot
  perturb the live path, structurally:** the live call runs first and unguarded, all
  shadow work is inside `try/except Exception` that disables the shadow and records
  the failure, and the counterfactual holds a copy with no client. Proved by running
  the same strategy through a plain broker and through a `ShadowBroker` whose shadow
  raises on every call and asserting the two `BacktestResult`s are **equal**; the null
  test (simulated vs its own model → exactly zero divergence) calibrates the
  reference price. CLI `trading paper --divergence` writes `fill_divergence.csv` and
  prints the block; **off by default**, with a CLI test asserting `equity_curve.csv`
  and `result.json` are byte-identical with and without the flag. Below
  `MIN_PAIRED_FILLS = 30` the verdict says the model is "neither confirmed nor
  refuted" (ADR-0029's spirit). **Run live against the paper account (2026-08-08,
  venue shut):** `AAPL buy 0.01 — live pending | model filled @ 311.5507` off a raw
  open of `311.395`, i.e. exactly 5.00 bps, with the parked-order case (ADR-0036) as
  the reported divergence and the verdict correctly refusing to conclude anything;
  account left flat and checked. Still unverified: a *filled* live order (needs the
  venue open — covered offline, and the live test asserts it when the market is
  open), so there are **zero real paired fills** behind the 5 bps question so far.
  Wanted next: a `divergence` block in `result.json` + a dashboard panel (additive;
  `divergence_rows` already emits the flat shape).
- **Is the Sharpe a measurement or a point estimate? (2026-08-08, ADR-0039):** every
  headline figure the bench printed — Sharpe, Sortino, Calmar, alpha — was a point
  estimate rendered to two decimals, a format that reads as a measurement. `metrics.py`
  now qualifies it three ways. **A stationary block bootstrap** (`sharpe_confidence_interval`,
  Politis & Romano: geometric block lengths, default 60 bars) brackets the Sharpe with a
  percentile interval; resampling *individual* returns instead would destroy the serial
  structure a trend edge consists of and hand back a **narrower**, more confident answer
  from less information (measured on the AR(1) fixture: width 2.975 with 60-bar blocks vs
  1.966 i.i.d. — the inequality is pinned by a test). **A paired win rate**
  (`paired_bootstrap`) answers "beats the benchmark in X% of resamples" by drawing **one**
  index sequence per resample and applying it to *both* series — resampling them
  independently compares the strategy in one imaginary market against the benchmark in a
  different one, and the guard fixture (strategy = benchmark + a constant, so its Sharpe is
  higher *by construction*) drops from 1.0 to 0.585 the moment you do. **Trial deflation**
  (`SweepSummary.trial_count` / `deflated_winner()`, Bailey & López de Prado's
  `expected_max_sharpe` + `probabilistic_sharpe_ratio`) scores a search's winner against
  the Sharpe the luckiest of N skill-free trials would have shown — and the correction
  bites through the candidates' *spread*, not merely their count. Honesty rails throughout:
  below `MIN_BOOTSTRAP_OBSERVATIONS = 30` return periods there is **no** interval and a note
  saying why; a short series gets its block length cut and the reduction is printed, not
  hidden; an interval straddling zero says in words that the sample cannot distinguish the
  strategy from having no edge; and the trial count **always** prints the caveat that it
  covers one invocation and is a **LOWER BOUND** (`metrics.trial_count_note`, shared by the
  backtest and sweep paths so the sentence cannot drift). Determinism is part of the API:
  every entry point takes an explicit `seed` (`DEFAULT_BOOTSTRAP_SEED = 20260808`), builds
  its own `random.Random`, never touches the module-global RNG, and prints the seed.
  **Wired to the CLI (KAN-675):** `backtest --bootstrap` (plus `--bootstrap-resamples` /
  `--bootstrap-seed`) computes the block **once** and hands it to both `summarize` and
  `write_result_json`; it is **off by default** because the 1,000-resample default costs
  ~2.7 s on a 21-year daily run, and neither `summarize` nor `result_to_dict` ever derives
  it internally — writing a `result.json` must not silently pay for a bootstrap nobody
  asked for. Without the flag the summary and `result.json` are byte-identical to before
  (pinned by a literal golden in `test_report.py` and a CLI-level identity test).
  `sweep` needs no flag: it already ran every trial and kept each one's `ReturnMoments`, so
  the winner's deflation is free arithmetic and prints under the ranking table always.
  `result.json` gains one additive top-level `significance` key (null when not requested),
  so `RESULT_SCHEMA_VERSION` stays **1**.
- **CI's merge path no longer leaves the machine (2026-08-08, ADR-0040):** the
  required `integration` job made live yfinance calls, so on 2026-08-08 an upstream
  `YFRateLimitError` while landing PR #40 blocked a merge that had nothing to do with
  the code — and a rate limit is likeliest exactly when the repo is busiest, since
  every PR run spends more request budget. The job is now **entirely offline** and a
  second job, `integration-network` (`pytest -m network`, nightly `schedule` +
  `workflow_dispatch`, **never on `pull_request`, never a required check**), holds the
  one test that is live by definition: the provider-contract check that yfinance still
  returns the OHLCV columns the adapter parses. The boundary is a **marker**, not a
  path — `network` layers tests by *what they can block*, and the fast gate deselects
  it too. The ADR-0008 phantom-split guard runs off a committed 12 KB cache CSV
  (`tests/fixtures/yfinance_cache/AAPL_20200601_20201201_adj.csv`; 2020-06..2020-12 is
  five years past and immutable) with a stub fetcher that **raises if called**, so a
  missing fixture cannot silently fall back to the network. Two worse problems fell
  out. **(1)** ADR-0032's premise was wrong: `yf.download` catches *every* per-ticker
  exception and returns an empty frame, so a 429 reached the engine as
  `REASON_NO_BARS` — `EmptyUniverseError: … not listed in this window` — meaning a
  provider refusal read as a data regression and, worse, a real break read as a flake
  to re-run. `_default_fetch` now probes an empty response through `Ticker.history`
  (which re-raises `YFRateLimitError` unconditionally while a genuine absence stays
  empty) and raises `ProviderRefusedError` → `REASON_FETCH_FAILED`; classification is
  by **exception type**, never by matching log text. **(2)** the split guard never
  discriminated: with the default `max_position_pct = 0.25` an *unadjusted* series
  bottoms out at −25.3%, inside the −35% floor it asserts, so the test passed on raw
  prices. It now runs fully invested (−8.0% adjusted vs −73.9% unadjusted) and a
  sibling test de-adjusts the same fixture by the known 4:1 ratio to prove the floor
  trips. Verified in a network namespace with connectivity removed: the required layer
  passes, the `network` layer fails.
- **NOT yet built:** tick frequency and other asset classes (each its own ADR).
  Real Alpaca paper/live-quote runs need `uv sync --extra alpaca` plus
  `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in the environment (see `.env.example`);
  the dashboard server needs the `dashboard` extra (`uv sync --extra dashboard`).
  Also open: a
  **survivorship-bias-free point-in-time universe** (ADR-0027 records the gap; the
  `--source csv` path is the hook), **per-bar rolling liquidity** (the ADV screen is
  point-in-time, judged once before the run), **parameter-stability / heatmap output**
  from a sweep, and **regime-split metrics**. The **paper-vs-simulated fill
  divergence report** is built (ADR-0038) but has only ~one live paired fill behind
  it — the mechanism is done, the *evidence* about 5 bps is not; its `result.json`
  block and dashboard panel are still unbuilt (additive; `divergence_rows` already
  emits the flat shape). Three ADR-0039 gaps stay open too: `paper` has no
  `--bootstrap` (only `backtest` does), `--folds` walk-forward prints no deflation of
  its own, and there is **no cross-invocation trial ledger** — the tool sees one
  command, so an operator who tried six strategies by hand has made 36 trials and the
  tool will report 1. It says so every time; it cannot do better alone.

If code and prose disagree, the code wins — update the prose.

## Commands

```bash
make setup          # uv sync --frozen + install the pre-push hook (run once)
make check          # FAST GATE: lint + type-check + no-infra tests (what pre-push runs)
make test           # fast test layer only (no network)
make test-integration  # integration layer, OFFLINE (optional extras / broker creds; the required CI job)
make test-network   # live provider-contract layer (hits yfinance). Nightly in CI; never gates a merge
make test-all       # every layer
make audit          # dependency vulnerability scan
make ci-local       # everything on CI's merge path, locally (the six required checks)
```

Run one test: `uv run pytest tests/unit/test_types.py::TestPortfolioAccounting`.

## How work is done here (conventions)

- **Branch per slice off fresh `main`; PR-only.** No direct pushes to `main`; name
  branches `claude/<slice>`. Parallel lanes get their own git worktree so
  in-flight branches never collide, and the landing is serialized through one
  integration commit so `main` stays reviewable.
  **Now platform-enforced, as of 2026-08-04.** Branch protection on `main` is on
  and `enforce_admins` is **true**, so a direct push is rejected for everyone
  including the repo owner — the previous caveat ("not actually enabled … do not
  rely on the platform") is obsolete. Required to merge: all six CI checks
  (`lint`, `typecheck`, `unit`, `integration`, `build`, `security`), a PR (0
  approvals, so a solo maintainer can self-merge), a branch up to date with `main`
  (`strict`), linear history, and resolved conversations. Force-pushes and branch
  deletion are blocked. Inspect with
  `gh api repos/:owner/:repo/branches/main/protection`; the escape hatch, if a
  required check can never pass, is to edit protection — an admin can still do
  that, which is what keeps `enforce_admins: true` from deadlocking a solo repo.
- **Fast gate before every push.** `make check` must pass; the pre-push hook runs
  it. Bypass only with a scoped reason via `git push --no-verify`.
- **Layer tests by cost.** Fast layer = no infra, runs everywhere. Integration
  (`@pytest.mark.integration`) and e2e are CI-only; never let them gate a push.
- **A required check may not depend on a third party** (ADR-0040). `integration` is a
  required check, so it is **offline**: anything that talks to a service we do not
  control is marked `network` on top of `integration` and runs in the nightly,
  non-required `integration-network` job. Never add `integration-network` to branch
  protection — it does not run on PRs, so requiring it deadlocks every merge.
  Immutable historical data belongs in a committed fixture
  (`tests/fixtures/yfinance_cache/`), not in a live fetch on every run.
- **Every bug and flake becomes a failing test first, then a fix.** A regression
  seen twice is a missing test.
- **Prove a guard by watching it fail.** Break what a safety/compat test protects,
  see it go red, then restore — non-destructively (commit or `git stash` first;
  never `git checkout --` over uncommitted work).
- **Adversarial review before merge.** Read the diff to break it; prefer a public
  seam over a private reach.

## Domain invariants (do not regress)

- **No look-ahead:** an order submitted on bar *t* fills no earlier than *t+1*;
  `StrategyContext` never exposes future bars (ADR-0001).
- **Adjusted prices** for backtests — total return, no phantom split crashes
  (ADR-0008). **Raw actual quotes for paper/live** — the price notion is a per-mode
  feed policy, not shared; never mark a raw account on adjusted prices (ADR-0021).
- **Frequency is an adapter property, not a `get_bars` argument** — the protocol and
  the engine per-bar step never learn the interval; act on a bar only once complete
  (`ts+interval`); daily stays byte-identical (ADR-0022).
- **A bar belongs to a symbol and a timestamp, not to a request** — any adapter's
  overlapping ranges must agree bar-for-bar on the timestamps they share; a sub-range
  is a slice of its parent, never a re-anchored replay (ADR-0030).
- **Guardrails are enforced, not advisory:** position/exposure caps and the
  drawdown kill switch can veto or clamp orders (ADR-0009). The halt latches for the
  whole run **unless** recovery is configured, and recovery is off by default
  (ADR-0013 as amended by ADR-0031); exits are allowed while halted, always.
- **One execution path:** backtest and paper differ only in feed and clock;
  never fork strategy/broker/portfolio logic between them (ADR-0002).
- **No implicit shorting; fractional-share quantities allowed** (ADR-0011).

## Layout

```
src/trading/
  types.py                 # core value types (implemented, tested)
  interfaces.py            # DI seams: DataAdapter, Broker, Strategy, RiskGuardrails
  config.py                # BacktestConfig, CostConfig (defaults: $1,000, 5 bps)
  engine.py                # shared per-bar step + Engine.run (backtest) + PaperSession (V5)
  broker.py                # SimulatedBroker + CostModel
  brokers/alpaca.py        # AlpacaBroker — submit-then-poll paper broker (ADR-0020);
                           #   refuses a duplicate while a same-side order is working (ADR-0036);
                           #   records a venue refusal at submit instead of dying (ADR-0041)
  report.py                # text summary + equity_curve.csv + result.json (result_to_dict, ADR-0023);
                           #   absent-symbol caveat lines + additive `absent` key (ADR-0032)
  divergence.py            # ShadowBroker: live-vs-modelled fill comparison + report (ADR-0038)
  cli.py                   # `trading backtest / paper / gen-data / sweep / dashboard / verify-universe`
                           #   (--source, --broker, --interval, @basket, --min-adv, --folds, --data-feed,
                           #    --divergence, --bootstrap);
                           #   _run_benchmark warns instead of aborting on a bad --benchmark (ADR-0032)
  sizing.py                # target-weight → fractional-share orders (V2)
  clock.py                 # Clock seam: WallClock / ImmediateClock / FakeClock (V5)
  frequency.py             # Frequency value: label/delta/periods_per_year — interval abstraction (ADR-0022)
  dashboard/               # web dashboard (ADR-0023): payload + static_export (stdlib) + server (lazy FastAPI)
  data/fake.py             # in-memory adapter for the fast test layer
  data/yfinance_adapter.py # cached, adjusted yfinance adapter (injectable fetcher)
  data/synthetic.py        # deterministic GBM adapter, daily+intraday — offline (ADR-0012/0022);
                           #   range-independent: one canonical series from EPOCH (ADR-0030)
  data/csv_adapter.py      # bring-your-own-data OHLCV CSV DataAdapter (--source csv)
  data/alpaca_client.py    # AlpacaClient seam + Fake/Real clients (ADR-0017/0018);
                           #   terminal order statuses (ADR-0033) + feed choice (ADR-0034)
                           #   + cancel_order, the seam's 6th call (ADR-0036)
                           #   + OrderRejectedError: a submit-time venue refusal (ADR-0041)
  data/alpaca_adapter.py   # DataAdapter over Alpaca bars; per-call adjusted (ADR-0021) + interval (ADR-0022)
  data/recent_window.py    # completed-bars feed for paper; per-mode raw (ADR-0021) + interval completeness (ADR-0022);
                           #   per-symbol fetch guard: retry forever, escalate, never quarantine (ADR-0035)
  strategies/              # buy_and_hold, sma_crossover, equal_weight, momentum, mean_reversion, cross_sectional + registry
  universe.py              # curated baskets (blue20) + @name expansion (ADR-0024) + broker verification (ADR-0028)
  liquidity.py             # ADV screen over a pre-backtest formation window — no look-ahead (ADR-0029)
  metrics.py               # perf metrics: return, Sharpe, Sortino, Calmar, drawdown, turnover, exposure,
                           #   entry count + trades-per-parameter significance (ADR-0029);
                           #   benchmark-relative beta/alpha/correlation/IR (ADR-0037);
                           #   Sharpe significance (ADR-0039): stationary block bootstrap CI,
                           #   paired win rate, deflated Sharpe — seeded, never the global RNG
  sweep.py                 # parameter sweep (ADR-0016) + true IS->OOS walk-forward (ADR-0026);
                           #   trial_count + deflated_winner() — best-of-N is not a finding (ADR-0039)
tests/
  unit/           # fast, no infra
  integration/    # marked; needs network/yfinance (CI-only)
docs/adr/         # one decision per file
```

Optional extras: `plot` (matplotlib PNG), `dashboard` (fastapi/uvicorn — `uv sync
--extra dashboard`), `alpaca` (alpaca-py, the live-trading SDK — `uv sync --extra
alpaca`, plus paper credentials in the environment; ADR-0018).
