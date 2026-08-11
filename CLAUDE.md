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
  **Separately:** an adjusted AAPL series came back carrying its 4:1 split. Recorded
  here at the time as "Alpaca has stopped applying split adjustments" — **that
  diagnosis was wrong in mechanism**, and ADR-0045 corrects it: the defect is one
  symbol's data, not the provider's pipeline. See the split-guard bullet below.
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
- **The benchmark stops sitting in cash (2026-08-08, ADR-0037 amended, KAN-672):**
  `--benchmark` runs `buy_and_hold` unconstrained, and `buy_and_hold` sized its one
  allocation from bar *t*'s close while the fill lands at bar *t+1*'s open plus 5 bps
  (ADR-0001/0004). An overnight gap up beyond the ~20 bps of `INVESTED_WEIGHT` headroom
  overshot the cash, the broker rejected — **recorded, not raised**, so
  `_run_benchmark`'s `except EmptyUniverseError` could not see it — and the strategy had
  already latched `_invested`, so **one unlucky bar left the benchmark 100% in cash for
  the whole run**, printed as a confident `Benchmark (SPY): +0.00%`. Measured: **22 of
  50** synthetic seeds over 2018. Newly urgent because ADR-0039's paired bootstrap reads
  that curve, silently turning "beats buy-and-hold" into "beats cash". The defect was the
  *one-shot entry*, not the arithmetic: the strategy now freezes the universe and weights
  on the first bar exactly as before but keeps the entry intent alive until the position
  exists, latching only then. A held leg is never re-targeted, so it stays buy-and-hold
  rather than becoming constant-mix, and **the first bar is byte-identical** (on a flat
  book equity *is* the cash, so the same `TargetWeight`s go out) — the entire fast suite
  passed unmodified. One wrinkle: on a *partly* established book, re-asserting a weight
  of equity demands cash the filled legs already spent, which measured **260 rejections
  in one 5-symbol run** and still never established the last leg; a retry there is
  therefore funded from the cash that actually remains, with the same headroom constant,
  and rounds to zero — submitting nothing — when the cash is gone. Worst case across the
  same 50 seeds: 5 rejections, every leg held. **22/50 flat → 0/50**, worst entry delay 7
  bars of 261. Separately and *independently of that fix*, `summarize` now prints a
  caveat under the `Benchmark (…)` line when the benchmark's peak exposure is zero ("the
  return on idle cash, not a market return") or when it held nothing until later than the
  first fillable bar, quoting the benchmark's own first rejection — which `summarize` had
  never looked at, counting only the strategy's — and `cli._run_benchmark` warns on
  stderr for the zero-exposure case. That guard is tested against a hand-built flat
  benchmark so it cannot go quiet as the strategy improves. `RESULT_SCHEMA_VERSION` stays
  **1** (nothing added). Known gap: nothing caps *how late* a benchmark may enter — the
  summary names the bar and leaves the judgement to the reader.
- **A live session stopped trading a week of history on its first poll (2026-08-08,
  ADR-0042, KAN-697):** `RecentWindowFeed.poll` asks for `[datetime.min, now]` and
  keeps the newest `DEFAULT_PAPER_LOOKBACK = 512` completed bars, and
  `PaperSession.run` treated every unseen timestamp as *fresh* — so on the **first**
  poll of a `--live` session all 512 backfill bars went through `Engine._step`, which
  runs the strategy, sizes, and **submits real orders**. Measured on the real live
  wiring simulated offline (`RecentWindowFeed` + `interval_is_complete` +
  `PaperSession` + a `FakeClock` that advances on `sleep_until`), `--interval 5m`,
  session opening 15:02: **58 orders on bars that had closed before the session
  started vs 5 on live bars**, the first one stamped eight days earlier. That poisons
  the one thing paper trading is for — a backfill order's reference price is a
  historical open while the venue fills today, so the ADR-0038 divergence sample was
  ~86% noise and still cleared `MIN_PAIRED_FILLS` to print a verdict. The two obvious
  fixes are both worse. **Skipping** the backfill starves the strategy: history
  accumulates *only* inside `_step`, so `sma_crossover` would need 20 live bars
  (100 min at 5m) and `cross_sectional` 121 before it could act. **Replaying with
  submission suppressed** desynchronizes it: strategies are transition-driven
  (`_long: dict[str, bool]`), so one fed history with its orders swallowed believes it
  is long against a flat book, sees no transition on the live bar, and sits flat all
  day *silently*. What ships instead: `engine.prime_history` loads warmup bars as
  **data** — history + `last_close`, with the strategy, sizer, guardrails and broker
  never invoked and **no `EquityPoint`** (the account held nothing then; a fabricated
  curve corrupts every metric). The boundary is the **first poll that reveals bars**,
  not "the first poll" (an opening fetch failure returns an empty feed under ADR-0035,
  which would just delay the bug one poll) and not a wall-clock cutoff (a bar
  mid-formation at startup is stamped *before* startup and would be skipped as
  warmup); a priming poll resets the empty-poll counter, and a bar arriving
  mid-session is never warmup. `PaperSession(warmup=True)` is the **default** — the
  safe one — and `trading paper --once` passes `warmup=live`, i.e. opts out, because
  replaying `[from, to]` and trading it *is* that mode. `--once` is byte-identical,
  proved not argued: same artifacts and stdout before/after, with the equity CSV's
  SHA-256 pinned as a golden; `Engine.run` is untouched and a backtest diffs clean
  against `origin/main`. The warmup is never silent — `warmup_bars` / `warmup_span` /
  `warmup_complete` on the session, plus a `run(on_warmup=...)` hook so the CLI's one
  line reaches stdout *and* `paper_session.log` the instant priming finishes — the
  session then sleeps to the next boundary, so announcing on the first live bar would
  leave a 1h session silent for an hour, indistinguishable from a hang. New
  `paper --lookback N` exposes the
  window (a **floor** under `--once`, so it can never truncate a replay).
  `RESULT_SCHEMA_VERSION` stays **1**. Known gaps: the warmup is not in `result.json`
  or the dashboard, and nothing checks the primed history is actually long enough for
  the configured strategy's lookback.
- **Pre-Monday hardening batch (2026-08-09, seven ADRs, six PRs):** everything below
  landed in one day to make the 2026-08-10 live divergence run survivable
  (`docs/monday-divergence-run.md`). Two of the seven were found *while verifying
  something else*, and both would have silently ruined the run.
- **A refusal reaches the bar it happened on (ADR-0044, KAN-679):** `Engine._step`
  diffed `broker.rejections` around `on_bar` **only**, so a refusal recorded at
  *settlement* reached that bar's `BarOutcome.broker_rejections` while one recorded at
  *submit* did not — which is exactly the duplicate-order guard (ADR-0036) and the
  venue's own veto (ADR-0041). Nothing was lost from the end-of-run artifacts
  (`_finalize` merges the broker's whole list), which is why it was called cosmetic —
  but **a `--live` session has no summary until it ends**, its per-bar status line is
  the operator's only real-time signal, and `cli._format_bar` has rendered
  `broker_rejections` since paper mode shipped. The field existed, the renderer
  existed, the engine never filled it in. Second gap, same fix: a refused order was
  still appended to `BarOutcome.submitted`, asserting that an order the venue never
  received had been placed. `_step` now diffs the rejection list around each `submit`
  too, **per order**, so a bar with one refusal and two acceptances reports exactly
  that, recording `checked` (never `order`) for the ones that got through. Reporting
  only — `_finalize` still merges the broker's list once. The backtest is untouched
  *structurally*, not merely by measurement: `SimulatedBroker.submit` only queues and
  has no rejection path, and `Engine.run` discards the `BarOutcome`. **`cli.py` needed
  no change.** 12 new fast tests; reverting the fix turns exactly 7 red.
- **A stopped session still writes its artifacts, and says what it did (ADR-0043,
  KAN-681/682):** nothing in `src/trading/` handled a signal, so the only graceful exit
  was `except KeyboardInterrupt` — i.e. SIGINT. `docker stop`, `systemd stop`, a reboot
  and a plain `kill` all send **SIGTERM**, whose default disposition kills the
  interpreter without unwinding, so `PaperSession.finalize()` never ran — the loss
  ADR-0033 exists to prevent, arriving through the *deployment mechanism*. Reproduced
  and then re-verified independently: `rc=-15` with only `paper_session.log` before,
  `rc=0` with all three artifacts and finalize in **0.09 s** after (Docker's grace is
  10 s). A handler raises `SessionTerminated`, a **`KeyboardInterrupt` subclass**, so
  ADR-0033's except-path catches it unchanged — one exit route, two names for what
  triggered it. **Raising, not a cooperative flag:** a live session sits inside
  `Clock.sleep_until` for a whole bar interval (5 min on Monday, an hour at `1h`), so a
  flag would be read long after SIGKILL landed. **A signal arriving during finalization
  is dropped** — truncating the `result.json` the first signal was honoured to save is
  strictly worse than taking a moment longer; `kill -9` remains the hard exit. Exit
  code is **0**: a clean stop must not read as a crash to a supervisor (EPIC-86).
  Installed by `trading paper` only, restored on exit, never at import. Alongside it,
  **logging is configured once, by the CLI callback** (`logging_config.py`, global
  `--log-level` / `--log-format text|json`): records to **stderr** in UTC while stdout
  keeps the per-bar report untouched, and the level governs the `trading` logger while
  the root stays at WARNING — **quieting is global, verbosity is ours**. Before this the
  package had one logger (ADR-0035) and no configuration, so its escalation warnings
  were unstamped and its recovery INFO line was invisible. Proved with a **real SIGTERM
  to a real subprocess**, which found a genuine bug: `getLogger(__name__)` is
  `"__main__"` under `python -m trading.cli`, outside the tree `--log-level` sets, so
  every lifecycle record vanished in exactly the deployment shape being fixed. Known
  gaps: **SIGHUP is unhandled** (closing the terminal still kills the run — use
  `nohup`/`tmux`), and the engine/guardrails/broker have no loggers of their own.
- **A phantom split reaches the bench through Alpaca (ADR-0045, KAN-694):** Alpaca's
  `adjustment=all` serves AAPL's 2020-08-31 bars with the 4:1 split **not** backed out
  — a bare **-74%** day inside the *adjusted* series, ADR-0008's oldest invariant broken
  through `--source alpaca`. **The premise everyone started from was wrong:** it is
  **one symbol's data, not the pipeline**. Measured independently, same day, same
  account — AAPL 4:1 → factor **1.0000** (not applied) while TSLA 5:1 → 5.0003, NVDA
  4:1 → 4.0001, AMZN 20:1 → 20.0001, GOOGL 20:1 → 20.0001. **TSLA split on the same
  session as AAPL and is correct**, so it is neither provider-wide nor date-scoped. And
  **Alpaca disagrees with itself**: its corporate-actions endpoint reports the split. So
  detection is sound rather than heuristic, and a blanket refusal would have been a
  permanent repo-wide tax for one ticker that sits in `blue20`. Every **adjusted** fetch
  is now cross-checked: `applied = (raw[pre]/adj[pre]) / (raw[post]/adj[post])` — the
  split ratio if applied, **1.0** if not, with the stock's own move **cancelling
  exactly**, so a ±30% ex-date gives the same answer. A failure raises classified
  `UnadjustedSplitError`, per symbol **and** per window, self-healing the day the
  provider is fixed. **RAW is never verified and costs no extra request** — an
  unapplied split is what raw *means* (ADR-0021) — which is why `paper --live` is
  untouched, confirmed by fetching AAPL across the split window raw: **bars returned, no
  raise, zero corporate-actions calls**. "We could not ask" (lookup failure) warns and
  passes bars through (ADR-0028's third bucket). Escape hatch is
  `AlpacaAdapter(verify_adjustments=False)`, a constructor param not a CLI flag. Seam
  gains a 7th call, `get_splits` → our own `SplitEvent`. A backtest routes the refusal
  through ADR-0032's guard into a loud caveat + `result.json`'s `absent`;
  **`paper --once --source alpaca` is affected and fails as a raw traceback** (that
  branch fetches unguarded — pre-existing, `cli.py`'s to fix). The two red `TestRealBars`
  assertions are **not weakened**, just retargeted at TSLA's working split; the AAPL
  state moved to a strict nightly `xfail`. Applying adjustments ourselves is feasible
  (the endpoint has the rates) and deliberately **not built**. Not yet reported upstream.
- **Alpaca is watched nightly, and CI holds its first live credentials (ADR-0046,
  KAN-695):** ADR-0040 built the contract mechanism and pointed it at one provider; its
  own closing line said Alpaca was uncovered. KAN-694 proved that real — the split
  adjustment vanished and **nothing noticed**, because every Alpaca test sat behind a
  credentials gate CI could never satisfy, so "skipped" and "passing" were
  indistinguishable. `tests/integration/test_alpaca_contract.py` (marked `integration`
  **and** `network`, so the required job never runs it) asserts adjusted-means-adjusted
  on a **working** split, the AAPL defect as a **strict `xfail`** that turns the nightly
  **RED the day Alpaca fixes it**, the corporate-actions endpoint ADR-0045 depends on,
  and bar shape + `get_asset`. Absent secrets skip cleanly and emit a `::warning` saying
  a green job means nothing. **Secrets are not yet added** — `ALPACA_API_KEY` /
  `ALPACA_SECRET_KEY`, **paper only**; the whole layer is read-only. **Never add
  `integration-network` to branch protection.**
- **The live feed asked for a window no provider would answer (ADR-0047, KAN-714) —
  THE Monday blocker:** `RecentWindowFeed.poll` asked for `[datetime.min, now]` — year 1
  to now — on the reasoning that a wide net cannot miss anything. **Alpaca answers that
  with an empty response**, not an error, so every symbol read absent, ADR-0035 recorded
  a legitimate-looking `REASON_NO_BARS`, and a `--live` session stopped on
  `max_empty_polls` having primed nothing and submitted nothing, while printing absence
  warnings that read as a venue outage rather than as our own request. Measured live
  (AAPL, IEX, raw): `datetime.min` → **0** bars at 1d and 5m, `1900-01-01` → 1,516 /
  121,662, `now-5d` → 4 / 348 — so **not** a data-plan limit, and **not a regression**:
  `_FAR_PAST = datetime.min` dates to V5 (PR #6) and only ever bit through Alpaca. Every
  offline test was green because `SyntheticAdapter` **clips** a `datetime.min` start to
  its 1990 epoch (ADR-0030 documents the clipping as deliberate) and `FakeAdapter`
  filters any range — **the stand-ins were more forgiving than the provider**, so a
  regression test written against them passes whether or not this is fixed (ADR-0040's
  lesson, second sighting). The same request made a 1-minute synthetic poll fabricate
  **3.7 M bars per symbol** (158.6 s → 0.1 s). `poll` now asks a bounded window sized by
  `fetch_span`, which pays two conversions — the 6.5 h session inside the 24 h day (a 5m
  bar is one of ~78/day, not 288) then `365/252` calendar days per session — and then
  `WINDOW_SLACK = 4`, so a source must be a **quarter** as dense as a market calendar
  before truncating the ADR-0042 warmup. The interval reaches the feed through the
  completeness policy that already carried it; an unstated policy gets the **widest**
  window (erring wide costs a fetch, erring narrow costs history). **A universe-wide
  clean-but-empty answer is now loud** — one ERROR naming the window asked for; twenty
  mega-caps do not delist on the same poll, and that silence is what hid this.
  Per-symbol absence is untouched. Verified live on the real Monday command with the
  venue shut: **`Warmup: primed 645 completed bar(s)`** where `main` said "no completed
  bars were available", no symbol absent, account left flat. `--once` byte-identical
  (four invocations hashed, including ADR-0042's golden); `cli.py` untouched. Known
  gap: nothing yet checks the primed history is long enough for the strategy's lookback
  (**KAN-702**) — this gives it a truthful number to check.
- **A crashed session keeps its measurement (ADR-0048, KAN-711):**
  `fill_divergence.csv` is the one artifact this bench produces that is **not**
  reconstructible — `paper_session.log` has the realized fills but no counterfactual, no
  reference price, no slippage — and every row lived in memory on the `ShadowBroker`
  until `finalize()`. ADR-0043 fixed the *signalled* half; it cannot reach `kill -9`, an
  OOM kill, power loss, or a suspending laptop (which the runbook warns about, there
  being no supervision). Reproduced with a real SIGKILL: the file was **absent
  entirely**; after, **227 rows on disk**. `DivergenceJournal` appends rows as they
  close, from `_flush_journal` at the end of `_observe` over what `_harvest` has just
  closed — and that placement **is** the late-settlement rule: a partial fill (ADR-0033)
  is amended inside attribution, before harvest, so the intermediate never hits disk; an
  order parked at the venue (ADR-0036) is not journaled at all and appears only if the
  session finalizes. A crashed file **under-reports; it never misreports**. It is a
  **byte prefix** of the finished file — independently confirmed, 177 surviving rows an
  exact prefix of 837 — so the survivor needs no tooling and is readable mid-session
  (what KAN-712 needs). `write_divergence_csv` and `_persist_state` now write a sibling
  temp file and `os.replace`. Journal I/O obeys ADR-0038's rule — after the live call,
  inside the same `try/except`, cursor advanced only on success — so a full disk
  disables the shadow rather than costing an order, proved by a journal that raises on
  every append producing an **equal** `BacktestResult`. A completed `--once` run is
  `diff -r` identical across all five artifacts. Cost: `fsync` ~1.6 ms per settling bar.
  Known gaps: `equity_curve.csv` is **not** incremental (deliberate — the session log
  already carries per-bar equity, its writer is shared with the backtest, and its rows
  gain a benchmark column at the end); the journal truncates the CSV at session *start*,
  so re-running into an occupied `--out` loses the old file earlier than before.
- **A live session tolerates silence in proportion to its interval (ADR-0049,
  KAN-671):** `PaperSession.run` stops after `max_empty_polls` consecutive polls
  revealing nothing new, the default is `2`, and `trading paper` overrode it only on the
  `--once` path — so the mode that runs for *weeks unattended* inherited a default
  written for a bounded offline replay, with no operator override. **A count is the
  wrong unit:** `2` polls is **ten minutes at `--interval 5m` and two days at `1d`**,
  which is why the card's two symptoms looked unrelated and were one bug. Measured on
  the real live wiring assembled offline: a 5m session hitting a **20-minute** gap at
  11:00 exited there with **17 live bars of a 77-bar day**, and a daily session started
  on a Thursday exited **Monday 00:00 UTC**. It exited *cleanly*; what was lost is the
  day. The tolerance is now a **duration** converted at the poll interval and floored in
  polls: `LIVE_SILENCE_TOLERANCE = 60 min`, `MIN_LIVE_EMPTY_POLLS = 4` (`1m → 60`,
  `5m → 12`, `30m/1h/1d → 4`). Tuned toward the **cheap error**: stopping late costs a
  dozen polls of a shut venue, stopping early costs the whole day. The floor was checked
  against the calendar rather than assumed — a normal weekend is **2** quiet daily polls
  and a three-day weekend **3**, so 4 clears both; four consecutive non-trading days
  would still end it, documented rather than handled. **The market calendar is
  deliberately not built** — that is KAN-687, it needs a new provider dependency, and a
  half-day it did not know about would end a session early, the exact failure being
  fixed. Chosen at `cli.py` where the live/replay distinction lives; the **diff to the
  shared `engine.py` is two hunks below `class Engine`** (a pure `silence_tolerance_polls`
  plus docstring), with no executable line inside `Engine`/`_step`/`Engine.run`/
  `_finalize`, and `Engine.run` has no empty-poll concept at all. New
  `paper --max-empty-polls N` overrides either path; a live session **announces its stop
  policy on startup** so an exit by policy is distinguishable from a hang. `--once`
  byte-identical (one disclosed exception: the ADR-0043 stderr lifecycle line gains a
  `max_empty_polls` field). `recent_window.py` untouched — an *empty poll* and an
  *absent symbol* are different conditions. **Monday's run now self-terminates about
  17:00–17:05 rather than 16:10–16:15, and is silent by design in between**; the runbook
  says so. Known gap: nothing tells the operator *why* it stopped.
- **The 5 bps assumption, measured at last (2026-08-10, ADR-0052):** the question the
  whole bench was built to answer. `sma_crossover` over `@blue20` at 5m ran live against
  the Alpaca paper account from 12:05 ET to the close — started 2h35m late and still
  cleared the bar: 53 bars, 63 orders, **60 paired fills** against `MIN_PAIRED_FILLS =
  30`. **Realized slippage 0.51 bps mean (median 0.59, stdev 3.75) against the model's
  5.00 — the cost model is conservative by ~4.5 bps**, with 54 of 60 fills better than
  modelled and both sides agreeing (buy +0.02, sell +1.20). So backtests have been
  **understating** returns, most for high-turnover strategies: the safe direction, and
  not a correctness bug. `slippage_bps` **stays at 5.0** on three grounds, all in the
  ADR: the measured mean is the same order as the IEX-vs-consolidated reference error
  (~0.4 bps on a mega-cap, ADR-0034), so the *level* is unresolved (95% interval −0.44
  to +1.46, though 5.00 sits 9.3 standard errors away, so "well below 5" is robust);
  **these are paper fills**, i.e. our cost model against *Alpaca's* fill model, since a
  paper account simulates rather than routes; and it is one afternoon, one venue, twenty
  mega-caps, ~$4,700 orders. KAN-618's sensitivity sweep is the honest next step, not a
  re-tuned constant. The run was clean — zero guardrail rejections, clamps, venue
  refusals, absent symbols, warnings or errors — and every weekend guard held: the
  bounded window primed 631 bars (ADR-0047), rows were durable as they settled
  (ADR-0048), the session self-terminated 60 min after the last bar (ADR-0049), and
  `make paper-live` survived a closed terminal (ADR-0051). Two things the run taught
  that no test had: the **parked-order case is persistent** — the session's NVDA sell
  was still working hours later, is the report's single outcome mismatch, and refused
  the flattening duplicate with the venue's own `held_for_orders` message through
  ADR-0041's classifier — and **a session ends holding its book plus any working
  orders**, so two stray BUYs would have rebuilt positions at the next open. Flattening
  is manual; the runbook now documents it. Known gaps the run exposed: no market
  calendar (KAN-687) let 6 extended-hours bars through `_step` (no orders on them, so
  the sample is clean — luck, not design), and a part-day run still prints
  `Sharpe 10.04` / `Annualized +35.25%` / `Turnover 107,106%` with nothing saying it is
  too short to annualize (KAN-705). (That annualization *caveat* is still unbuilt; the
  252-day calendar underneath it is fixed — see ADR-0054 below.)
- **Crypto groundwork — the equity assumptions baked into shared code (2026-08-11, three
  ADRs, three PRs, EPIC-87 phase 1):** deliberately landed **before** any crypto adapter
  exists, because the failure they prevent is silent: a first crypto backtest would
  annualize a 365-day market on a 252-day calendar under equity-tuned risk caps and print
  a confident number. All three are **library seams only — nothing is wired to the CLI**
  (`cli.py` and `engine.py` are untouched by all three), so a crypto run built through
  today's CLI still gets every equity default; the selection surface is one integration PR
  still to come. The whole batch is byte-identical to `cfb4d85` across a daily backtest, a
  **5m** backtest and a `paper --once` (8 artifacts hashed, plus `diff -r` on the paper
  `--out`) — verified per lane *and* on merged `main`, since three lanes each clean alone
  do not guarantee clean together.
- **A 24/7 daily bar closes at UTC midnight, and that was a choice (ADR-0053, KAN-706):**
  `default_is_complete` calls a daily bar finished once the clock's UTC **date** passes the
  bar's — a *session* rule, coherent only because the venue closes, and inherited by
  accident by anything that does not. A market with no close has no session: its daily bar
  is a rolling 24-hour window and the instant it shuts is a convention. **No new policy was
  needed** — `interval_is_complete(timedelta(days=1))` (ADR-0022) already *is* that rule,
  because `ts + interval` needs no calendar. Swept minute by minute over three days: on a
  midnight-stamped bar the two rules agree at **every one of 4,320** instants; at 4h/8h/13h
  stamps they disagree for **240/480/780** minutes and **every** disagreement runs one way —
  the session rule says *complete* while the window has not elapsed, i.e. it is early by
  exactly the stamp's offset and would hand a 24/7 strategy a forming bar. For US equities
  the same rule errs **late** whatever hour the provider stamps (a 20:00/21:00 UTC close
  precedes the date rollover), which is why the equity default **stays**: the interval rule
  on a session-open-stamped bar would withhold it 13.5 h past the real close. So a
  continuous market **drops the daily special case** rather than gaining a policy, and no
  `continuous_is_complete` was added — it would be a one-line delegation and a second
  callable `_policy_interval` must recognise (ADR-0035's reuse rule). The production diff is
  **docstrings and comments only: the AST is identical once docstrings are stripped**. The
  seam is genuinely reachable — `is_complete` is a `RecentWindowFeed` constructor parameter.
  `fetch_span`'s hardcoded equity calendar was **assessed, not refactored**: at
  `lookback=512` it over-asks a 24/7 source by **5.79x** at 1d and **21.39x** sub-daily
  (`(24/6.5) x (365/252) x 4`), the safe direction, so a continuous lookback cannot be
  truncated — one cost named, ~**10,953** bars/symbol/poll is **two** provider pages, not
  ADR-0047's one. 27 new fast tests, none using `SyntheticAdapter` or `FakeAdapter` for a
  24/7 claim (ADR-0040's lesson, third sighting), with both stand-ins' equity-shaped limits
  pinned so the file cannot be simplified back onto them; mutating the interval boundary
  turns 12 red, mutating it *into* the date rule 9, tightening `WINDOW_SLACK` 5.
- **Annualization was the US-equity session, hard-coded (ADR-0054, KAN-705):**
  `frequency.py` carried `TRADING_DAYS_PER_YEAR = 252` and `REGULAR_SESSION_MINUTES = 390`
  and returned `252 * (390 / interval_minutes)`, so `periods_per_year` — the **single** knob
  behind Sharpe, Sortino, Calmar, annualized return, turnover, return per unit exposure,
  alpha, IR and every ADR-0039 significance figure — was one market's year by construction.
  A 24/7 venue trades **365 x 1440**: at 5m the factor should be **105,120** and the code
  gave **19,656**, a **5.3480x** error and **2.3126x** in every Sharpe — and because the
  interval cancels, that ratio is identical at 1h, 30m, 5m and 1m, not just 5m; daily is
  1.4484x / 1.2035x. New `calendar.py` owns the vocabulary — a frozen validated
  `MarketCalendar(name, days_per_year, minutes_per_day)` with `US_EQUITY` (252 x 390, the
  former constants *exactly*) and `CRYPTO_24_7` (365 x 1440), and a registry whose
  `get_calendar` **raises rather than falling back to equity**, because a silent equity
  default *is* the bug. `Frequency` carries its calendar as a defaulted fourth field, so a
  `"5m"` on 24/7 is **unequal** to a `"5m"` on equity and the two cannot be conflated by a
  dict key; the crypto path is **keyword-only** (`parse(label, *, calendar=...)`), which is
  why **`cli.py` needed no change at all** — pinned by a test that introspects the
  signature. **The direction of the error is not what it looks like:** the equity factor is
  the *smaller* one, so it shrinks the magnitude — a profitable strategy is *understated*
  (conservative) while a **losing** one is flattered. Measured: a −3.73% 5m month scores
  Sharpe **−8.34** on 252 x 390 against **−19.28** on 365 x 1440 (annualized −34.05% vs
  −89.21%). And since total return and max drawdown do not scale with `periods_per_year` at
  all, a mis-annualized report pairs an **honest drawdown with a Sharpe from another
  market's year** — incoherent rather than merely biased. Additive throughout: `metrics.py`
  already threaded `periods_per_year` as a plain float and the two old constants survive as
  views onto `US_EQUITY`, so `report.py`/`data/synthetic.py` needed no change and **no
  existing test or golden was modified**. Reverting the derivation turns **16** red, all 16
  in the new module and none in the other 1,108 — which is itself the finding: nothing the
  bench already had could see this defect.
- **The risk defaults were an equity posture, and crypto got a bounded halt rather than
  wider numbers (ADR-0055, KAN-709):** measured through the real `Engine` on a synthetic
  series at **80% annualized volatility** (four times the default, drift held equal so vol
  is the only changed variable), the drawdown latch tripped in **20 of 20 seeds** — median
  first halt bar **250 of 2,610** — and then spent a median **90.5%** of the run refusing
  entries: independently reproduced on three seeds at 88.6–96.7% of the run halted, first
  halts at bars 85/196/298, returns **+11.72% / +1.02% / −17.72%** with **327/318/356**
  rejections, against **0 of 3** halts at equity volatility. ADR-0031's measured failure,
  arriving in year one and unanimously instead of merely likely — and **the caps are not the
  defect**, clamping 1–13 orders in 2,610 bars. So **halt recovery stops being optional and
  no number is widened**: `RiskConfig.crypto()` differs from `RiskConfig.equity()` (which is
  exactly the existing defaults, named so that choosing a market is a choice) in **one
  field**, `halt_cooldown_bars = 30`, pinned by a test that diffs the two configs.
  `crypto(halt_cooldown_bars=None)` is a **`ValueError`** — a 24/7 posture whose switch is
  permanent is the thing the preset exists to prevent. **Widening was measured and
  refused**, and the measurement is stronger than the argument: `max_drawdown_pct = 0.50`
  with recovery produced **0 halts and +539.93%** (a disabled guardrail with extra steps)
  and `0.35` still latching gave **−13.41%**, while the calibrated posture — the *unchanged*
  0.20 threshold plus the cooldown — produced **8 bounded halt episodes and +578.94%**, so
  it beats the widened one on return *while keeping the guardrail live*. The cooldown's
  floor is **arithmetic**: under `(threshold / per-bar sigma)²` bars it re-arms inside the
  move that tripped it (**16** bars here), and 30 is the next legible unit above; return
  falls monotonically past 30, so a return-maximizer would pick 5, which is below the floor
  and refused — **the criterion picked the number, not the return column**.
  `halt_recovery_drawdown_pct` stays `None` on evidence: alone at this volatility it
  re-armed *nothing* (ADR-0031 §2's deadlock under OR). Exits stay allowed while halted,
  asserted end to end — all 44 rejections in a crypto-posture run are BUYs, **zero** SELLs.
  `risk.py` gains **no executable line** (AST identical once docstrings are stripped): a
  posture is a `RiskConfig`, so there is no `if crypto:` and no second path. **The honest
  limit, in the ADR and in the tests:** a GBM series at crypto-like volatility **is not
  crypto** — no fat tails, no regime breaks, worst single-bar portfolio loss 9.29% across
  26,090 returns where real crypto has 20% days — so this establishes the *shape* of the
  failure, not the right level, and `max_daily_loss_pct` is left **off** precisely because
  nothing here can size it. The posture beats the latch on 10/10 seeds but beats *no* halt
  on only 4/10, asserted as a coin flip in both directions so it can never be read as free
  return.
- **What phase 1 left open, deliberately:** nothing selects a market — no `--market` flag,
  so the three seams are reachable only from Python and a real-crypto `--source csv` run
  today still annualizes on 252 days, inherits the session completeness rule, and latches in
  its first year. `result.json` carries a bare interval label with **no market**, so
  `report._resolve_periods_per_year` parses it on the equity calendar; a crypto run must
  pass `periods_per_year=` explicitly at the `write_result_json` call site in `cli.py`. The
  same hardcoded 252 still sits in `risk.py` (`_TRADING_DAYS`, vol-target realized vol —
  latent only because `target_volatility` is off in both postures; on a 365-bar year it
  would allow a vol-targeted book **20.4% more gross** than it asked for) and in
  `data/recent_window.py` (`RTH_SESSION`/`CALENDAR_DAYS_PER_SESSION`). `halt_cooldown_bars`
  is a **count, not a duration** — ADR-0049's unit mistake again: 30 bars is six weeks of an
  equity calendar, 30 days of a 24/7 one and 2.5 hours at `5m`. And **`SyntheticAdapter`
  cannot generate 24/7 bars** (measured: weekday-only daily bars, intraday confined to
  13:30–20:00 UTC), so there is **no offline continuous fixture** — which matters because
  the required `integration` job must stay offline (ADR-0040) and a differently-shaped
  stand-in passes whether or not the bug exists. Filed as **KAN-830**, blocking the adapter.
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

Operator targets for a live paper session (ADR-0051) — needs the `alpaca` extra and
paper credentials:

```bash
make paper-preflight   # read-only pre-run checks; non-zero if the account is not clean
make paper-dryrun      # rehearse the exact live command into a scratch --out, stop at the first quiet poll
make paper-live        # launch the real run DETACHED (tmux, else setsid) into a fresh timestamped --out
make paper-stop        # SIGTERM so ADR-0043 finalizes; never SIGKILL
make paper-status      # artifacts, paper_state.json, and the tail of the running session's console
```

**Never launch a live session with bare `nohup`.** `uv run` installs its own SIGHUP
handler, which overrides the `SIG_IGN` `nohup` sets (measured: wrapper `SigCgt` has
bit 1 set, `SigIgn` does not), so a hangup in uv's first second kills the wrapper
before the child exists. `make paper-live` uses tmux or `setsid` instead.

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
- **Squash against your own base commit, never `origin/main`.** `git reset --soft
  origin/main` is the reflex for collapsing WIP into one commit, and it is **unsafe
  here by construction**: lanes land while other lanes are still working, so
  `origin/main` moves and the reset diffs your tree against a newer commit — staging
  **every file a sibling landed as a deletion**. Nothing warns you; the working tree
  looks right and only the index is wrong, so the PR silently reverts merged work.
  Record the base when you branch (`BASE=$(git rev-parse HEAD)`) and squash to that.
  **Two lanes hit this in one afternoon on 2026-08-09** — both caught it in
  `git status` before committing, which is the only thing that stood between it and a
  bad merge. So: check `git status` after any soft reset, and treat a deletion you did
  not make as a stop sign. Picking up a sibling's landed work is a *rebase*, a separate
  deliberate action from squashing your own history.
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
  never fork strategy/broker/portfolio logic between them (ADR-0002). A live paper
  session's opening window of already-closed bars is **history to prime, not a range
  to trade** — primed as data, with the strategy, sizer, guardrails and broker never
  invoked and no equity point recorded (ADR-0042). `_step` stays the only code that
  trades, in both modes.
- **No implicit shorting; fractional-share quantities allowed** (ADR-0011).
- **A live session must survive being stopped:** SIGTERM takes the same finalizing
  exit Ctrl-C does, and a signal arriving *during* finalization is ignored so the
  artifacts are written whole (ADR-0033 as extended by ADR-0043). Signal handlers and
  logging configuration are installed by the **CLI entry point only** — importing
  `trading` as a library must change neither. Beyond that, the one artifact that
  cannot be reconstructed (`fill_divergence.csv`) is written **as it settles**, never
  held to the end, and a crashed file is a byte prefix that under-reports rather than
  misreports (ADR-0048).
- **Ask a provider for a window it will answer:** never `datetime.min`. A request is
  bounded from `lookback × interval` with slack; an unbounded net does not catch more,
  it catches nothing (Alpaca answers an absurd start with an *empty response*, not an
  error — ADR-0047). And an offline stand-in that is more forgiving than the provider
  cannot test this: `SyntheticAdapter` clips, `FakeAdapter` filters, so a regression
  test written against either passes whether or not the bug exists (ADR-0040's lesson).
- **A live session's stop policy is a duration, not a poll count** — the same count
  means ten minutes at 5m and two days at 1d. Tune it toward the cheap error: stopping
  late costs a few polls of a shut venue, stopping early costs the whole day's
  measurement (ADR-0049).
- **The annualization basis belongs to the market, not to the module** — `periods_per_year`
  is the single knob behind every risk-adjusted figure, so a hard-coded 252 x 390 silently
  reports one market's year for another. It comes from a `MarketCalendar`, and a lookup that
  cannot resolve one **raises rather than falling back to equity** (ADR-0054). Note the
  direction before trusting a figure: the equity basis is the smaller one, so it understates
  a winner and **flatters a loser**, and because total return and max drawdown do not scale
  at all, a mis-annualized report pairs an honest drawdown with a foreign Sharpe.
- **Completeness is a per-market policy, and a market that never closes has no session** —
  a 24/7 daily bar is a rolling 24-hour window (`ts + interval`, closing at UTC midnight by
  convention), never "the UTC date has turned over". The equity session rule errs *late* and
  stays; the same rule on a continuous venue errs **early** and hands the strategy a forming
  bar (ADR-0053).
- **Calibrate a guardrail; never widen it until nothing trips.** A limit chosen so it stops
  firing is a disabled guardrail with extra steps, and it was measured to be worse on return
  as well as dishonest — a bounded halt beat a widened threshold both ways. Where a market's
  ordinary volatility makes a latch permanent, the fix is that **recovery stops being
  optional**, not a looser level (ADR-0055). Exits stay allowed while halted, always.

## Layout

```
src/trading/
  types.py                 # core value types (implemented, tested)
  interfaces.py            # DI seams: DataAdapter, Broker, Strategy, RiskGuardrails
  config.py                # BacktestConfig, CostConfig (defaults: $1,000, 5 bps)
  engine.py                # shared per-bar step + Engine.run (backtest) + PaperSession (V5);
                           #   prime_history: a live session's opening window is warmup, not
                           #   orders — data only, no strategy/broker/curve (ADR-0042);
                           #   _step diffs broker.rejections around submit too (ADR-0044);
                           #   silence_tolerance_polls: paper-only, below `class Engine` (ADR-0049)
  broker.py                # SimulatedBroker + CostModel
  brokers/alpaca.py        # AlpacaBroker — submit-then-poll paper broker (ADR-0020);
                           #   refuses a duplicate while a same-side order is working (ADR-0036);
                           #   records a venue refusal at submit instead of dying (ADR-0041)
  report.py                # text summary + equity_curve.csv + result.json (result_to_dict, ADR-0023);
                           #   absent-symbol caveat lines + additive `absent` key (ADR-0032);
                           #   flags a benchmark that never invested (ADR-0037 amended)
  divergence.py            # ShadowBroker: live-vs-modelled fill comparison + report (ADR-0038);
                           #   DivergenceJournal: rows appended as they settle, atomic writes (ADR-0048)
  cli.py                   # `trading backtest / paper / gen-data / sweep / dashboard / verify-universe`
                           #   (--source, --broker, --interval, @basket, --min-adv, --folds, --data-feed,
                           #    --divergence, --bootstrap, --lookback, --log-level, --log-format,
                           #    --max-empty-polls);
                           #   _run_benchmark warns instead of aborting on a bad --benchmark (ADR-0032);
                           #   owns the SIGTERM handler + logging config — a library owns neither (ADR-0043);
                           #   derives the live silence tolerance from the interval (ADR-0049)
  logging_config.py        # the one logging configuration, called only by the CLI entry point (ADR-0043):
                           #   stderr, UTC, text|json lines; --log-level governs `trading`, not the world
  sizing.py                # target-weight → fractional-share orders (V2)
  clock.py                 # Clock seam: WallClock / ImmediateClock / FakeClock (V5)
  frequency.py             # Frequency value: label/delta/periods_per_year — interval abstraction (ADR-0022);
                           #   carries its MarketCalendar; parse(label, *, calendar=…) (ADR-0054)
  calendar.py              # MarketCalendar: US_EQUITY (252x390) / CRYPTO_24_7 (365x1440) — the annualization
                           #   basis is a property of the market, and get_calendar raises rather than
                           #   defaulting to equity (ADR-0054)
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
  data/alpaca_adapter.py   # DataAdapter over Alpaca bars; per-call adjusted (ADR-0021) + interval (ADR-0022);
                           #   verifies an adjusted series really is adjusted; RAW never checked (ADR-0045)
  data/recent_window.py    # completed-bars feed for paper; per-mode raw (ADR-0021) + interval completeness (ADR-0022);
                           #   per-symbol fetch guard: retry forever, escalate, never quarantine (ADR-0035);
                           #   bounded fetch window — datetime.min is a request no provider answers (ADR-0047)
  strategies/              # buy_and_hold, sma_crossover, equal_weight, momentum, mean_reversion, cross_sectional + registry
                           #   buy_and_hold retries its entry until the position exists (ADR-0037 amended)
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
