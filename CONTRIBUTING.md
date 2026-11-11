# Contributing

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```bash
make setup   # install locked deps + the pre-push hook (once)
make check   # fast gate: lint + type-check + no-infra tests
```

| Target | What it runs |
|--------|--------------|
| `make test` | Fast layer only; no network |
| `make test-integration` | Integration layer, **offline** (the required CI job) |
| `make test-network` | Live provider-contract checks; nightly in CI, never gates a merge |
| `make audit` | Dependency vulnerability scan |
| `make ci-local` | Everything on CI's merge path, locally |

Optional extras: `uv sync --extra dashboard` (web server), `--extra alpaca`
(live paper trading; needs `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, paper only —
see `.env.example`), `--extra plot` (matplotlib PNG).

## Workflow

- Branch per slice off fresh `main` (`claude/<slice>`); PR only. Direct pushes to
  `main` are rejected for everyone.
- The pre-push hook runs `make check`. Bypass one push with `--no-verify` and a
  scoped reason.
- Every bug becomes a failing test first, then a fix.
- A required CI check may not depend on a third party: anything live is marked
  `network` and runs only in the nightly job (ADR-0040).
- The invariants that must not regress, and the squash-against-your-own-base
  rule, are in [`CLAUDE.md`](CLAUDE.md). Decisions live in [`docs/adr/`](docs/adr/).
