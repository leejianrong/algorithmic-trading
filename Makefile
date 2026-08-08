# One-command dev loop (dev-playbook principle 17). Every target is stack-aware
# via `uv run`, so a newcomer needs only `make setup` then `make check`.

.PHONY: setup install-hooks check lint format typecheck test test-integration test-network test-all audit ci-local

setup:  ## Install locked deps and the pre-push hook.
	uv sync --frozen
	$(MAKE) install-hooks

install-hooks:  ## Point git at the versioned hooks in .githooks/.
	git config core.hooksPath .githooks
	chmod +x .githooks/*
	@echo "pre-push hook installed (bypass a single push with: git push --no-verify)"

# --- The fast gate: what pre-push runs and what cheap CI mirrors -------------
check: lint typecheck test  ## Fast gate: lint + type-check + no-infra tests.

lint:  ## Static lint + format check (mirrors CI's lint job).
	uv run ruff check .
	uv run ruff format --check .

format:  ## Auto-format.
	uv run ruff format .

typecheck:  ## Strict type-check.
	uv run mypy

test:  ## Fast test layer only (no network, no infra).
	uv run pytest

# --- Heavier layers: CI-only, opt-in ----------------------------------------
# Split by what can block a merge (ADR-0040): `test-integration` is the REQUIRED
# CI job and must stay offline; `test-network` is the nightly, non-required one.
test-integration:  ## Integration layer, offline (needs optional extras / broker creds; no internet).
	uv run pytest -m "integration and not network"

test-network:  ## Live provider-contract layer (hits yfinance). Nightly in CI; never gates a merge.
	uv run pytest -m network

test-all:  ## Every layer, including integration, network and e2e.
	uv run pytest -o addopts="-ra --strict-markers"

audit:  ## Vulnerability scan of locked dependencies.
	uv run pip-audit

ci-local:  ## Everything on CI's merge path, locally (the six required checks).
	$(MAKE) check
	$(MAKE) test-integration
	$(MAKE) audit
	@echo "Merge-path checks done. The nightly live-provider job is: make test-network"
