# ADR-0018: Alpaca credentials via env, and the SDK as an optional lazy dependency

- Status: Accepted
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

## Context

`RealAlpacaClient` (ADR-0017) talks to a real brokerage: it needs an API key and
secret, and it needs the `alpaca-py` SDK installed. Neither belongs in the fast
gate. This sandbox has no network and cannot resolve packages, `alpaca-py` ships
no type stubs, and secrets must never be committed or required to run the offline
bench. The bench already treats yfinance as an injectable, cached network path and
matplotlib as a lazily-imported optional extra; live-trading credentials and the
trading SDK deserve the same discipline. This ADR settles how the secrets are
read and how the SDK stays optional.

## Decision

**Credentials come from the environment, never the repo.** `RealAlpacaClient`
reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` from `os.environ` (a `paper` flag
selects the paper-trading endpoint), with an option to pass them explicitly to the
constructor for tests or alternate wiring. A minimal, dependency-free `.env` read
is acceptable, but adds **no** new package. When either credential is missing the
constructor raises a clear `ValueError` naming the two variables. No key, secret,
or `.env` file is ever committed.

**The SDK is optional and imported lazily.** `alpaca-py` is **not** added to
`[project.dependencies]` or `[project.optional-dependencies]`, and `uv.lock` is
untouched, so the fast gate installs and runs without it. Every `import alpaca...`
lives **inside** `RealAlpacaClient.__init__` and its methods; importing
`trading.data.alpaca_client` therefore never requires the SDK. A missing install
surfaces only when someone constructs the real client, as
`ImportError("alpaca-py is required for live trading; pip install alpaca-py")`.

**The strict type gate tolerates the un-stubbed, un-installed SDK.** A single
`[[tool.mypy.overrides]]` block (`module = ["alpaca.*"]`,
`ignore_missing_imports = true`) mirrors the existing `yfinance.*` override, so
`mypy --strict` passes with no SDK present. `RealAlpacaClient` converts every SDK
response into our own `Bar`/DTOs, so despite the SDK being untyped nothing leaks
`Any` past the wrapper's method boundaries.

**The fake sidesteps secrets entirely.** `FakeAlpacaClient` needs no key, no SDK,
and no network, so the whole fast test layer (and the coming adapter/broker lanes)
exercises the seam offline. `RealAlpacaClient` is verified by inspection and types
only; it is never constructed in this sandbox.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Add `alpaca-py` to core (or an extra) dependencies now | Cannot resolve or install in this offline sandbox, would touch `uv.lock`, and forces a live-trading SDK onto every install of an offline-first bench. |
| Import `alpaca-py` at module top | Makes importing the client module fail without the SDK, breaking the fast layer and any offline path that merely references the module. |
| A config file / CLI flag for the API key | Invites committing secrets and normalises keys living on disk; environment variables are the standard, review-safe channel. |
| A vendored `alpaca-py` type stub to satisfy strict mypy | High-maintenance for a dependency we import lazily and convert immediately; the scoped `ignore_missing_imports` override is the same, already-accepted treatment as yfinance. |

## Consequences

- The fast gate stays green with no SDK and no credentials; only a real
  live/paper run needs either, and a missing key or install fails loudly and
  early with an actionable message.
- Secrets stay out of the repo and out of process config; rotating a key is an
  environment change, not a code change.
- `RealAlpacaClient` is unexercised in CI's fast layer by design; its correctness
  rests on inspection, types, and a future integration/e2e layer gated on real
  credentials, never the fast gate.
- The `alpaca.*` mypy override is a deliberate, narrow hole: it silences missing
  imports for that package only, matching the yfinance precedent, and the wrapper
  still returns our concrete types so the hole does not widen into the callers.
