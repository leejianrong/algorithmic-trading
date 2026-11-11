"""ADR-0084: the ADR-0039 paired win rate against the diversified baseline (offline).

Reuses ``metrics.paired_bootstrap`` untouched; what is at stake is that the CLI/report
wire it for ``--diversified-baseline`` + ``--bootstrap`` only, with the same seed,
and that the key is omitted (not null) when absent.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from trading.cli import app
from trading.engine import BacktestResult, EquityPoint
from trading.metrics import assess_diversified_baseline, paired_bootstrap
from trading.report import result_to_dict, summarize
from trading.types import Portfolio

runner = CliRunner()
_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def _curve(returns: list[float]) -> list[EquityPoint]:
    pts = [EquityPoint(_EPOCH, 1000.0, 1.0)]
    eq = 1000.0
    for i, r in enumerate(returns, start=1):
        eq *= 1.0 + r
        pts.append(EquityPoint(_EPOCH + timedelta(days=i), eq, 1.0))
    return pts


def _run(curve: list[EquityPoint]) -> BacktestResult:
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=1000.0,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=1000.0),
    )


def _pair(n: int = 400) -> tuple[BacktestResult, BacktestResult]:
    rng = random.Random(3)
    base = [rng.gauss(0.0, 0.02) for _ in range(n)]
    return _run(_curve([r + 0.0003 for r in base])), _run(_curve(base))


class TestReport:
    def test_constant_edge_wins_every_paired_resample(self) -> None:
        strat, base = _pair()
        paired = paired_bootstrap(strat.equity_curve, base.equity_curve, resamples=200, seed=1)
        assert paired is not None and paired.win_rate == 1.0
        rep = assess_diversified_baseline(strat, base, label="eq/x")
        text = summarize(strat, diversified_baseline=rep, baseline_paired=paired)
        assert "Beats baseline: 100.0% of 200 PAIRED resamples" in text
        assert "seed 1" in text

    def test_absent_without_paired(self) -> None:
        strat, base = _pair()
        rep = assess_diversified_baseline(strat, base, label="eq/x")
        assert "Beats baseline" not in summarize(strat, diversified_baseline=rep)

    def test_json_key_nested_and_omitted_when_absent(self) -> None:
        strat, base = _pair()
        rep = assess_diversified_baseline(strat, base, label="eq/x")
        paired = paired_bootstrap(strat.equity_curve, base.equity_curve, resamples=50, seed=2)
        with_p = result_to_dict(
            strat, mode="backtest", diversified_baseline=rep, baseline_paired=paired
        )
        without = result_to_dict(strat, mode="backtest", diversified_baseline=rep)
        assert with_p["diversified_baseline"]["paired"]["win_rate"] == 1.0
        assert "paired" not in without["diversified_baseline"]
        assert with_p["schema_version"] == 1

    def test_too_short_series_says_so_when_requested(self) -> None:
        strat, base = _run(_curve([0.01] * 5)), _run(_curve([0.0] * 5))
        rep = assess_diversified_baseline(strat, base, label="eq/x")
        from trading.metrics import compute
        from trading.report import diversified_baseline_lines

        lines = diversified_baseline_lines(
            rep, compute(strat), 6, paired=None, paired_requested=True
        )
        assert any("no paired win rate against the diversified baseline" in x for x in lines)

    def test_indecisive_win_rate_is_flagged(self) -> None:
        strat, _ = _pair()
        rng = random.Random(8)
        other = _run(_curve([rng.gauss(0.0, 0.02) for _ in range(400)]))
        paired = paired_bootstrap(strat.equity_curve, other.equity_curve, resamples=200, seed=1)
        assert paired is not None
        rep = assess_diversified_baseline(strat, other, label="eq/x")
        text = summarize(strat, diversified_baseline=rep, baseline_paired=paired)
        decisive = paired.win_rate <= 0.05 or paired.win_rate >= 0.95
        assert ("not decisive" in text) == (not decisive)


def _backtest(out: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "sma_crossover",
            "--symbols",
            "AAA,BBB",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out / "equity_curve.csv"),
            "--from",
            "2020-01-01",
            "--to",
            "2021-01-01",
            "--diversified-baseline",
            "--baseline-basket",
            "CCC,DDD",
            *extra,
        ],
    )


class TestCli:
    def test_absent_without_bootstrap(self, tmp_path: Path) -> None:
        r = _backtest(tmp_path)
        assert r.exit_code == 0, r.output
        assert "Beats baseline" not in r.output
        assert (
            "paired"
            not in json.loads((tmp_path / "result.json").read_text())["diversified_baseline"]
        )

    def test_present_and_deterministic_with_bootstrap(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        args = ("--bootstrap", "--bootstrap-resamples", "100", "--bootstrap-seed", "7")
        ra = _backtest(a, *args)
        _backtest(b, *args)
        assert ra.exit_code == 0, ra.output
        line = next(x for x in ra.output.splitlines() if x.startswith("Beats baseline"))
        assert "of 100 PAIRED resamples" in line and "seed 7" in line
        pa = json.loads((a / "result.json").read_text())["diversified_baseline"]["paired"]
        pb = json.loads((b / "result.json").read_text())["diversified_baseline"]["paired"]
        assert pa == pb and pa["seed"] == 7 and pa["resamples"] == 100

    def test_bootstrap_without_baseline_adds_nothing(self, tmp_path: Path) -> None:
        r = runner.invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--symbols",
                "AAA,BBB",
                "--source",
                "synthetic",
                "--seed",
                "5",
                "--out",
                str(tmp_path / "e.csv"),
                "--from",
                "2020-01-01",
                "--to",
                "2021-01-01",
                "--bootstrap",
                "--bootstrap-resamples",
                "50",
            ],
        )
        assert r.exit_code == 0, r.output
        assert "Beats baseline" not in r.output
