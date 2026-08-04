"""The web dashboard: read a run's canonical ``result.json`` and visualize it.

Two output modes over one page (ADR-0023):

* **Static export** (:mod:`trading.dashboard.static_export`) — a single, fully
  self-contained ``.html`` file with the run data inlined, zero external
  references, openable over ``file://``. This is the load-bearing, fast-gate
  tested path and is pure standard library.
* **Interactive server** (:mod:`trading.dashboard.server`) — a thin FastAPI shell
  serving the *same* page plus a JSON endpoint. FastAPI/uvicorn are a lazily
  imported optional extra (same pattern as ``alpaca-py``), so importing this
  package never requires them.

The load-bearing parse/render logic lives in :mod:`trading.dashboard.payload`
and :mod:`trading.dashboard.static_export`, both pure stdlib.
"""

from __future__ import annotations

from trading.dashboard.payload import build_payload, load_document, load_payload
from trading.dashboard.static_export import render_html, write_html

__all__ = [
    "build_payload",
    "load_document",
    "load_payload",
    "render_html",
    "write_html",
]
