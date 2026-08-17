"""Fast, no-infra tests for the ADR-0067 Monte Carlo path-shuffle reporting wiring.

Covers ``report.monte_carlo_lines`` / ``summarize(monte_carlo=...)`` /
``result_to_dict``'s additive ``monte_carlo`` key. Every fixture is a hand-built
curve/result; no engine, no network. The properties at stake mirror ADR-0066's
own wiring tests (``test_report_regimes.py``): a run that never asks for the
shuffle must be byte-identical to before this feature existed, the block must
reach both the terminal and ``result.json`` from a single computation, and the
``monte_carlo`` key is OMITTED (not ``null``) when absent, exactly like
``regimes`` — because a baseline ``result.json`` hash was already pinned by the
time this feature landed, and an unconditional ``null`` would move it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading.engine import BacktestResult, EquityPoint
from trading.metrics import MIN_BOOTSTRAP_OBSERVATIONS, monte_carlo_shuffle
from trading.report import monte_carlo_lines, result_to_dict, summarize, write_result_json
from trading.types import Portfolio


def _ts(day: int) -> datetime:
    return datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=day)


def _curve_from_returns(returns: list[float], start: float = 1000.0) -> list[EquityPoint]:
    equity = start
    points = [EquityPoint(_ts(0), equity)]
    for i, r in enumerate(returns, start=1):
        equity *= 1.0 + r
        points.append(EquityPoint(_ts(i), equity))
    return points


def _result(curve: list[EquityPoint]) -> BacktestResult:
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=curve[0].equity,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=curve[-1].equity),
    )


def _long_result() -> BacktestResult:
    returns = [0.0006 * ((-1) ** i) + 0.0001 * i for i in range(400)]
    return _result(_curve_from_returns(returns))


class TestSummaryIsByteIdenticalWithoutMonteCarlo:
    def test_no_monte_carlo_block_when_omitted(self) -> None:
        summary = summarize(_long_result())
        assert "Monte Carlo" not in summary

    def test_default_matches_explicit_none(self) -> None:
        result = _long_result()
        assert summarize(result) == summarize(result, monte_carlo=None)


class TestSummaryRendersTheBlockWhenSupplied:
    def test_the_block_prints_with_its_provenance(self) -> None:
        result = _long_result()
        report = monte_carlo_shuffle(result.equity_curve, resamples=100)
        summary = summarize(result, monte_carlo=report)
        assert "Monte Carlo shuffle (" in summary
        assert "Sharpe (order-invariant)" in summary
        assert "Max drawdown — actual path" in summary

    def test_too_short_to_shuffle_prints_only_the_note(self) -> None:
        result = _result(_curve_from_returns([0.001] * 5))
        report = monte_carlo_shuffle(result.equity_curve)
        lines = monte_carlo_lines(report)
        assert len(lines) == 1
        assert lines[0].startswith("  note:")

    def test_a_too_short_report_prints_the_floor_in_the_note(self) -> None:
        result = _result(_curve_from_returns([0.001] * 5))
        report = monte_carlo_shuffle(result.equity_curve)
        summary = summarize(result, monte_carlo=report)
        assert f"below the {MIN_BOOTSTRAP_OBSERVATIONS}" in summary


class TestResultJsonOmitsTheKeyWhenAbsent:
    """The convention this card must follow, matching ``regimes`` (ADR-0066)."""

    def test_key_absent_by_default(self) -> None:
        payload = result_to_dict(_long_result(), mode="backtest")
        assert "monte_carlo" not in payload

    def test_schema_version_unchanged(self) -> None:
        payload = result_to_dict(_long_result(), mode="backtest")
        assert payload["schema_version"] == 1

    def test_write_result_json_omits_key_too(self, tmp_path: Path) -> None:
        path = tmp_path / "result.json"
        write_result_json(_long_result(), path, mode="backtest")
        payload = json.loads(path.read_text())
        assert "monte_carlo" not in payload


class TestResultJsonCarriesTheComputedBlock:
    def test_key_present_and_asdict_shaped(self) -> None:
        result = _long_result()
        report = monte_carlo_shuffle(result.equity_curve, resamples=100)
        payload = result_to_dict(result, mode="backtest", monte_carlo=report)
        assert "monte_carlo" in payload
        block = payload["monte_carlo"]
        assert set(block.keys()) == {
            "resamples",
            "seed",
            "confidence",
            "observations",
            "sharpe",
            "actual_max_drawdown",
            "shuffled_low",
            "shuffled_median",
            "shuffled_high",
            "actual_percentile",
            "notes",
        }
        assert block["resamples"] == 100

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        result = _long_result()
        report = monte_carlo_shuffle(result.equity_curve, resamples=100)
        path = tmp_path / "result.json"
        write_result_json(result, path, mode="backtest", monte_carlo=report)
        payload = json.loads(path.read_text())
        assert payload["monte_carlo"]["observations"] > 0
        assert payload["monte_carlo"]["actual_max_drawdown"] is not None
