"""``result.json`` and the static dashboard carry the divergence block (ADR-0086).

The block is what ``render_report`` already prints, as data. Three things matter:
it is **omitted** (not null) when ``paper --divergence`` was not passed, so every
other run's bytes stay put; it reuses ``summarize``/``_verdict``/``divergence_rows``
rather than recomputing; and the panel says what the terminal says — in particular
"neither confirmed nor refuted" below ``MIN_PAIRED_FILLS``, and the venue-fee
caveat that this report cannot measure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading.cli import app
from trading.config import CostConfig
from trading.dashboard.payload import build_payload
from trading.dashboard.static_export import render_html
from trading.divergence import (
    MIN_PAIRED_FILLS,
    OUTCOME_FILLED,
    FillDivergence,
    Settlement,
    divergence_rows,
    divergence_to_dict,
    render_report,
    summarize,
)
from trading.engine import BacktestResult, EquityPoint
from trading.report import RESULT_SCHEMA_VERSION, result_to_dict, write_result_json
from trading.types import Order, Portfolio, Side

START = datetime(2026, 1, 5, tzinfo=UTC)


def _record(
    realized_bps: float, side: Side = Side.BUY, lag_days: int = 1, symbol: str = "AAA"
) -> FillDivergence:
    reference = 100.0
    sign = 1.0 if side is Side.BUY else -1.0
    return FillDivergence(
        order=Order(symbol, side, qty=1),
        submitted_at=START,
        submitted_ts=START,
        reference_price=reference,
        reference_ts=START + timedelta(days=lag_days),
        live=Settlement(
            OUTCOME_FILLED,
            ts=START,
            observed_at=START,
            qty=1.0,
            price=reference * (1 + sign * realized_bps / 10_000.0),
        ),
        shadow=Settlement(
            OUTCOME_FILLED,
            ts=START,
            observed_at=START,
            qty=1.0,
            price=reference * (1 + sign * 0.0005),
        ),
    )


def _result() -> BacktestResult:
    curve = [EquityPoint(START, 1000.0, 0.0), EquityPoint(START + timedelta(days=1), 1010.0, 0.5)]
    return BacktestResult(
        symbols=["AAA"],
        starting_cash=1000.0,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=1010.0),
    )


def _block(records: list[FillDivergence], costs: CostConfig | None = None) -> dict[str, object]:
    return divergence_to_dict(summarize(records, costs=costs), records)


class TestBlockMirrorsThePrintedReport:
    def test_aggregates_are_the_summary_fields(self) -> None:
        records = [_record(10.0), _record(20.0, Side.SELL), _record(30.0)]
        summary = summarize(records)
        block = divergence_to_dict(summary, records)
        assert block["comparable"] == 3
        assert block["mean_realized_bps"] == summary.mean_realized_bps
        assert block["median_realized_bps"] == summary.median_realized_bps
        assert block["stdev_realized_bps"] == summary.stdev_realized_bps
        assert block["min_samples"] == MIN_PAIRED_FILLS
        assert block["price_notion"] == summary.price_notion

    def test_rows_are_divergence_rows_verbatim(self) -> None:
        records = [_record(10.0), _record(20.0, Side.SELL)]
        assert _block(records)["rows"] == divergence_rows(records)

    def test_buy_sell_split(self) -> None:
        by_side = _block([_record(10.0), _record(30.0), _record(20.0, Side.SELL)])["by_side"]
        assert by_side["buy"]["n"] == 2  # type: ignore[index]
        assert by_side["buy"]["mean_realized_bps"] == pytest.approx(20.0)  # type: ignore[index]
        assert by_side["sell"]["n"] == 1  # type: ignore[index]

    def test_verdict_is_the_reports_own_sentence(self) -> None:
        records = [_record(20.0) for _ in range(5)]
        summary = summarize(records)
        block = divergence_to_dict(summary, records)
        assert block["verdict"] in render_report(summary, records)
        assert "neither confirmed nor refuted" in str(block["verdict"])
        assert block["conclusive"] is False

    def test_a_conclusive_sample_says_conclusive(self) -> None:
        block = _block([_record(20.0) for _ in range(MIN_PAIRED_FILLS)])
        assert block["conclusive"] is True
        assert "neither confirmed nor refuted" not in str(block["verdict"])

    def test_staleness_block_only_when_the_tape_skipped(self) -> None:
        assert _block([_record(10.0), _record(10.0)])["reference_staleness"] is None
        stale = _block([_record(10.0), _record(10.0, lag_days=4)])["reference_staleness"]
        assert stale is not None
        assert stale["stale_fills"] == 1  # type: ignore[index]

    def test_round_trips_through_json(self) -> None:
        block = _block([_record(10.0), _record(20.0, Side.SELL, lag_days=3)])
        assert json.loads(json.dumps(block)) == block


class TestResultJson:
    def test_key_omitted_by_default(self) -> None:
        assert "divergence" not in result_to_dict(_result(), mode="paper")

    def test_default_equals_explicit_none_byte_for_byte(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        write_result_json(_result(), a, mode="paper")
        write_result_json(_result(), b, mode="paper", divergence=None)
        assert a.read_bytes() == b.read_bytes()

    def test_key_present_and_schema_unchanged(self) -> None:
        block = _block([_record(10.0)])
        payload = result_to_dict(_result(), mode="paper", divergence=block)
        assert payload["divergence"] == block
        assert payload["schema_version"] == RESULT_SCHEMA_VERSION == 1

    def test_only_the_new_key_differs(self) -> None:
        plain = result_to_dict(_result(), mode="paper")
        with_block = result_to_dict(_result(), mode="paper", divergence=_block([_record(10.0)]))
        del with_block["divergence"]
        assert with_block == plain


def _html(block: dict[str, object] | None, market: str = "us_equity") -> str:
    doc = result_to_dict(_result(), mode="paper", market=market, divergence=block)
    return render_html(build_payload(doc))


class TestStaticPanel:
    def test_absent_without_the_key(self) -> None:
        assert "Fill divergence" not in _html(None)

    def test_present_with_the_key(self) -> None:
        html = _html(_block([_record(10.0), _record(20.0, Side.SELL)]))
        assert "Fill divergence" in html
        assert "Paired fills" in html
        assert "AAA" in html

    def test_thin_sample_wording_reaches_the_page(self) -> None:
        assert "neither confirmed nor refuted" in _html(_block([_record(10.0)]))

    def test_no_external_references(self) -> None:
        html = _html(_block([_record(10.0)]))
        assert "http://" not in html and "https://" not in html

    def test_fee_caveat_for_a_crypto_cost_model(self) -> None:
        html = _html(_block([_record(10.0)], CostConfig.crypto()), market="crypto_24_7")
        assert "NOT MEASURED BY" in html

    def test_no_fee_caveat_for_equities(self) -> None:
        assert "NOT MEASURED BY" not in _html(_block([_record(10.0)]))

    def test_symbols_are_escaped(self) -> None:
        html = _html(_block([_record(10.0, symbol="<b>X</b>")]))
        assert "<b>X</b>" not in html.split('id="run-data"')[0]


class TestCliResultJson:
    def _run(self, out: Path, *extra: str) -> None:
        result = CliRunner().invoke(
            app,
            [
                "paper", "--strategy", "buy_and_hold", "--symbols", "AAA,BBB",
                "--from", "2024-01-02", "--to", "2024-03-01", "--source", "synthetic",
                "--seed", "7", "--cash", "10000", "--out", str(out), *extra,
            ],
        )  # fmt: skip
        assert result.exit_code == 0, result.output

    def test_flag_adds_only_the_key(self, tmp_path: Path) -> None:
        plain, tracked = tmp_path / "plain", tmp_path / "tracked"
        self._run(plain)
        self._run(tracked, "--divergence")
        plain_doc = json.loads((plain / "result.json").read_text())
        tracked_doc = json.loads((tracked / "result.json").read_text())
        assert "divergence" not in plain_doc
        block = tracked_doc.pop("divergence")
        assert tracked_doc == plain_doc
        assert block["orders"] == len(block["rows"])
        assert block["comparable"] >= 1
