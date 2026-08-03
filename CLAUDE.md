# Agent brief — algorithmic trading test bench

A test bench where one **event-driven engine** runs a strategy over US-equity
**daily** bars two ways — `backtest` and `paper` — with the same execution,
sizing, costs, and risk limits in both. Built as a stepping stone toward real
capital, so it favors honest numbers over flattering ones. Full context:
[`PLAN.md`](PLAN.md), slices in [`SLICES.md`](SLICES.md), decisions in
[`docs/adr/`](docs/adr/), decision register in [`QUESTIONS.md`](QUESTIONS.md).

## Build status — trust the code over these docs

As of this writing:

- **Done and tested:** project scaffold, quality gates, and the decided core
  value types — `Bar`, `Order`, `TargetWeight`, `Fill`, `Position`, `Portfolio`
  (`src/trading/types.py`); the DI-seam interfaces (`src/trading/interfaces.py`);
  and an in-memory `FakeAdapter` (`src/trading/data/fake.py`). Fast tests green.
- **NOT yet built:** the engine, the simulated broker, sizing, risk guardrails,
  concrete data adapters (yfinance/CSV), strategies, the CLI, and reporting.
  These are stubs-at-best. **Slice V1 in `SLICES.md` is the next work.**

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

- **Branch per slice off fresh `main`; PR-only; `main` is protected.** No direct
  pushes to `main`. (This session's designated dev branch is
  `claude/plan-new-project-l4n535`.)
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
  (ADR-0008).
- **Guardrails are enforced, not advisory:** position/exposure caps and the
  drawdown kill switch can veto or clamp orders (ADR-0009).
- **One execution path:** backtest and paper differ only in feed and clock;
  never fork strategy/broker/portfolio logic between them (ADR-0002).
- **No implicit shorting; whole-share quantities only.**

## Layout

```
src/trading/
  types.py        # core value types (implemented, tested)
  interfaces.py   # DI seams: DataAdapter, Broker, Strategy, RiskGuardrails
  data/fake.py    # in-memory adapter for the fast test layer
  # engine / broker / strategy / risk / cli / report  → V1 onward
tests/
  unit/           # fast, no infra
  integration/    # marked; needs network/yfinance (CI-only)
docs/adr/         # one decision per file
```
