"""The per-bar status line carries a time of day when the bars are intraday (KAN-720).

``cli._format_bar`` stamped every line with ``outcome.ts.date().isoformat()`` -- the
date and nothing else. That was correct while paper mode was daily-only and became
wrong the moment ADR-0022 added sub-daily intervals: at ``--interval 5m`` roughly 78
bars per symbol-day all print the same leading stamp, so stdout gives the operator no
way to tell which bar is which, when a fill happened, or whether the session is
progressing or wedged. The same line is written to ``paper_session.log``, which is the
only per-bar artifact that survives mid-session (``paper_state.json`` is a single
overwritten snapshot), so the after-the-fact record had no time axis either.

The stamp is now formatted at the *session's* frequency, passed in from the caller:
daily keeps the bare date, byte-for-byte; intraday adds ``%H:%M``, matching how the
ADR-0042 warmup line already renders a span.

The load-bearing test is :func:`test_five_minute_session_log_has_one_stamp_per_bar` --
the defect was never "the format is ugly", it was that N bars collapsed onto a handful
of distinct stamps, so the assertion is on the *distinct-stamp count*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from trading.cli import _format_bar, app
from trading.engine import BarOutcome
from trading.frequency import DAILY, Frequency

runner = CliRunner()

_FIVE_MIN = Frequency.parse("5m")


def _outcome(ts: datetime) -> BarOutcome:
    """A minimal held-nothing bar: the stamp is the only thing under test."""
    return BarOutcome(
        ts=ts,
        fills=[],
        intents=[],
        submitted=[],
        clamps=[],
        guardrail_rejections=[],
        broker_rejections=[],
        halted_now=False,
        halted=False,
        equity=1_000.0,
        exposure=0.0,
    )


class TestStampFormat:
    def test_daily_bar_renders_the_bare_date(self) -> None:
        """Pinned literally: the daily line must not move by one character."""
        line = _format_bar(_outcome(datetime(2026, 8, 10, tzinfo=UTC)), DAILY)
        assert line == "2026-08-10  decision: (hold)  |  equity: $1,000.00  exposure: 0.0%"

    def test_intraday_bar_renders_the_time_of_day(self) -> None:
        line = _format_bar(_outcome(datetime(2026, 8, 10, 14, 35, tzinfo=UTC)), _FIVE_MIN)
        assert line == "2026-08-10 14:35  decision: (hold)  |  equity: $1,000.00  exposure: 0.0%"

    def test_daily_stamp_ignores_a_non_midnight_timestamp(self) -> None:
        """The frequency decides, not the timestamp.

        Deriving intraday-ness from ``ts.time() != midnight`` would have needed no
        plumbing, but it is inference rather than fact: a daily bar that happened to
        carry a session-open time would silently change format. The frequency is
        carried deliberately (ADR-0022), so a daily run renders a date whatever the
        clock says.
        """
        line = _format_bar(_outcome(datetime(2026, 8, 10, 13, 30, tzinfo=UTC)), DAILY)
        assert line.startswith("2026-08-10  decision:")

    def test_intraday_bars_within_one_day_render_distinctly(self) -> None:
        stamps = {
            _format_bar(_outcome(datetime(2026, 8, 10, 14, m, tzinfo=UTC)), _FIVE_MIN).split(
                "  decision:"
            )[0]
            for m in (0, 5, 10, 15)
        }
        assert len(stamps) == 4


class TestSessionLog:
    def test_five_minute_session_log_has_one_stamp_per_bar(self, tmp_path: Path) -> None:
        """The actual defect: 156 bar lines collapsed onto 2 distinct stamps.

        Measured on ``main`` at 825e8dd over this exact two-day replay. Asserting on
        the distinct-stamp count -- not on the presence of a colon somewhere -- is
        what makes this a regression test for the thing that was wrong.
        """
        out = tmp_path / "paper5m"
        result = runner.invoke(
            app,
            [
                "paper",
                "--strategy",
                "sma_crossover",
                "--symbols",
                "AAA,BBB",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--interval",
                "5m",
                "--from",
                "2024-01-02",
                "--to",
                "2024-01-03",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output

        lines = (out / "paper_session.log").read_text().splitlines()
        bar_lines = [line for line in lines if "  decision:" in line]
        assert len(bar_lines) > 100, "expected a multi-day 5m replay, got too few bars"

        stamps = [line.split("  decision:")[0] for line in bar_lines]
        assert len(set(stamps)) == len(stamps), (
            f"{len(bar_lines)} bar lines collapsed onto {len(set(stamps))} distinct stamps"
        )
        # Every stamp carries a time of day, and more than one calendar day is covered.
        assert all(len(s) == len("2024-01-02 14:35") for s in stamps)
        assert len({s[:10] for s in stamps}) == 2

    def test_daily_session_log_stamps_are_dates_only(self, tmp_path: Path) -> None:
        """Daily is unchanged: one bare date per bar, and one bar per trading day."""
        out = tmp_path / "paperdaily"
        result = runner.invoke(
            app,
            [
                "paper",
                "--strategy",
                "sma_crossover",
                "--symbols",
                "AAA,BBB",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--from",
                "2021-01-01",
                "--to",
                "2021-06-30",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output

        bar_lines = [
            line
            for line in (out / "paper_session.log").read_text().splitlines()
            if "  decision:" in line
        ]
        stamps = [line.split("  decision:")[0] for line in bar_lines]
        assert stamps, "expected bar lines in the daily session log"
        assert all(len(s) == len("2021-01-04") for s in stamps)
        assert len(set(stamps)) == len(stamps)
