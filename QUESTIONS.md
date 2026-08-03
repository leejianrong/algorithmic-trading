# Questions

Statuses: `DECIDED` (user answered) · `ASSUMED` (default taken, correct it if
wrong) · `FORK` (waiting on the user) · `DEFERRED` (not needed this milestone).

## Open forks

None — closed across two grill rounds.

## Register

| ID | Question | Status | Answer or default | Landed |
|----|----------|--------|-------------------|--------|
| Q1 | Primary user / actors? | ASSUMED | Single developer (newer to algo trading), heading toward real capital; CLI/agent and the guardrails are actors; developer wins ties, safety outranks the strategy | PLAN §Users and actors |
| Q2 | Asset class / market? | DECIDED | US equities | ADR-0005 |
| Q3 | Bar frequency? | DECIDED (round 2) | Daily — most forgiving and best free data; timestamp keeps intraday open | ADR-0005 |
| Q4 | Build vs. buy the engine? | DECIDED | Custom event-driven engine | ADR-0001 |
| Q5 | Shared backtest/paper execution path? | DECIDED (round 2) | One engine; feed + clock are the only differences; guardrails on the shared path | ADR-0002, ADR-0009 |
| Q6 | Historical data source? | DECIDED | yfinance, behind a pluggable adapter | ADR-0003 |
| Q7 | Paper-trading meaning? | DECIDED | Simulated broker in MVP; Alpaca paper API is the next milestone | ADR-0004 |
| Q8 | Language / stack? | DECIDED (round 3) | Python 3.13+, pandas/numpy, yfinance, pytest, typer/argparse, uv/pip | PLAN §Implementation decisions |
| Q9 | Concurrency / conflict? | ASSUMED | Single-threaded, one run at a time | PLAN §Assumed defaults (implied) |
| Q10 | State & storage? | ASSUMED | In-memory per run; only cache, results, and paper-session log on disk; no DB | PLAN §Scope, §Assumed defaults |
| Q11 | External dependencies? | DECIDED | yfinance (+ pandas/numpy/matplotlib/pytest); Alpaca next milestone | ADR-0003, ADR-0004 |
| Q12 | Runtime / deployment? | ASSUMED | Local Python CLI/library; no server/container in MVP | PLAN §Assumed defaults |
| Q13 | Config mechanism? | ASSUMED | TOML file (capital, costs, limits, universe) + CLI flags override | PLAN §Assumed defaults |
| Q14 | Fill model? | ASSUMED | Conservative: next-open ± configurable (realistically pessimistic) slippage; commission applied | ADR-0004, PLAN §Assumed defaults |
| Q15 | Failure behaviour? | ASSUMED | Fail fast on data errors / missing symbols; reject invalid orders leaving state unchanged | PLAN §Testing, SLICES V1 |
| Q16 | Versioning / migration? | DEFERRED | Cache format + strategy API stability; low concern until a second consumer exists | n/a |
| Q17 | Success metrics & Sharpe basis? | ASSUMED | Buy-and-hold reproduces a hand-computed total return; Sharpe on daily returns, rf = 0; equity marked at adjusted close | PLAN §Testing, SLICES V4 |
| Q18 | Security / secrets? | ASSUMED | None in MVP (yfinance keyless); Alpaca keys via env/.env (gitignored) at next milestone | ADR-0004 |
| Q19 | Look-ahead prevention? | DECIDED (design) | Orders fill no earlier than the bar after submission; `context` exposes no future bars | ADR-0001, SLICES V2 |
| Q20 | Live real-money trading? | DEFERRED | Explicitly out of scope; nothing in MVP can place a real order | PLAN §Scope (Out) |
| Q21 | End goal / rigor level? | DECIDED (round 2) | Stepping stone to real capital → realism and enforced guardrails are first-class | PLAN §Problem, §Users; ADR-0009 |
| Q22 | Portfolio breadth & sizing defaults? | DECIDED (round 2–3) | Multi-symbol portfolio from day one; target-percent-of-equity sizing; default capital $1,000 (small/realistic; $500 a config change) and default limits (25% position / 100% gross / 20% drawdown) in config | ADR-0006, ADR-0007, PLAN §Assumed defaults |
| Q25 | Whole-share vs fractional sizing at small capital? | FORK (deferred) | Whole shares for now; small capital likely forces fractional shares (own ADR, amends ADR-0007) — surfaces at V2 | PLAN §Open risks |
| Q23 | Price adjustment? | DECIDED (round 2) | Backtest on split/dividend-adjusted (total-return) prices; paper/live path trades on actual quotes later | ADR-0008 |
| Q24 | Risk guardrails: enforce or report? | DECIDED (round 2) | Full, enforced in-engine: per-order checks, position/exposure caps, drawdown/daily-loss kill switch; optional SPY benchmark in the report | ADR-0009, SLICES V3, V4 |

## Coverage

| Category | Covered by |
|----------|-----------|
| Primary user and actors | Q1, Q21 |
| Scope boundary | Q2, Q3, Q7, Q20, Q22 |
| Data model and identity | Q2, Q3, Q22, Q23 (Instrument/Bar/Portfolio in ADR-0005, ADR-0006) |
| State and storage | Q10, Q6 (cache) |
| Concurrency and conflict | Q9 |
| Interfaces and contracts | Q4, Q5, Q6, Q7, Q22 (Strategy/DataAdapter/Broker/sizing) |
| Failure behaviour | Q15, Q19, Q24 (guardrails) |
| External dependencies | Q11, Q8 |
| Runtime and deployment | Q12, Q8 |
| Measurable success | Q17, Q23 |
| Security and secrets | Q18 |
| Versioning and migration | Q16 |
