"""Fast CLI test: an interrupted live paper session still writes its artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app
from trading.clock import WallClock
from trading.engine import PaperSession

runner = CliRunner()


class TestInterruptedLiveSessionFinalizes:
    """ADR-0033: Ctrl-C is a live session's only exit; it must not lose the run.

    The live loop runs until interrupted, so before this every ``--live`` session
    ended by skipping the code *after* ``session.run(...)`` -- the equity CSV, the
    dashboard's ``result.json``, and the printed summary were unreachable in live
    mode. Verified against a real Alpaca paper session (exit 130, only
    ``paper_session.log`` and ``paper_state.json`` on disk); reproduced here
    offline by making the loop raise the same way, with no network or clock wait.
    """

    def _invoke(self, tmp_path: Path) -> Result:
        return runner.invoke(
            app,
            [
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
                str(tmp_path),
            ],
        )

    @pytest.fixture
    def interrupted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_run = PaperSession.run

        def run_then_interrupt(self: PaperSession, **kwargs: object) -> object:
            # Warm up and poll exactly the way a live session does, then interrupt
            # like a user. Since ADR-0042 a live session's opening poll is *warmup*
            # -- history primed, nothing traded -- so this reaches the Ctrl-C path
            # having built real state without having submitted a single order,
            # which is precisely the shape a Monday-morning session is interrupted
            # in.
            real_run(self, max_new_bars=2, max_empty_polls=1)
            raise KeyboardInterrupt

        # A live session waits on the wall clock for the next bar boundary, and
        # after the warmup poll there is nothing else to do until then. Left real,
        # this test would block until the next UTC midnight; the wait is not what
        # ADR-0033 is about.
        monkeypatch.setattr(WallClock, "sleep_until", lambda self, when: None)
        monkeypatch.setattr(PaperSession, "run", run_then_interrupt)

    def test_writes_equity_csv_and_result_json(self, tmp_path: Path, interrupted: None) -> None:
        result = self._invoke(tmp_path)

        assert result.exit_code == 0, result.output
        assert (tmp_path / "equity_curve.csv").exists()
        assert (tmp_path / "result.json").exists()
        assert (tmp_path / "paper_session.log").exists()

    def test_reports_the_interruption_and_the_summary(
        self, tmp_path: Path, interrupted: None
    ) -> None:
        result = self._invoke(tmp_path)

        assert "interrupted" in result.output.lower()
        # The normal end-of-run summary still prints.
        assert "Processed" in result.output

    def test_reports_what_it_warmed_up_on(self, tmp_path: Path, interrupted: None) -> None:
        """Even a session interrupted before its first live bar says what it primed.

        A silent warmup is indistinguishable from a session that did nothing at all,
        and the count is how an operator checks the strategy has its lookback before
        it starts trading (ADR-0042).
        """
        result = self._invoke(tmp_path)

        assert "Warmup:" in result.output
        assert "no orders submitted" in result.output
        assert "Warmup:" in (tmp_path / "paper_session.log").read_text()
