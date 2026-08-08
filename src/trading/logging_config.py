"""The one place logging is configured — owned by the CLI entry point (ADR-0043).

Everything this bench says, it says through :func:`typer.echo` to a terminal. That
is the right channel for an operator watching a run and the wrong one for a session
nobody is watching: a container's per-bar stdout scrolls into a ring buffer, and the
questions that matter afterwards ("when did it start", "which symbol went missing",
"why did it stop") need timestamps and levels, not prose.

Three deliberate boundaries:

**A library never configures logging.** Importing ``trading`` must leave a host
application's handlers exactly as it found them, so nothing here runs at import
time; :func:`configure_logging` is called from the CLI callback and nowhere else.
The rest of the package only ever calls ``logging.getLogger(__name__)`` and logs.

**Logs go to stderr; the report stays on stdout.** ``docker logs`` merges both, so
nothing is lost, but a terminal operator keeps the per-bar decision lines
uninterrupted on stdout and every warning stays where the existing
``typer.echo(..., err=True)`` warnings already are. It also means a shell pipeline
reading a run's output never has log lines injected into it.

**The level knob governs *our* loggers.** ``--log-level DEBUG`` on a run using the
Alpaca SDK would otherwise turn on urllib3/httpx request logging and bury the
session in third-party chatter — the exact drowning this module exists to avoid. So
the requested level is set on the ``trading`` logger while the root stays at
WARNING, i.e. where it effectively sits today. Raising the level above WARNING does
apply to everything: quieting is global, verbosity is ours.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import TextIO

#: Emitted by every command unless ``--log-level`` says otherwise. INFO rather than
#: WARNING because the records that make an unattended session legible are INFO —
#: the feed's "symbol is back" recovery line (ADR-0035) among them, which until now
#: was written and never seen, since the root logger's default only passes WARNING.
DEFAULT_LOG_LEVEL = "INFO"

#: ``text`` for a human, ``json`` for a log shipper. See :func:`configure_logging`.
DEFAULT_LOG_FORMAT = "text"
LOG_FORMATS = ("text", "json")

#: The package whose loggers ``--log-level`` actually controls.
PACKAGE_LOGGER = "trading"

#: Stamped on the handler we install so re-configuring replaces it instead of
#: stacking a second copy (which would double every line).
_HANDLER_NAME = "trading-cli"

_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_TEXT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _text_formatter() -> logging.Formatter:
    """The human format, stamped in **UTC**.

    Every timestamp this bench produces is UTC — bar timestamps, the equity curve,
    ``result.json`` — and a log that is the only record of an unattended session must
    line up with them. ``logging`` defaults to local time, which on a container is
    whatever the image happened to be built with.
    """
    formatter = logging.Formatter(_TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT)
    formatter.converter = time.gmtime
    return formatter


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per line — the format that survives being read later.

    A session that ran for six hours unattended is read by ``grep``, by a log
    shipper, or by a human three days later asking a question nobody anticipated.
    Prose answers none of those well; ``{"level": "ERROR", "logger": ...}`` answers
    all three. It is not the default (see :func:`configure_logging`) because Monday's
    operator is a human at a terminal, and a format nobody can read at a glance is
    its own kind of invisible.

    Keys are fixed and ordered, so a downstream consumer can rely on the shape:
    ``ts`` (ISO-8601 UTC), ``level``, ``logger``, ``message``, plus ``exception``
    only when there is a traceback.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # ensure_ascii=False keeps symbols and venue messages readable; the newline
        # is the record separator the format is named for and json.dumps emits none.
        return json.dumps(payload, ensure_ascii=False)


def resolve_level(level: str) -> int:
    """Map a level name to its numeric value, or raise ``ValueError``.

    Case-insensitive, and rejects the numeric strings ``logging.getLevelName``
    happily round-trips into a fictional level such as ``Level 42`` — an operator
    who typos a level should be told, not silently given a threshold nothing
    matches. ``NOTSET`` is rejected for the same reason: as a *threshold* it means
    "inherit", which is not a level anyone means to ask for.
    """
    candidate = logging.getLevelNamesMapping().get(level.strip().upper())
    if not candidate:
        known = ", ".join(name for name in logging.getLevelNamesMapping() if name != "NOTSET")
        raise ValueError(f"--log-level must be one of {known}; got {level!r}")
    return candidate


def configure_logging(
    level: str = DEFAULT_LOG_LEVEL,
    fmt: str = DEFAULT_LOG_FORMAT,
    *,
    stream: TextIO | None = None,
) -> logging.Handler:
    """Install the process's single log handler and return it.

    Idempotent: calling it again replaces the handler this function installed rather
    than adding another, so a test process (or a future embedded caller) that runs
    several commands does not get each line N times.

    ``stream`` defaults to the *current* ``sys.stderr``, read at call time rather
    than at import, so a caller that has redirected stderr — Typer's ``CliRunner``,
    ``pytest``'s capture — gets its own stream. :class:`logging.StreamHandler`
    flushes on every record, which is what makes a line appear in ``docker logs``
    when it happens instead of when a 4 KB buffer fills.

    Raises ``ValueError`` on an unknown level or format; the CLI turns that into its
    usual exit-2 error rather than a traceback.
    """
    if fmt not in LOG_FORMATS:
        raise ValueError(f"--log-format must be one of {', '.join(LOG_FORMATS)}; got {fmt!r}")
    numeric = resolve_level(level)

    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.name = _HANDLER_NAME
    handler.setFormatter(JsonLinesFormatter() if fmt == "json" else _text_formatter())

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.name == _HANDLER_NAME]:
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)

    # Verbosity is ours, quieting is everyone's (see the module docstring). A record
    # from `trading.*` is admitted by its own logger's level and then handled here
    # regardless of the root's level, which only gates loggers that inherit it.
    root.setLevel(max(numeric, logging.WARNING))
    logging.getLogger(PACKAGE_LOGGER).setLevel(numeric)
    return handler
