# One-command dev loop (dev-playbook principle 17). Every target is stack-aware
# via `uv run`, so a newcomer needs only `make setup` then `make check`.

.PHONY: setup install-hooks check lint format typecheck test test-integration test-network test-all audit ci-local paper-preflight paper-dryrun paper-live paper-stop paper-status

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

test-network:  ## Live provider-contract layer (yfinance + Alpaca). Nightly in CI; never gates a merge.
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

# --- The unattended live paper run (docs/monday-divergence-run.md) -----------
# Wrappers over one already-tested command, not new logic. They add the three
# things an overnight run needs and the CLI does not provide: a preflight, a
# --out that is unique per launch (ADR-0048 truncates fill_divergence.csv at
# session start, so a retry into an occupied directory destroys the previous
# attempt's rows), and a detached launch (SIGHUP is unhandled, ADR-0043). The raw
# command stays visible: it is echoed at every launch and written to
# <out>/launch.cmd. Nothing here runs in CI or touches `check`.
PAPER_STRATEGY ?= sma_crossover
PAPER_SYMBOLS ?= @blue20
PAPER_INTERVAL ?= 5m
PAPER_DATE ?= 2026-08-10
PAPER_OUT_ROOT ?= results/paper
PAPER_ENV_FILE ?= .env
# Anything else the session should carry, e.g. PAPER_EXTRA_ARGS="--max-empty-polls 24".
PAPER_EXTRA_ARGS ?=
# stop/status act on the last launch unless told otherwise:
#   make paper-stop PAPER_OUT=results/paper/2026-08-10T133000Z-divergence
PAPER_OUT ?=
# auto (tmux if installed, else setsid) | tmux | setsid.
PAPER_LAUNCHER ?= auto
export PAPER_STRATEGY PAPER_SYMBOLS PAPER_INTERVAL PAPER_DATE PAPER_OUT_ROOT PAPER_ENV_FILE
export PAPER_EXTRA_ARGS PAPER_OUT PAPER_LAUNCHER
# `uv run --env-file` errors on a missing file, and the keys may already be
# exported, so the flag is conditional on the file being there.
PAPER_ENV_ARG := $(if $(wildcard $(PAPER_ENV_FILE)),--env-file $(PAPER_ENV_FILE),)

paper-preflight:  ## Read-only pre-run checks (SDK, creds, flat account, venue clock). Non-zero if not clean.
	uv run $(PAPER_ENV_ARG) python scripts/paper_preflight.py

paper-dryrun:  ## Acceptance test: the real command into a scratch --out, stopping at the first quiet poll.
	PAPER_EXTRA_ARGS="--max-empty-polls 1" ./scripts/paper_session.sh dryrun

paper-live:  ## Launch the real run detached (tmux, else setsid) into a fresh timestamped --out.
	./scripts/paper_session.sh launch

paper-stop:  ## Stop the launched session with SIGTERM so it finalizes (ADR-0043). Never SIGKILL.
	./scripts/paper_session.sh stop

paper-status:  ## Where the launched run's artifacts are, its state file, and the tail of its console.
	./scripts/paper_session.sh status
