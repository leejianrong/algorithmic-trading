"""Algorithmic trading test bench.

A single engine drives both backtesting and paper trading over US-equity daily
bars. See PLAN.md for scope and docs/adr/ for the decisions behind this layout.

Only the decided, stable contracts live here so far: the core value types
(:mod:`trading.types`), the dependency-injection seams (:mod:`trading.interfaces`),
and an in-memory data adapter for tests (:mod:`trading.data.fake`). The engine,
broker, strategies, sizing, guardrails, CLI, and report land in slice V1 onward
(see SLICES.md and CLAUDE.md for honest build status).
"""

__all__ = ["interfaces", "types"]
