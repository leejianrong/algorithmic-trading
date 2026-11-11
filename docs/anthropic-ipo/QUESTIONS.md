# Questions

## Open forks
None. Round 1 closed.

## Register

| ID | Question | Status | Answer or default | Landed |
|----|----------|--------|-------------------|--------|
| Q1 | Deliverable | DECIDED | Memo + pre-registered rules; manual trade | PLAN |
| Q2 | Widen comp set | DECIDED | Yes; 21 verifiable names, not a complete >$1B list | ADR-0077 |
| Q3 | Structure | DECIDED | Bear put spread only | ADR-0076 |
| Q4 | Max loss | DECIDED | $500 | ADR-0076 |
| Q5 | Pre-register rules | DECIDED | Yes, use recommended | ADR-0077 |
| Q6 | Thesis (own vs trade) | DECIDED | Trade the post-IPO fade | PLAN |
| Q7 | Non-public info / conflict | ASSUMED | None; agent view is conflicted | PLAN |
| Q8 | Hype gate | ASSUMED | Day-2 close >= +25% vs offer | PLAN rules |
| Q9 | Spread/exit numbers | ASSUMED | 20% wide, 40% debit cap, 60% TP, day 22 | PLAN rules |
| Q10 | Comp offer prices | ASSUMED | Unverified vs prospectus | ADR-0077 |
| Q11 | IV for illustration | ASSUMED | 100%, guess | ADR-0077 |
| Q12 | Broker, options approval, jurisdiction | FORK | Candidate: Alpaca (the bench's broker, paper only so far). Docs say a put spread is a Level 3 multi-leg (`order_class: mleg`) order, available in paper and the API. UNKNOWN: whether the owner has a funded live Alpaca account and Level 3 approval, and their jurisdiction | PLAN S4 |
| Q13 | Bench support for options | DEFERRED | Out of scope (ADR-0011) | PLAN |

## Coverage

| Category | Covered by |
|----------|-----------|
| Primary user and actors | Q1, Q7 |
| Scope boundary | Q3, Q13 |
| Data model and identity | n/a: one trade, one journal |
| State and storage | Journal and chain CSV in git |
| Concurrency and conflict | n/a |
| Interfaces and contracts | n/a: manual |
| Failure behaviour | Gates and no-trade (Q8, Q9) |
| External dependencies | yfinance (comps only); broker (Q12) |
| Runtime and deployment | Manual |
| Measurable success | R0, R1; each gate is a number |
| Security and secrets | Q7; no credentials used |
| Versioning and migration | n/a |
