# Agent brief — algorithmic trading test bench

**What:** one event-driven `Engine` runs a strategy over US-equity (and, via
`--market crypto`, 24/7 crypto) bars two ways — `backtest` and `paper` — with the
same execution, sizing, costs and risk limits in both.
**Why:** a stepping stone toward real capital, so it favors honest numbers over
flattering ones. A flattering default is a bug here.

If code and prose disagree, **the code wins** — fix the prose in the same PR.

## Where things are (read on demand, not up front)

- [`docs/build-history.md`](docs/build-history.md) — dated log of everything built and
  measured. Append new entries there, never here. Search it before re-deriving a result.
- [`docs/adr/`](docs/adr/) — one decision per file (ADR-0001…). Cite the number when a
  change touches an invariant below.
- [`PLAN.md`](PLAN.md), [`SLICES.md`](SLICES.md), [`QUESTIONS.md`](QUESTIONS.md) — scope,
  slices, decision register.
- [`docs/research-playbook.md`](docs/research-playbook.md) — hypothesis-to-live-capital loop.
  [`docs/monday-divergence-run.md`](docs/monday-divergence-run.md) — live paper runbook.
- [`docs/anthropic-ipo/`](docs/anthropic-ipo/) — pre-registered plan for a manual, defined-risk put-spread trade
  on a post-IPO fade (ADR-0076/0077). Manual only: the bench has no options support.
- Code map: `src/trading/` — `engine.py` (shared `_step`, `Engine.run`, `PaperSession`),
  `broker.py`, `brokers/alpaca.py`, `risk.py`, `sizing.py`, `metrics.py`, `sweep.py`,
  `report.py`, `divergence.py`, `cli.py`, `data/*` adapters, `strategies/`. Explore the
  tree rather than trusting a list here.

## Commands

```bash
make setup             # uv sync --frozen + install the pre-push hook (once)
make check             # FAST GATE: lint + type-check + no-infra tests (pre-push runs it)
make test              # fast layer only
make test-integration  # integration layer, OFFLINE (the required CI job)
make test-network      # live provider-contract layer; nightly, never gates a merge
make test-all          # every layer
make audit             # dependency vulnerability scan
make ci-local          # everything on CI's merge path (the six required checks)
uv run pytest tests/unit/test_types.py::TestPortfolioAccounting   # one test
```

Optional extras: `plot`, `dashboard` (`uv sync --extra dashboard`), `alpaca`
(`uv sync --extra alpaca` + `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`, paper only; see
`.env.example`). CI type-checks twice, the second time with `alpaca` installed.

Live paper session (needs `alpaca` extra + paper creds): `make paper-preflight`,
`make paper-dryrun`, `make paper-live`, `make paper-stop`, `make paper-status`.
**Never launch a live session with bare `nohup`** — `uv run` installs its own SIGHUP
handler that overrides it; `make paper-live` uses tmux or `setsid`. Stop with SIGTERM
(`make paper-stop`), never SIGKILL. A session ends holding its book plus any working
orders; flattening is manual (see the runbook).

## Workflow conventions

- **Branch per slice off fresh `main`; PR-only**, branches `claude/<slice>`. Branch
  protection is enforced for everyone, admins included: six required checks (`lint`,
  `typecheck`, `unit`, `integration`, `build`, `security`), linear history, up to date
  with `main`. Parallel lanes get their own git worktree.
- **Squash against your own base commit, never `origin/main`.** `git reset --soft
  origin/main` stages every file a sibling landed as a *deletion* and the PR silently
  reverts merged work. Record `BASE=$(git rev-parse HEAD)` when you branch; check
  `git status` after any soft reset; a deletion you did not make is a stop sign.
- **`make check` before every push.** Bypass only with `--no-verify` and a scoped reason.
- **A required check may not depend on a third party** (ADR-0040). Anything hitting a
  service we don't control is marked `network` on top of `integration` and runs only in
  the nightly `integration-network` job. **Never add it to branch protection.** Immutable
  historical data goes in a committed fixture (`tests/fixtures/`), not a live fetch.
- **Offline stand-ins are more forgiving than real providers** (`SyntheticAdapter` clips,
  `FakeAdapter` filters). A regression test written only against them can pass whether or
  not the bug exists — assert the provider-shaped behaviour directly.
- **Every bug becomes a failing test first, then a fix.** A regression seen twice is a
  missing test.
- **Prove a guard by watching it fail:** break what the test protects, see red, restore —
  commit or stash first; never `git checkout --` over uncommitted work.
- **Adversarial review before merge.** Prefer a public seam over a private reach.
- Equity output must stay **byte-identical** unless a change says otherwise: hash the
  artifacts (`equity_curve.csv`, `result.json`, `paper_state.json`) before and after.
- `RESULT_SCHEMA_VERSION` stays **1**: add `result.json` keys additively, and omit (not
  `null`) optional blocks that were not requested.

## Domain invariants (do not regress)

- **No look-ahead:** an order on bar *t* fills no earlier than *t+1*; `StrategyContext`
  never exposes future bars (ADR-0001).
- **Adjusted prices in backtests** (total return, no phantom splits, ADR-0008); **raw
  quotes in paper/live** — never mark a raw account on adjusted prices (ADR-0021).
- **Frequency is an adapter property, not a `get_bars` argument.** Act on a bar only once
  complete (`ts+interval`); daily stays byte-identical (ADR-0022).
- **A bar belongs to a symbol and timestamp, not a request:** overlapping ranges from any
  adapter must agree bar-for-bar (ADR-0030).
- **Guardrails veto, not warn** (ADR-0009). The drawdown halt latches for the whole run
  unless recovery is configured (ADR-0013/0031). Exits are allowed while halted, always.
  **Calibrate a guardrail; never widen it until nothing trips** (ADR-0055).
- **One execution path:** backtest and paper differ only in feed and clock; no
  `if paper:` forks (ADR-0002). `_step` is the only code that trades. A live session's
  opening window of closed bars is **history to prime** — data only, no orders, no equity
  point (ADR-0042).
- **No implicit shorting; fractional shares allowed** (ADR-0011).
- **Costs belong to the market, and "free" is the most flattering wrong answer.** A market
  with no researched cost model is unselectable, never defaulted to equity's (ADR-0060).
  Never fold a venue fee into the fill price. Cost is a function of liquidity, not asset
  class (ADR-0052/0063).
- **The annualization basis belongs to the market** (`MarketCalendar`, ADR-0054);
  lookups **raise rather than fall back to equity**. Completeness is a per-market policy —
  a 24/7 daily bar is a rolling 24h window (ADR-0053). Selecting a market is one explicit
  `--market` flag that sets calendar + completeness + risk posture + costs (ADR-0057).
- **A derived statistic must share the basis of the population it is compared against**;
  a mixed-basis deflation raises (ADR-0059).
- **A live session must survive being stopped:** SIGTERM takes the same finalizing exit as
  Ctrl-C and a signal during finalization is ignored (ADR-0033/0043). Signal handlers and
  logging config are installed by the **CLI entry point only**. `fill_divergence.csv` is
  written as it settles; a crashed file under-reports, never misreports (ADR-0048).
- **Ask a provider for a window it will answer:** never `datetime.min` — Alpaca answers it
  with an empty response (equities) or one bar (crypto). Bound it from `lookback × interval`
  plus slack (ADR-0047/0058).
- **A live stop policy is a duration, not a poll count**; tune toward the cheap error
  (ADR-0049).
- **Report uncertainty, not point estimates:** Sharpe gets a block-bootstrap CI, a sweep
  winner is deflated by trial count (a lower bound), and below the minimum sample the
  report says so rather than printing a number (ADR-0029/0039/0062).

## Known live gaps (verify against code before relying on)

No market-calendar-aware sleep (KAN-687); `fetch_span` and `MIN_LIVE_EMPTY_POLLS` are still
equity-shaped; `halt_cooldown_bars` is a count, not a duration; survivorship bias is
documented and unfixed on price data (ADR-0027/0064); the fill-divergence report cannot see
the crypto fee (taken in quantity); a plain yfinance 404 reads as absence (ADR-0080).
