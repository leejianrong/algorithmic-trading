"""A live session says so when its primed history is shorter than the strategy needs (KAN-702)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app
from trading.clock import WallClock
from trading.engine import PaperSession
from trading.strategies import STRATEGIES, required_history_bars
from trading.strategies.cross_sectional import CrossSectional
from trading.strategies.mean_reversion import MeanReversion
from trading.strategies.momentum import Momentum
from trading.strategies.sma_crossover import SmaCrossover
from trading.strategies.trend_following import TrendFollowing

runner = CliRunner()


class TestDeclaredNeed:
    def test_each_lookback_strategy_declares_from_its_own_params(self) -> None:
        assert required_history_bars(SmaCrossover(fast=5, slow=30)) == 30
        assert required_history_bars(Momentum(lookback=60)) == 61
        assert required_history_bars(MeanReversion(period=14)) == 15
        assert required_history_bars(CrossSectional(lookback=120)) == 121
        assert required_history_bars(TrendFollowing(lookback=252, skip_recent=21)) == 274

    def test_undeclaring_strategies_are_none(self) -> None:
        assert required_history_bars(STRATEGIES["buy_and_hold"]()) is None
        assert required_history_bars(STRATEGIES["equal_weight"]()) is None


def _invoke(tmp_path: Path, strategy: str, start: str, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "paper", "--strategy", strategy, "--symbols", "AAA",
            "--from", start, "--to", "2024-06-28", "--source", "synthetic",
            "--live", "--out", str(tmp_path), *extra,
        ],
    )  # fmt: skip


@pytest.fixture(autouse=True)
def _one_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = PaperSession.run

    def run_then_stop(self: PaperSession, **kwargs: object) -> object:
        real_run(self, max_new_bars=1, max_empty_polls=1, on_warmup=kwargs.get("on_warmup"))
        raise KeyboardInterrupt

    monkeypatch.setattr(WallClock, "sleep_until", lambda self, when: None)
    monkeypatch.setattr(PaperSession, "run", run_then_stop)


class TestWarmupShortfallLine:
    def test_short_history_warns_on_stdout_and_log(self, tmp_path: Path) -> None:
        # trend_following needs 274 bars; a 50-bar lookback is far short.
        res = _invoke(tmp_path, "trend_following", "2024-06-03", "--lookback", "50")
        warns = [ln for ln in res.output.splitlines() if ln.startswith("WARNING: primed")]
        assert len(warns) == 1, res.output
        assert "needs 274" in warns[0] and "short" in warns[0] and "1d bars" in warns[0]
        log = (tmp_path / "paper_session.log").read_text()
        assert log.count("WARNING: primed") == 1
        # Reporting only: the session still finalized.
        assert (tmp_path / "result.json").exists()

    def test_sufficient_history_is_silent(self, tmp_path: Path) -> None:
        res = _invoke(tmp_path, "sma_crossover", "2023-01-02")
        assert "Warmup: primed" in res.output
        assert "WARNING: primed" not in res.output

    def test_strategy_without_a_declaration_is_silent(self, tmp_path: Path) -> None:
        res = _invoke(tmp_path, "buy_and_hold", "2024-06-03")
        assert "Warmup: primed" in res.output
        assert "WARNING: primed" not in res.output

    def test_once_replay_never_warns(self, tmp_path: Path) -> None:
        res = runner.invoke(
            app,
            ["paper", "--strategy", "trend_following", "--symbols", "AAA",
             "--from", "2024-06-03", "--to", "2024-06-28", "--source", "synthetic",
             "--out", str(tmp_path)],
        )  # fmt: skip
        assert "WARNING: primed" not in res.output
