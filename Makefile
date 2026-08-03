# One-command dev loop (dev-playbook principle 17). Every target is stack-aware
# via `uv run`, so a newcomer needs only `make setup` then `make check`.

.PHONY: setup install-hooks check lint format typecheck test test-integration test-all audit ci-local

setup:  ## Install locked deps and the pre-push hook.
	uv sync --frozen
	$(MAKE) install-hooks

install-hooks:  ## Point git at the versioned hooks in .githooks/.
	git config core.hooksPath .githooks
	chmod +x .githooks/*
	@echo "pre-push hook installed (bypass a single push with: git push --no-verify)"

# --- The fast gate: what pre-push runs and what cheap CI mirrors -------------
check: lint typecheck test  ## Fast gate: lint + type-check + no-infra tests.

lint:  ## Static lint.
	uv run ruff check .

format:  ## Auto-format.
	uv run ruff format .

typecheck:  ## Strict type-check.
	uv run mypy

test:  ## Fast test layer only (no network, no infra).
	uv run pytest

# --- Heavier layers: CI-only, opt-in ----------------------------------------
test-integration:  ## Integration layer (needs network / yfinance).
	uv run pytest -m integration

test-all:  ## Every layer, including integration and e2e.
	uv run pytest -o addopts="-ra --strict-markers"

audit:  ## Vulnerability scan of locked dependencies.
	uv run pip-audit

ci-local:  ## Everything CI runs, locally.
	$(MAKE) check
	$(MAKE) test-integration
	$(MAKE) audit
