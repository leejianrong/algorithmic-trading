"""Logging is configured once, by the entry point, onto stderr (ADR-0043).

Before this the package had exactly one logger (``data.recent_window``, ADR-0035)
and no configuration at all, so its escalation warnings went to the root logger's
last-resort handler and its recovery INFO line — "the symbol is back in the feed" —
was written and never seen by anyone.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from trading.cli import app
from trading.logging_config import (
    PACKAGE_LOGGER,
    JsonLinesFormatter,
    configure_logging,
    resolve_level,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Put the global logging state back — these tests mutate a process singleton."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    root_level = root.level
    package_level = logging.getLogger(PACKAGE_LOGGER).level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        logging.getLogger(PACKAGE_LOGGER).setLevel(package_level)


class TestLevelParsing:
    def test_accepts_a_name_in_any_case(self) -> None:
        assert resolve_level("debug") == logging.DEBUG
        assert resolve_level(" Warning ") == logging.WARNING

    @pytest.mark.parametrize("bad", ["chatty", "42", "", "NOTSET"])
    def test_rejects_anything_else(self, bad: str) -> None:
        """A typo must be told, not silently turned into a threshold nothing matches.

        ``NOTSET`` is in ``logging``'s own name mapping but means "inherit" as a
        threshold, so accepting it would answer a level request with a non-answer.
        """
        with pytest.raises(ValueError, match="--log-level"):
            resolve_level(bad)

    def test_rejects_an_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="--log-format"):
            configure_logging("INFO", "yaml")


class TestWhatReachesTheStream:
    def test_our_own_info_records_are_visible(self) -> None:
        """The whole point: the ADR-0035 recovery line was invisible at the default."""
        stream = io.StringIO()
        configure_logging("INFO", "text", stream=stream)

        logging.getLogger("trading.data.recent_window").info("AAPL is back in the feed")

        written = stream.getvalue()
        assert "AAPL is back in the feed" in written
        assert "INFO" in written
        assert "trading.data.recent_window" in written

    def test_third_party_chatter_stays_at_warning(self) -> None:
        """``--log-level DEBUG`` must not turn on the HTTP client's request log.

        A live Alpaca session polls constantly; letting the SDK's transport talk at
        INFO would bury the session's own records, which is the drowning this
        configuration exists to prevent.
        """
        stream = io.StringIO()
        configure_logging("DEBUG", "text", stream=stream)

        logging.getLogger("urllib3.connectionpool").info("Starting new HTTPS connection")
        logging.getLogger("urllib3.connectionpool").warning("Retrying (Retry(total=2))")

        written = stream.getvalue()
        assert "Starting new HTTPS connection" not in written
        assert "Retrying" in written

    def test_quieting_is_global_even_though_verbosity_is_not(self) -> None:
        stream = io.StringIO()
        configure_logging("ERROR", "text", stream=stream)

        logging.getLogger("urllib3.connectionpool").warning("Retrying (Retry(total=2))")
        logging.getLogger("trading.data.recent_window").warning("AAPL dropped from this poll")

        assert stream.getvalue() == ""

    def test_the_text_timestamp_is_utc(self) -> None:
        """Every other timestamp this bench emits is UTC; the log must agree.

        ``logging`` stamps local time by default, which on a container is whatever
        the image was built with — and correlating a log line against a bar
        timestamp in ``result.json`` is most of what reading it afterwards is for.
        """
        stream = io.StringIO()
        configure_logging("INFO", "text", stream=stream)

        logging.getLogger("trading.cli").info("hello")

        stamp = stream.getvalue().split(" ", 1)[0]
        assert stamp.endswith("Z"), stamp
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert abs((datetime.now(UTC) - parsed).total_seconds()) < 120

    def test_reconfiguring_replaces_rather_than_stacks(self) -> None:
        """Two configurations must not print every line twice."""
        first, second = io.StringIO(), io.StringIO()
        configure_logging("INFO", "text", stream=first)
        configure_logging("INFO", "text", stream=second)

        logging.getLogger("trading.cli").info("once, please")

        assert "once, please" not in first.getvalue()
        assert second.getvalue().count("once, please") == 1


class TestJsonLines:
    def test_one_parseable_object_per_record(self) -> None:
        stream = io.StringIO()
        configure_logging("INFO", "json", stream=stream)

        logging.getLogger("trading.cli").warning("SIGTERM received — finalizing")

        payload = json.loads(stream.getvalue().strip())
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "trading.cli"
        assert payload["message"] == "SIGTERM received — finalizing"
        assert payload["ts"].startswith("20")  # ISO-8601, UTC
        assert "exception" not in payload

    def test_a_traceback_becomes_a_field_not_a_smear_of_lines(self) -> None:
        """The reason JSON earns its keep: a traceback stays inside one record."""
        record = logging.LogRecord("trading.cli", logging.ERROR, __file__, 1, "boom", None, None)
        try:
            raise RuntimeError("venue said no")
        except RuntimeError:
            record.exc_info = sys.exc_info()

        payload = json.loads(JsonLinesFormatter().format(record))

        assert payload["message"] == "boom"
        assert "venue said no" in payload["exception"]
        assert "\n" not in json.dumps(payload)


class TestCliWiring:
    @pytest.mark.parametrize("flag,value", [("--log-level", "chatty"), ("--log-format", "yaml")])
    def test_a_bad_option_is_a_clean_error_not_a_traceback(self, flag: str, value: str) -> None:
        result = runner.invoke(app, [flag, value, "backtest", "--help"])

        assert result.exit_code == 2
        assert "error:" in result.output


class TestImportingTheLibraryConfiguresNothing:
    """A host application's logging must survive ``import trading`` untouched.

    Checked in a subprocess because this test session has already imported the
    package and configured logging several times over; only a clean interpreter can
    answer the question. Same technique ADR-0028 uses to prove ``universe.py`` has no
    runtime import of ``trading.data``.
    """

    def test_no_handler_and_no_level_is_installed_on_import(self) -> None:
        probe = (
            "import logging, json;"
            "import trading, trading.cli, trading.logging_config;"
            "root = logging.getLogger();"
            "print(json.dumps({'handlers': [type(h).__name__ for h in root.handlers],"
            " 'root_level': root.level,"
            " 'package_level': logging.getLogger('trading').level}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )

        observed = json.loads(completed.stdout)
        assert observed == {"handlers": [], "root_level": logging.WARNING, "package_level": 0}
