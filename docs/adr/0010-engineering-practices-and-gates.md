# ADR-0010: Engineering practices and quality gates (dev-playbook)

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

The bench is heading toward real capital, where a regression isn't an
inconvenience but a potential loss. The project is also worked on by coding
agents as well as a human, so the workflow has to be legible and enforced rather
than remembered. Setting the gates up while the codebase is one module is far
cheaper than retrofitting them once the engine, broker, and strategies exist.

## Decision

Adopt the dev-playbook practices, adapted to a Python/uv stack, from the first
commit:

- **Layer tests by cost.** A fast, no-infra layer (default `pytest` run) and a
  CI-only integration layer marked `@pytest.mark.integration`. A slow check never
  gates a local push.
- **Dependency-injection seams.** The engine depends on `DataAdapter`, `Broker`,
  `Strategy`, and `RiskGuardrails` protocols, each with a real and an in-memory
  fake implementation, so the fast layer needs zero infrastructure.
- **A fast pre-push hook** (`.githooks/pre-push`) that mirrors the cheap CI jobs
  (`make check`), with `--no-verify` as the documented escape hatch.
- **Parallel CI** with lockfile-frozen installs and caching: separate lint,
  type-check, unit, integration, build, and security jobs; cancel-in-progress.
- **Secrets and supply chain:** secret-bearing files git-ignored, a gitleaks
  history scan and `pip-audit` in CI, a committed `uv.lock`.
- **Enablement and docs:** a `CLAUDE.md` agent brief stating honest build status
  and exact commands, ADRs (this directory) for the "why", and a one-command dev
  loop (`Makefile`).

## Alternatives considered

| Option | Why not |
|--------|---------|
| Add gates later, once there's "real" code | Retrofitting tests and seams after the engine exists is the expensive path; habits and coverage are hardest to add under a growing surface. |
| A single `make test` that runs everything | A test command that needs the network before its first assertion gets bypassed, and then it protects nothing. |
| Rely on CI alone, no local gate | Pushes land red often, slowing everyone; the cheap checks belong on the developer's machine too. |

## Consequences

- Buys: regressions caught before merge, a workflow agents follow by default,
  reproducible installs, and secrets/vulns surfaced automatically.
- Costs: upfront setup and a little ceremony per change (branch, gate, PR); the
  gates must be kept honest as the stack grows (e.g. add an e2e job when V5
  lands, bump pinned actions when runtimes deprecate).
- Forecloses: nothing; docs-as-code publishing (a Zensical/mkdocs site with a PR
  build-check) and richer observability are additive when there's a running
  service to document and watch.
- Now true: new code lands with a test in the right layer, and any new external
  dependency (yfinance today, Alpaca next milestone) enters behind a seam with a
  fake, not as a hard call in the engine.
