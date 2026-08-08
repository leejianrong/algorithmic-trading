"""A real SIGTERM to a real ``trading paper`` process still writes its artifacts.

ADR-0043. Every mechanism that stops an unattended session — ``docker stop``,
``docker restart``, ``systemd stop``, a VPS reboot, a plain ``kill`` — sends
**SIGTERM**, and Python's default disposition for it terminates the interpreter
without unwinding, so :meth:`PaperSession.finalize` never runs. That is precisely
the loss ADR-0033 added ``finalize()`` to prevent, arriving through a second door.

These tests spawn the CLI as a **subprocess** and signal it for real. A unit test
that calls the handler directly proves the handler works; it does not prove the
handler is *installed*, which is the entire defect. The child runs
``--source synthetic``, so it is offline and belongs in the fast layer.

Readiness is a real signal, not a sleep: the child prints its ADR-0042 warmup line
the moment priming finishes and then blocks in ``WallClock.sleep_until`` waiting for
the next daily bar boundary — which is exactly the state a live session spends
99.9% of its life in, and therefore the state ``docker stop`` will find it in.
"""

from __future__ import annotations

import csv
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import pytest

from trading.cli import SessionTerminated, _sigterm_stops_the_session, _TerminationGuard

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="SIGTERM delivery is a POSIX notion; the guard is a no-op elsewhere"
)

# Generous ceilings: they exist to turn a hang into a failure, not to measure speed.
READY_TIMEOUT_S = 60.0
EXIT_TIMEOUT_S = 30.0

# Docker's default grace period between SIGTERM and SIGKILL. Finalization must fit
# inside it or the artifacts are lost anyway, just more slowly (ADR-0043).
DOCKER_GRACE_PERIOD_S = 10.0


@dataclass(frozen=True)
class TerminatedRun:
    """What one signalled child process left behind."""

    returncode: int
    stdout: str
    out_dir: Path
    finalize_seconds: float


def _drain(stream: IO[str], sink: queue.Queue[str | None]) -> None:
    for line in stream:
        sink.put(line)
    sink.put(None)


def _run_until_warmup_then_signal(out_dir: Path, signals: int) -> TerminatedRun:
    """Start a live paper session, wait for its warmup line, then signal it.

    ``signals`` SIGTERMs are delivered back to back; one is the deployment case,
    two is the impatient operator who runs ``kill`` again because nothing appeared
    to happen.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "trading.cli",
            "paper",
            "--strategy",
            "buy_and_hold",
            "--symbols",
            "AAA",
            "--from",
            "2024-01-02",
            "--to",
            "2024-01-10",
            "--source",
            "synthetic",
            "--live",
            "--out",
            str(out_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert child.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(target=_drain, args=(child.stdout, lines), daemon=True)
    reader.start()

    seen: list[str] = []
    deadline = time.monotonic() + READY_TIMEOUT_S
    try:
        while "Warmup:" not in "".join(seen):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"child never warmed up; saw: {''.join(seen)!r}")
            line = lines.get(timeout=remaining)
            if line is None:
                raise AssertionError(f"child exited before warming up; saw: {''.join(seen)!r}")
            seen.append(line)

        sent_at = time.monotonic()
        for _ in range(signals):
            child.send_signal(signal.SIGTERM)
        returncode = child.wait(timeout=EXIT_TIMEOUT_S)
        finalize_seconds = time.monotonic() - sent_at
    finally:
        if child.poll() is None:  # pragma: no cover - only on a failing assertion above
            child.kill()
            child.wait(timeout=EXIT_TIMEOUT_S)

    while True:  # collect whatever the child said on its way out
        line = lines.get(timeout=EXIT_TIMEOUT_S)
        if line is None:
            break
        seen.append(line)

    return TerminatedRun(returncode, "".join(seen), out_dir, finalize_seconds)


@pytest.fixture(scope="module")
def terminated(tmp_path_factory: pytest.TempPathFactory) -> TerminatedRun:
    """One signalled session, shared by the assertions below (spawning is slow)."""
    return _run_until_warmup_then_signal(tmp_path_factory.mktemp("sigterm"), signals=1)


class TestSigtermFinalizes:
    """A session stopped by SIGTERM leaves the same artifacts Ctrl-C leaves."""

    def test_exits_cleanly_rather_than_dying_by_signal(self, terminated: TerminatedRun) -> None:
        """Exit 0, not ``-SIGTERM``.

        Before ADR-0043 this returned ``-15``: the default disposition, no unwinding,
        no ``finalize()``. The session *completed* — it wrote everything it was asked
        to write — so 0 is the honest code, and ``docker stop`` does not read it.
        """
        assert terminated.returncode == 0, terminated.stdout

    def test_says_which_signal_stopped_it(self, terminated: TerminatedRun) -> None:
        assert "SIGTERM" in terminated.stdout, terminated.stdout
        assert "finalizing" in terminated.stdout.lower(), terminated.stdout

    def test_writes_every_artifact(self, terminated: TerminatedRun) -> None:
        for name in ("equity_curve.csv", "result.json", "paper_session.log"):
            assert (terminated.out_dir / name).exists(), f"{name} missing\n{terminated.stdout}"

    def test_the_equity_csv_parses(self, terminated: TerminatedRun) -> None:
        """Well-formed, not merely present — a truncated write would still exist."""
        with (terminated.out_dir / "equity_curve.csv").open() as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames
            rows = list(reader)
        assert header is not None
        assert {"ts", "equity", "exposure"} <= set(header), header
        # A session signalled during its post-warmup sleep has traded nothing, so an
        # empty body is correct; ADR-0042 forbids fabricating equity points for the
        # primed bars. The header is the proof the file was written whole.
        assert isinstance(rows, list)

    def test_the_result_json_parses_and_is_a_paper_run(self, terminated: TerminatedRun) -> None:
        payload = json.loads((terminated.out_dir / "result.json").read_text())
        assert payload["mode"] == "paper"
        assert payload["schema_version"] == 1
        for key in ("symbols", "equity_curve", "fills", "rejections", "metrics"):
            assert key in payload, sorted(payload)

    def test_the_session_log_kept_the_warmup_record(self, terminated: TerminatedRun) -> None:
        assert "Warmup:" in (terminated.out_dir / "paper_session.log").read_text()

    def test_the_session_logged_its_own_lifecycle(self, terminated: TerminatedRun) -> None:
        """The unattended session's log records reach a real process's stderr.

        Regression: the CLI's logger was ``logging.getLogger(__name__)``, which under
        ``python -m trading.cli`` is ``"__main__"`` — outside the ``trading`` tree
        that ``--log-level`` sets, so every one of these records fell back to the
        root's WARNING threshold and vanished. Nothing in-process caught it, because
        under Typer's ``CliRunner`` the module is imported normally (ADR-0043).
        """
        assert "paper session starting" in terminated.stdout, terminated.stdout
        assert "SIGTERM received" in terminated.stdout, terminated.stdout
        assert "paper session finished" in terminated.stdout, terminated.stdout

    def test_finalizes_inside_the_docker_grace_period(self, terminated: TerminatedRun) -> None:
        """SIGKILL lands 10s after SIGTERM by default; finalizing must fit inside it."""
        assert terminated.finalize_seconds < DOCKER_GRACE_PERIOD_S, terminated.finalize_seconds


class TestTerminationGuardSemantics:
    """The guard's rules, stated directly.

    These are a supplement to the subprocess tests above, never a substitute: they
    say what the guard does, and only a real signal to a real process says that it
    is *installed*, which is the defect ADR-0043 fixes.
    """

    def test_the_first_signal_raises_the_interrupt_the_loop_already_ends_on(self) -> None:
        guard = _TerminationGuard()

        with pytest.raises(SessionTerminated):
            guard.handle(signal.SIGTERM, None)
        # Subclassing KeyboardInterrupt is what lets ADR-0033's except-path catch a
        # stop signal without a second exit route existing to drift from it.
        assert issubclass(SessionTerminated, KeyboardInterrupt)

    def test_a_repeat_signal_is_ignored_so_finalization_completes(self) -> None:
        guard = _TerminationGuard()

        with pytest.raises(SessionTerminated):
            guard.handle(signal.SIGTERM, None)
        guard.handle(signal.SIGTERM, None)  # returns instead of raising
        assert guard.signals == 2

    def test_disarming_drops_a_signal_that_arrives_while_writing_artifacts(self) -> None:
        guard = _TerminationGuard()
        guard.disarm()

        guard.handle(signal.SIGTERM, None)  # returns instead of raising
        assert guard.signals == 1

    def test_the_handler_is_installed_and_then_restored(self) -> None:
        """Process-global state is borrowed for the session, not claimed forever."""
        before = signal.getsignal(signal.SIGTERM)

        with _sigterm_stops_the_session() as guard:
            assert signal.getsignal(signal.SIGTERM) == guard.handle

        assert signal.getsignal(signal.SIGTERM) == before


class TestSecondSigtermDoesNotAbortFinalization:
    """The impatient operator runs ``kill`` twice; the artifacts survive anyway.

    Finalization is bounded, in-memory work plus a handful of small writes. Letting a
    second signal raise inside it would truncate exactly the files the first signal
    was honoured in order to save, so a signal that arrives once finalization has
    begun is ignored (ADR-0043). SIGKILL remains the way to stop it regardless.
    """

    def test_artifacts_are_still_whole(self, tmp_path: Path) -> None:
        run = _run_until_warmup_then_signal(tmp_path, signals=2)

        assert run.returncode == 0, run.stdout
        payload = json.loads((run.out_dir / "result.json").read_text())
        assert payload["mode"] == "paper"
        assert (run.out_dir / "equity_curve.csv").exists()
