"""A thin, optional FastAPI shell over the same page the static export renders.

FastAPI and uvicorn are an **optional** dependency: they are imported *lazily,
inside functions*, behind a guard that raises a clear :class:`ImportError`
pointing at the ``dashboard`` extra when they are missing (the same pattern
:class:`trading.data.alpaca_client.RealAlpacaClient` uses for ``alpaca-py``).
Importing this module therefore never requires FastAPI, so the pure-stdlib core
(:mod:`trading.dashboard.payload` / :mod:`trading.dashboard.static_export`) stays
fully usable — and fast-gate testable — without it (ADR-0023).

The server is deliberately small: it renders the *same* HTML the static export
produces and adds one JSON endpoint. Two routes:

* ``GET /`` — the self-contained dashboard page (:func:`static_export.render_html`).
* ``GET /api/result`` — the parsed, schema-validated ``result.json`` document.

Both re-read ``result_path`` per request, so re-running a strategy and refreshing
the browser shows the new numbers without restarting the server.

A second, independent app (:func:`create_live_app` / :func:`serve_live`,
ADR-0075) serves a *live* view of a running ``trading paper --out DIR``
session instead of a finished ``result.json`` — the same two-route shape, over
:mod:`trading.dashboard.live_payload` / :mod:`trading.dashboard.live_view`
rather than :mod:`payload` / :mod:`static_export`:

* ``GET /`` — the self-polling live page (:func:`live_view.render_live_html`).
* ``GET /api/live`` — the normalized live payload as JSON, re-read per request.

A CLI invocation picks exactly one of the two apps (``trading dashboard
--result PATH --serve`` vs. ``trading dashboard --live-dir DIR --serve``); they
are never combined into one running server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trading.dashboard.payload import load_document, load_payload
from trading.dashboard.static_export import render_html

_INSTALL_HINT = (
    "FastAPI is required for the dashboard server but is not installed. Install "
    "the optional dashboard extra (e.g. `pip install 'algo-trading-bench[dashboard]'`) or, "
    "for an ad-hoc run, `uv pip install fastapi uvicorn`. The static HTML export "
    "needs none of this."
)


def create_app(result_path: str | Path) -> Any:
    """Build the FastAPI application serving the dashboard for ``result_path``.

    FastAPI is imported lazily here; a missing install raises a clear
    :class:`ImportError` naming the ``dashboard`` extra. The ``result.json`` is
    loaded once up front so a bad path or schema mismatch fails fast, then
    re-read per request so the page reflects the file's current contents.
    """
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover - exercised only without FastAPI
        raise ImportError(_INSTALL_HINT) from exc

    path = Path(result_path)
    # Fail fast: validate the document (path exists, schema matches) at startup.
    load_payload(path)

    app = FastAPI(title="Trading run dashboard", docs_url=None, redoc_url=None)

    def index() -> Any:
        """Serve the self-contained dashboard HTML, rendered from the live file."""
        return HTMLResponse(render_html(load_payload(path)))

    def api_result() -> Any:
        """Return the parsed, schema-validated ``result.json`` document as JSON."""
        return JSONResponse(load_document(path))

    app.add_api_route("/", index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/result", api_result, methods=["GET"])
    return app


def serve(
    result_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the dashboard server for ``result_path`` (blocking) via uvicorn.

    uvicorn is imported lazily; a missing install raises the same clear
    :class:`ImportError` naming the ``dashboard`` extra.
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised only without uvicorn
        raise ImportError(_INSTALL_HINT) from exc

    app = create_app(result_path)
    uvicorn.run(app, host=host, port=port)


def create_live_app(live_dir: str | Path) -> Any:
    """Build the FastAPI app serving a *live* view of ``live_dir`` (ADR-0075).

    ``live_dir`` is the ``--out`` directory of a ``trading paper`` session,
    finished or still running. Unlike :func:`create_app`, there is deliberately
    **no upfront validation read**: the entire point of this view is to work
    correctly before a session has written anything at all (``paper_state.json``
    does not exist until the first completed bar) and to keep working across
    every transient gap a live session's artifacts can have.
    :mod:`trading.dashboard.live_payload` already handles all of that by
    returning ``None``/empty defaults rather than raising, so this app never
    needs a special case for "the session hasn't started" or "caught it
    mid-write" — every request just re-reads whatever is on disk right now.
    """
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover - exercised only without FastAPI
        raise ImportError(_INSTALL_HINT) from exc

    from trading.dashboard.live_payload import build_live_payload
    from trading.dashboard.live_view import render_live_html

    path = Path(live_dir)
    app = FastAPI(title="Live paper session dashboard", docs_url=None, redoc_url=None)

    def index() -> Any:
        """Serve the self-polling live HTML page, seeded with the current snapshot."""
        return HTMLResponse(render_live_html(build_live_payload(path)))

    def api_live() -> Any:
        """Return the current normalized live payload as JSON (polled by the page)."""
        return JSONResponse(build_live_payload(path))

    app.add_api_route("/", index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/live", api_live, methods=["GET"])
    return app


def serve_live(
    live_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the live-session dashboard server for ``live_dir`` (blocking) via uvicorn.

    uvicorn is imported lazily; a missing install raises the same clear
    :class:`ImportError` naming the ``dashboard`` extra.
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised only without uvicorn
        raise ImportError(_INSTALL_HINT) from exc

    app = create_live_app(live_dir)
    uvicorn.run(app, host=host, port=port)
