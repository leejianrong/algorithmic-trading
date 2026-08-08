# ADR-0018: Alpaca credentials via env, and the SDK as an optional lazy dependency

- Status: Accepted, amended 2026-08-04 (the SDK is now locked as an optional extra)
- Date: 2026-08-04
- Deciders: strategy developer (project owner)

> **Amendment (2026-08-04) — the SDK is locked, as an extra.** The decision below
> that `alpaca-py` is "**not** added to `[project.dependencies]` or
> `[project.optional-dependencies]`, and `uv.lock` is untouched" was correct for an
> offline sandbox and is **superseded**. The credentials-from-environment decision,
> the lazy-import rule, and the `alpaca.*` mypy override all stand unchanged. See
> "Amendment: locking the SDK" at the end of this file.

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
only; it is never constructed in this sandbox. *(Superseded 2026-08-04 — it is now
verified by execution against a paper account. Inspection-and-types turned out to
catch neither of the three runtime bugs nor the eight type errors that first
execution found; see the amendment below.)*

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

## Amendment: locking the SDK (2026-08-04)

### What changed

The build machine has a working network. The sole reason `alpaca-py` was left
unlocked — "cannot resolve or install in this offline sandbox" — no longer holds,
and leaving it unlocked had a cost that came due immediately (below).

**`alpaca-py` is now an optional extra**, not a core dependency:

```toml
[project.optional-dependencies]
alpaca = ["alpaca-py>=0.33"]
```

locked additively into `uv.lock` (`alpaca-py` 0.43.5 + `sseclient-py`; no existing
pin moved). It follows the exact pattern of the `plot` and `dashboard` extras, for
the same reason those are extras: an offline-first bench must not force a
live-trading SDK on every install. `uv sync --frozen` does not install extras, so
the fast gate and every offline path are untouched; a real run needs
`uv sync --extra alpaca`.

**What is unchanged:** credentials still come only from `ALPACA_API_KEY` /
`ALPACA_SECRET_KEY` in the environment, never the repo; every `import alpaca...`
still lives inside `RealAlpacaClient`'s methods, so importing the module still
never requires the SDK; and a missing install still fails loudly at construction.
A committed, key-free `.env.example` documents the two variables. `.env` stays
gitignored.

**The `alpaca.*` mypy override stays.** The SDK still ships no stubs for our
purposes and is still absent from a default install, so the override still earns
its place. Only half its rationale changed: "not installed" is now "not installed
*by default*".

### Why the "verified by inspection and types only" clause had to go

The original decision closed with: "`RealAlpacaClient` is verified by inspection
and types only; it is never constructed in this sandbox." That was honest about the
constraint but it quietly became a load-bearing gap. On first execution the wrapper
had **eight `mypy --strict` errors and three genuine runtime bugs** (ADR-0033,
ADR-0034) — every one of them reachable on a normal paper run, none of them
detectable by the gate as configured.

The mechanism is worth recording, because it generalises to every optional
dependency: CI's `typecheck` job runs `uv sync --frozen`, which installs no extras,
so mypy resolved `import alpaca...` through the `ignore_missing_imports` override
and typed the entire SDK surface as `Any`. "Type-checked" therefore meant
"type-checked against nothing". With the SDK installed, the real signatures show
that every alpaca-py client method returns `Model | Dict[str, Any]` and that
`TradeAccount.cash` / `.equity` are `Optional[str]` — so `float(account.cash)`
was a crash waiting for an omitted field.

So the CI `typecheck` job now runs mypy **twice**: once bare, once after
`uv sync --frozen --extra alpaca`. An optional dependency that is never installed
in CI is not type-checked at all, and an untyped seam is exactly where a
never-executed path hides.

### Consequences of the amendment

- Reproducible installs for live trading: the SDK version is pinned in `uv.lock`
  rather than being whatever `pip install alpaca-py` happened to fetch.
- `mypy --strict` now passes in *both* configurations (extra present and absent),
  and the second is enforced in CI, so the wrapper's types cannot silently rot back
  to `Any`.
- The credential-gated integration layer (`tests/integration/test_alpaca_live.py`)
  replaces "verified by inspection" with "verified by execution". It skips cleanly
  in CI — no SDK and no credentials there — which is the same property the original
  decision wanted, now without the blind spot.
- One residual asymmetry, accepted: CI's `unit` and `integration` jobs still run
  without the extra, so the *runtime* Alpaca paths remain unexercised in CI. They
  are exercised by a developer with paper credentials. Automating that would mean
  putting live credentials in CI secrets, which is not worth it for a paper bench.
