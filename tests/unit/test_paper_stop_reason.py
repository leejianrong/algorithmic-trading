"""A paper session says why it stopped (ADR-0083).

ADR-0049 recorded the gap: a session ends for several different reasons and nothing
told the operator which. Each reason is pinned below in the scenario that causes it,
and the reason is recorded, never acted on -- so no test here changes *when* a
session stops, only what it says.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading.broker import SimulatedBroker
from trading.cli import SessionTerminated, app
from trading.clock import FakeClock, WallClock
from trading.config import CostConfig, RiskConfig
from trading.data.fake import FakeAdapter
from trading.data.recent_window import RecentWindowFeed, default_is_complete
from trading.engine import (
    STOP_BAR_LIMIT,
    STOP_INTERRUPTED,
    STOP_POLL_LIMIT,
    STOP_REPLAY_COMPLETE,
    STOP_SILENCE_TOLERANCE,
    STOP_TERMINATED,
    Engine,
    PaperSession,
)
from trading.interfaces import StrategyContext
from trading.risk import Guardrails
from trading.types import Bar, Order, Portfolio, TargetWeight

runner = CliRunner()


class _Flat:
    name = "flat"

    def on_bar(
        self, ts: datetime, bars: dict[str, Bar], context: StrategyContext
    ) -> list[Order | TargetWeight]:
        return []


def _session(n_bars: int) -> PaperSession:
    first = datetime(2024, 1, 2, tzinfo=UTC)
    bars = [Bar("AAA", first + timedelta(days=i), 10, 10, 10, 10, 1) for i in range(n_bars)]
    adapter = FakeAdapter(bars)
    clock = FakeClock(first + timedelta(days=n_bars + 5))
    feed = RecentWindowFeed(adapter, clock, default_is_complete)
    broker = SimulatedBroker(Portfolio(cash=1_000.0), CostConfig(0.0, 0.0))
    engine = Engine(adapter, broker, Guardrails(RiskConfig()))
    return PaperSession(engine, _Flat(), ["AAA"], feed, clock, warmup=False)


class TestSessionRecordsWhyItStopped:
    def test_no_reason_before_the_session_has_ended(self) -> None:
        assert _session(3).stop is None

    def test_silence_tolerance_names_the_poll_count(self) -> None:
        session = _session(3)
        session.run(max_empty_polls=3)

        assert session.stop is not None
        assert session.stop.reason == STOP_SILENCE_TOLERANCE
        assert "3 consecutive poll(s)" in session.stop.detail

    def test_bar_limit(self) -> None:
        session = _session(5)
        session.run(max_new_bars=2)

        assert session.stop is not None
        assert session.stop.reason == STOP_BAR_LIMIT
        assert "2 bar(s)" in session.stop.detail

    def test_poll_limit(self) -> None:
        session = _session(3)
        session.run(max_polls=1, max_empty_polls=99)

        assert session.stop is not None
        assert session.stop.reason == STOP_POLL_LIMIT

    def test_recording_does_not_change_when_it_stops(self) -> None:
        session = _session(4)
        session.run(max_empty_polls=2)

        assert len(session.session_log) == 4


_ONCE = [
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
]


class TestCliSurfacesTheReason:
    def test_once_replay_is_complete_not_silent(self, tmp_path: Path) -> None:
        result = runner.invoke(app, [*_ONCE, "--out", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert f"Stopped: {STOP_REPLAY_COMPLETE}" in result.output
        payload = json.loads((tmp_path / "result.json").read_text())
        assert payload["stop"]["reason"] == STOP_REPLAY_COMPLETE
        assert payload["schema_version"] == 1

    @pytest.mark.parametrize(
        ("exc", "reason"),
        [(KeyboardInterrupt, STOP_INTERRUPTED), (SessionTerminated, STOP_TERMINATED)],
    )
    def test_interrupt_and_terminate_are_told_apart(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exc: type[KeyboardInterrupt],
        reason: str,
    ) -> None:
        def raise_it(self: PaperSession, **kwargs: object) -> object:
            raise exc

        monkeypatch.setattr(WallClock, "sleep_until", lambda self, when: None)
        monkeypatch.setattr(PaperSession, "run", raise_it)

        result = runner.invoke(app, [*_ONCE, "--live", "--out", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert f"Stopped: {reason}" in result.output
        payload = json.loads((tmp_path / "result.json").read_text())
        assert payload["stop"]["reason"] == reason

    def test_a_backtest_result_has_no_stop_key(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "backtest",
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
                "--out",
                str(tmp_path / "eq.csv"),
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads((tmp_path / "result.json").read_text())
        assert "stop" not in payload
