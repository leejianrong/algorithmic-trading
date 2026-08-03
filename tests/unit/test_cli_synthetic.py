"""CLI tests for the offline synthetic path — full stack, no network."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()

_COMMON = ["--symbols", "AAA,BBB", "--from", "2021-01-01", "--to", "2021-06-30"]


def test_backtest_source_synthetic_runs_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Final equity" in result.output
    assert out.exists()
    # header + at least ~120 trading-day rows over six months
    assert len(out.read_text().splitlines()) > 100


def test_backtest_target_vol_runs_offline(tmp_path: Path) -> None:
    out = tmp_path / "equity.csv"
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--source",
            "synthetic",
            "--target-vol",
            "0.05",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Final equity" in result.output


def test_backtest_target_vol_rejects_nonpositive(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--source",
            "synthetic",
            "--target-vol",
            "0",
            "--out",
            str(tmp_path / "e.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "target_volatility" in result.output


def test_unknown_source_is_rejected(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--source",
            "bogus",
            "--out",
            str(tmp_path / "e.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "source" in result.output.lower()


def test_paper_source_synthetic_runs_offline_and_terminates(tmp_path: Path) -> None:
    out = tmp_path / "paper"
    result = runner.invoke(
        app,
        [
            "paper",
            "--strategy",
            "sma_crossover",
            "--source",
            "synthetic",
            "--seed",
            "5",
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Paper session (once)" in result.output
    assert "Final equity" in result.output
    assert "completed bar(s)." in result.output
    # The session log, running-state JSON, and equity CSV are all persisted.
    assert (out / "paper_session.log").exists()
    assert (out / "paper_state.json").exists()
    assert (out / "equity_curve.csv").exists()
    # One logged line per completed bar (~120 trading days over six months).
    assert len((out / "paper_session.log").read_text().splitlines()) > 100


def test_gen_data_writes_cache_compatible_files_backtestable_offline(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    gen = runner.invoke(app, ["gen-data", "--seed", "5", "--out-dir", str(cache), *_COMMON])
    assert gen.exit_code == 0, gen.output
    assert sorted(p.name for p in cache.glob("*.csv")) == [
        "AAA_20210101_20210630_adj.csv",
        "BBB_20210101_20210630_adj.csv",
    ]

    # The yfinance adapter reads the generated cache with no network.
    out = tmp_path / "equity.csv"
    bt = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--source",
            "yfinance",
            "--cache-dir",
            str(cache),
            "--out",
            str(out),
            *_COMMON,
        ],
    )
    assert bt.exit_code == 0, bt.output
    assert "Final equity" in bt.output
