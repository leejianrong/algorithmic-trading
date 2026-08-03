# Questions

Statuses: `DECIDED` (user answered) · `ASSUMED` (default taken, correct it if
wrong) · `FORK` (waiting on the user) · `DEFERRED` (not needed this milestone).

## Open forks

None — round 1 closed all forks.

## Register

| ID | Question | Status | Answer or default | Landed |
|----|----------|--------|-------------------|--------|
| Q1 | Primary user / actors? | ASSUMED | Single strategy developer on their own machine; CLI/agent as automating actor; developer wins ties | PLAN §Users and actors |
| Q2 | Asset class / market? | DECIDED | US equities | ADR-0005 |
| Q3 | Bar frequency? | DECIDED (implied) | Daily for MVP; intraday later | ADR-0005 |
| Q4 | Build vs. buy the engine? | DECIDED | Custom event-driven engine | ADR-0001 |
| Q5 | Shared backtest/paper execution path? | DECIDED (implied by "both") | One engine; feed + clock are the only differences | ADR-0002 |
| Q6 | Historical data source? | DECIDED | yfinance, behind a pluggable adapter | ADR-0003 |
| Q7 | Paper-trading meaning? | DECIDED | Simulated broker in MVP; Alpaca paper API on roadmap | ADR-0004 |
| Q8 | Language / stack? | ASSUMED | Python 3.11+, pandas/numpy, yfinance, pytest, typer/argparse, uv/pip | PLAN §Implementation decisions |
| Q9 | Concurrency / conflict? | ASSUMED | Single-threaded, one run at a time, no concurrency | PLAN §Assumed defaults |
| Q10 | State & storage? | ASSUMED | In-memory per run; only data cache + result files on disk; no DB | PLAN §Assumed defaults |
| Q11 | External dependencies? | DECIDED | yfinance (+ pandas/numpy/matplotlib/pytest); Alpaca deferred | ADR-0003, ADR-0004 |
| Q12 | Runtime / deployment? | ASSUMED | Local Python CLI/library; no server or container in MVP | PLAN §Implementation decisions |
| Q13 | Config mechanism? | ASSUMED | TOML file + CLI flags (flags override) | PLAN §Assumed defaults |
| Q14 | Fill model? | ASSUMED | Market orders fill at next bar's open ± configurable slippage; commission applied | ADR-0004, PLAN §Assumed defaults |
| Q15 | Failure behaviour? | ASSUMED | Fail fast on data-fetch errors / missing symbols; reject underfunded orders leaving state unchanged | PLAN §Testing, SLICES V1 |
| Q16 | Versioning / migration? | DEFERRED | Cache format + strategy API stability; low concern until a second consumer exists | n/a |
| Q17 | Success metrics & Sharpe basis? | ASSUMED | Buy-and-hold reproduces a hand-computed return; Sharpe on daily returns with rf = 0; equity marked at close | PLAN §Testing, SLICES V3 |
| Q18 | Security / secrets? | ASSUMED | None in MVP (yfinance keyless); Alpaca keys via env/.env (already gitignored) when added | ADR-0004 |
| Q19 | Look-ahead prevention? | DECIDED (design) | Orders fill no earlier than the bar after they are submitted; `context` exposes no future bars | ADR-0001, SLICES V2 |
| Q20 | Live real-money trading? | DEFERRED | Explicitly out of scope; nothing in MVP can place a real order | PLAN §Scope (Out) |

## Coverage

| Category | Covered by |
|----------|-----------|
| Primary user and actors | Q1 |
| Scope boundary | Q2, Q3, Q7, Q20 |
| Data model and identity | Q2, Q3 (Instrument/Bar in ADR-0005) |
| State and storage | Q10, Q6 (cache) |
| Concurrency and conflict | Q9 |
| Interfaces and contracts | Q4, Q5, Q6, Q7 (Strategy/DataAdapter/Broker) |
| Failure behaviour | Q15, Q19 |
| External dependencies | Q11, Q8 |
| Runtime and deployment | Q12, Q8 |
| Measurable success | Q17 |
| Security and secrets | Q18 |
| Versioning and migration | Q16 |
