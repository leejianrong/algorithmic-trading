# syntax=docker/dockerfile:1
#
# Reproducible image for unattended paper trading (KAN-683, EPIC-86).
#
# - uv-based, resolved with `uv sync --frozen --extra alpaca` so the image
#   matches uv.lock exactly, the same way CI's `alpaca-mypy`/`network` jobs do
#   (dev-playbook principle 14: install from the lock, never `uv add` at build
#   time). Never `--extra dashboard` -- the web dashboard server has no place
#   in a trading container (ADR-0023's FastAPI/uvicorn extra is for the
#   operator's own machine, not the unattended runner).
# - Multi-stage: the builder carries uv + a resolved venv; the runtime stage
#   copies only the venv and the application source, so no compiler, no uv
#   binary and no build-time package ships in what can place real orders.
# - Runs as a non-root user (this container will hold Alpaca credentials
#   capable of submitting orders -- see ENTRYPOINT below).
# - Zero secrets baked in: nothing here COPYs .env, no ARG ever carries a key,
#   and .dockerignore keeps .env out of the build context entirely so it is
#   physically impossible for uv/pip to embed it in a layer. Credentials
#   (ALPACA_API_KEY / ALPACA_SECRET_KEY) reach the running container the same
#   way they reach a local `uv run` invocation -- read from the process
#   environment at runtime (`os.environ`, see .env.example) -- so `docker run
#   --env-file .env ...` (or an orchestrator's own secret injection, KAN-684)
#   works unmodified. There is deliberately no second credential-loading path
#   inside the image.

# ---- Builder ----------------------------------------------------------------
# Pinned to the exact Python version this repo targets (.python-version: 3.13),
# and to a specific uv release for a reproducible build layer.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.16 /uv /uvx /usr/local/bin/

WORKDIR /app

# uv env tuning for a container build: bytecode-compile the venv up front
# (faster cold start, paid once at build time) and hardlink from uv's cache
# rather than requiring the same filesystem across a `--mount=type=cache`.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Resolve dependencies first, from the lock alone, before the source tree
# invalidates the layer cache on every code change. hatchling (the configured
# build backend) reads `readme = "README.md"` out of pyproject.toml even to
# resolve metadata, so it has to be present at this step; `--no-install-project`
# means src/trading itself is not required yet.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra alpaca

# Now add the actual source and install the project into the same venv.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra alpaca

# ---- Runtime ------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Non-root: this container will hold live paper-trading credentials capable
# of placing real orders (ADR-0018/0043's operating assumption).
RUN groupadd --system trading && \
    useradd --system --gid trading --create-home --home-dir /home/trading trading

WORKDIR /app

# Only the resolved venv and the application source cross the stage boundary
# -- no uv binary, no C toolchain, no pip cache.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# Predictable, documented output path for run artifacts (result.json,
# equity_curve.csv, paper_state.json, paper_session.log, fill_divergence.csv).
# KAN-685 mounts a volume here to persist results across container restarts;
# `--out` / `PAPER_OUT_ROOT` should point under this directory.
RUN mkdir -p /app/results && chown -R trading:trading /app/results /home/trading

USER trading

VOLUME ["/app/results"]

# No hardcoded subcommand: the entrypoint is the `trading` CLI itself, so a
# supervisor (KAN-686) or a plain `docker run` passes the subcommand and flags
# as container arguments, e.g.:
#   docker run --env-file .env -v "$PWD/results:/app/results" trading-bench \
#     paper --broker alpaca --live --market us_equity --symbols @blue20 \
#     --out /app/results/paper/run1
ENTRYPOINT ["trading"]
CMD ["--help"]
