"""Fast CLI test: an interrupted live paper session still writes its artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app
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
            # Process a couple of bars the normal way, then interrupt like a user.
            real_run(self, max_new_bars=2, max_empty_polls=1)
            raise KeyboardInterrupt

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
