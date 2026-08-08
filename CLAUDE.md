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
  `canceled`, not yet watched), and **duplicate order stacking** — while orders sit
  parked the portfolio stays flat, so a target-weight strategy re-emits the same order
  every bar and the broker submits it again; needs its own slice (ADR-0036).
- **NOT yet built:** tick frequency and other asset classes (each its own ADR).
  Real Alpaca paper/live-quote runs need `uv sync --extra alpaca` plus
  `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in the environment (see `.env.example`);
  the dashboard server needs the `dashboard` extra (`uv sync --extra dashboard`).
  Also open: a
  **survivorship-bias-free point-in-time universe** (ADR-0027 records the gap; the
  `--source csv` path is the hook), **per-bar rolling liquidity** (the ADV screen is
  point-in-time, judged once before the run), **parameter-stability / heatmap output**
  from a sweep, **regime-split metrics**, and a **paper-vs-simulated fill divergence
  report** (the thing paper trading is actually for).

If code and prose disagree, the code wins — update the prose.

## Commands

```bash
make setup          # uv sync --frozen + install the pre-push hook (run once)
make check          # FAST GATE: lint + type-check + no-infra tests (what pre-push runs)
make test           # fast test layer only (no network)
make test-integration  # integration layer (needs network / yfinance)
make test-all       # every layer
make audit          # dependency vulnerability scan
make ci-local       # everything CI runs, locally
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
  brokers/alpaca.py        # AlpacaBroker — submit-then-poll paper broker (ADR-0020)
  report.py                # text summary + equity_curve.csv + result.json (result_to_dict, ADR-0023);
                           #   absent-symbol caveat lines + additive `absent` key (ADR-0032)
  cli.py                   # `trading backtest / paper / gen-data / sweep / dashboard / verify-universe`
                           #   (--source, --broker, --interval, @basket, --min-adv, --folds, --data-feed);
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
  data/alpaca_adapter.py   # DataAdapter over Alpaca bars; per-call adjusted (ADR-0021) + interval (ADR-0022)
  data/recent_window.py    # completed-bars feed for paper; per-mode raw (ADR-0021) + interval completeness (ADR-0022);
                           #   per-symbol fetch guard: retry forever, escalate, never quarantine (ADR-0035)
  strategies/              # buy_and_hold, sma_crossover, equal_weight, momentum, mean_reversion, cross_sectional + registry
  universe.py              # curated baskets (blue20) + @name expansion (ADR-0024) + broker verification (ADR-0028)
  liquidity.py             # ADV screen over a pre-backtest formation window — no look-ahead (ADR-0029)
  metrics.py               # perf metrics: return, Sharpe, Sortino, Calmar, drawdown, turnover, exposure,
                           #   entry count + trades-per-parameter significance (ADR-0029)
  sweep.py                 # parameter sweep (ADR-0016) + true IS->OOS walk-forward (ADR-0026)
tests/
  unit/           # fast, no infra
  integration/    # marked; needs network/yfinance (CI-only)
docs/adr/         # one decision per file
```

Optional extras: `plot` (matplotlib PNG), `dashboard` (fastapi/uvicorn — `uv sync
--extra dashboard`), `alpaca` (alpaca-py, the live-trading SDK — `uv sync --extra
alpaca`, plus paper credentials in the environment; ADR-0018).
