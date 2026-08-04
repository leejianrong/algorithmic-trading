"""CLI tests for the offline synthetic path — full stack, no network."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def test_backtest_source_csv_runs(tmp_path: Path) -> None:
    (tmp_path / "AAA.csv").write_text(
        "ts,open,high,low,close,volume\n"
        "2021-01-04,100,101,99,100,1000\n"
        "2021-01-05,100,102,100,101,1000\n"
        "2021-01-06,101,103,100,102,1000\n"
    )
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--symbols",
            "AAA",
            "--source",
            "csv",
            "--cache-dir",
            str(tmp_path),
            "--from",
            "2021-01-01",
            "--to",
            "2021-01-31",
            "--out",
            str(tmp_path / "e.csv"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Final equity" in result.output


def test_source_alpaca_without_credentials_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "buy_and_hold",
            "--source",
            "alpaca",
            "--out",
            str(tmp_path / "e.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "error" in result.output.lower()


def test_backtest_sector_caps_run(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--source",
            "synthetic",
            "--max-sector-exposure",
            "0.3",
            "--sector-map",
            "AAA:tech,BBB:energy",
            "--out",
            str(tmp_path / "e.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Final equity" in result.output


def test_sector_map_malformed_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "backtest",
            "--strategy",
            "equal_weight",
            "--source",
            "synthetic",
            "--max-sector-exposure",
            "0.3",
            "--sector-map",
            "nope",
            "--out",
            str(tmp_path / "e.csv"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "sector-map" in result.output


def test_paper_broker_alpaca_requires_live(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "paper",
            "--strategy",
            "buy_and_hold",
            "--source",
            "synthetic",
            "--broker",
            "alpaca",
            "--out",
            str(tmp_path / "p"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "live" in result.output.lower()


def test_paper_broker_unknown_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "paper",
            "--strategy",
            "buy_and_hold",
            "--source",
            "synthetic",
            "--broker",
            "bogus",
            "--out",
            str(tmp_path / "p"),
            *_COMMON,
        ],
    )
    assert result.exit_code == 2
    assert "broker" in result.output.lower()
