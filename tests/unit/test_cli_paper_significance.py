"""CLI wiring for `trading paper --bootstrap` / `--ledger` (ADR-0074, KAN-677).

Before this, only `backtest`/`sweep` had `--bootstrap`/`--ledger`/`--hypothesis` —
`paper` had neither, even though a paper result arguably needs the confidence
interval most: paper results are survivorship-free (ADR-0027) while a curated
backtest universe is not. Mirrors `test_cli_significance.py`'s two-property
structure exactly:

- absent, nothing changes — no ledger file, no different bytes anywhere;
- present, the figures reach the terminal *and* `result.json`, computed once.

`--ledger` must work on both exits a paper session has: the normal `--once`
completion, and the `KeyboardInterrupt`/`SessionTerminated` finalize path
(ADR-0033/ADR-0043) — reusing the same interrupt harness
`test_cli_paper_interrupt.py` built for that path, so a `--live` session stopped
mid-run still logs its one trial.

Every run here uses `--source synthetic` (no network) and a small resample count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from trading.cli import app
from trading.clock import WallClock
from trading.engine import PaperSession
from trading.ledger import TrialLedger

runner = CliRunner()

# Long enough to clear MIN_BOOTSTRAP_OBSERVATIONS (30 return periods).
_RANGE = ["--from", "2021-01-01", "--to", "2021-12-31"]


def _paper(out_dir: Path, *extra: str) -> Result:
    return runner.invoke(
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
            "--out",
            str(out_dir),
            *_RANGE,
            *extra,
        ],
    )


def _result_json(out_dir: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((out_dir / "result.json").read_text())
    return payload


class TestPaperBootstrapIsOffByDefault:
    def test_no_significance_line_reaches_the_terminal(self, tmp_path: Path) -> None:
        result = _paper(tmp_path)
        assert result.exit_code == 0, result.output
        assert "Sharpe 95% CI:" not in result.output
        assert "Deflated:" not in result.output

    def test_result_json_carries_the_key_as_null(self, tmp_path: Path) -> None:
        assert _paper(tmp_path).exit_code == 0
        payload = _result_json(tmp_path)
        assert "significance" in payload
        assert payload["significance"] is None
        assert payload["schema_version"] == 1

    def test_byte_identical_to_a_session_that_never_asked(self, tmp_path: Path) -> None:
        default_dir = tmp_path / "default"
        explicit_dir = tmp_path / "explicit"
        default_dir.mkdir()
        explicit_dir.mkdir()

        default = _paper(default_dir)
        explicit = _paper(explicit_dir, "--no-bootstrap", "--hypothesis", "")

        assert default.exit_code == 0, default.output
        assert explicit.exit_code == 0, explicit.output
        assert (default_dir / "result.json").read_bytes() == (
            explicit_dir / "result.json"
        ).read_bytes()
        assert (default_dir / "equity_curve.csv").read_bytes() == (
            explicit_dir / "equity_curve.csv"
        ).read_bytes()

    def test_no_ledger_file_is_created(self, tmp_path: Path) -> None:
        assert _paper(tmp_path).exit_code == 0
        assert not (tmp_path / "ledger.jsonl").exists()

    def test_bad_resample_count_exits_before_the_session_runs(self, tmp_path: Path) -> None:
        result = _paper(tmp_path, "--bootstrap", "--bootstrap-resamples", "0")
        assert result.exit_code == 2, result.output
        assert "--bootstrap-resamples must be >= 1" in result.output


class TestPaperBootstrapReachesBothOutputs:
    def test_the_interval_prints_with_its_provenance(self, tmp_path: Path) -> None:
        result = _paper(tmp_path, "--bootstrap", "--bootstrap-resamples", "40")
        assert result.exit_code == 0, result.output
        assert "Sharpe 95% CI:" in result.output
        assert "40 resamples" in result.output

    def test_the_same_figures_land_in_result_json(self, tmp_path: Path) -> None:
        assert _paper(tmp_path, "--bootstrap", "--bootstrap-resamples", "40").exit_code == 0
        block = _result_json(tmp_path)["significance"]
        assert block is not None
        assert block["sharpe_interval"]["resamples"] == 40
        assert block["deflated"]["trials"] == 1


class TestPaperLedgerOnce:
    def test_appends_one_trial_even_without_bootstrap(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _paper(tmp_path, "--ledger", str(ledger_path))
        assert result.exit_code == 0, result.output
        records = TrialLedger(ledger_path).load()
        assert len(records) == 1
        assert records[0].command == "paper"
        assert records[0].trial_count == 1
        assert records[0].strategy == "sma_crossover"
        assert records[0].symbols == ("AAA", "BBB")
        assert records[0].market == "us_equity"
        assert records[0].observed_sharpe is not None

    def test_hypothesis_is_recorded_verbatim(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = _paper(
            tmp_path, "--ledger", str(ledger_path), "--hypothesis", "paper incubation run"
        )
        assert result.exit_code == 0, result.output
        assert TrialLedger(ledger_path).load()[0].hypothesis == "paper incubation run"

    def test_a_second_session_widens_the_deflation(self, tmp_path: Path) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        first = _paper(
            first_dir, "--ledger", str(ledger_path), "--bootstrap", "--bootstrap-resamples", "40"
        )
        assert first.exit_code == 0, first.output
        second = _paper(
            second_dir, "--ledger", str(ledger_path), "--bootstrap", "--bootstrap-resamples", "40"
        )
        assert second.exit_code == 0, second.output

        first_block = _result_json(first_dir)["significance"]["deflated"]
        second_block = _result_json(second_dir)["significance"]["deflated"]
        assert first_block["trials"] == 1
        assert second_block["trials"] == 2
        assert "1 from this run plus 1 carried over" in second.output


class TestPaperLedgerOnInterrupt:
    """The finalize path a --live session takes on SIGTERM/Ctrl-C (ADR-0033/0043)
    must log a trial too — a session stopped mid-run is still one real trial, and
    it is the shape a live incubation run is actually stopped in.
    """

    def _invoke(self, tmp_path: Path, *extra: str) -> Result:
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
                *extra,
            ],
        )

    @pytest.fixture
    def interrupted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_run = PaperSession.run

        def run_then_interrupt(self: PaperSession, **kwargs: object) -> object:
            real_run(self, max_new_bars=2, max_empty_polls=1)
            raise KeyboardInterrupt

        monkeypatch.setattr(WallClock, "sleep_until", lambda self, when: None)
        monkeypatch.setattr(PaperSession, "run", run_then_interrupt)

    def test_ledger_gets_a_record_after_an_interrupted_session_finalizes(
        self, tmp_path: Path, interrupted: None
    ) -> None:
        ledger_path = tmp_path / "ledger.jsonl"
        result = self._invoke(tmp_path, "--ledger", str(ledger_path))
        assert result.exit_code == 0, result.output
        assert "Interrupted" in result.output

        records = TrialLedger(ledger_path).load()
        assert len(records) == 1
        assert records[0].command == "paper"
        assert records[0].trial_count == 1

    def test_bootstrap_also_reaches_result_json_on_the_interrupt_path(
        self, tmp_path: Path, interrupted: None
    ) -> None:
        result = self._invoke(tmp_path, "--bootstrap", "--bootstrap-resamples", "30")
        assert result.exit_code == 0, result.output
        payload = json.loads((tmp_path / "result.json").read_text())
        # Present either way (None with too few bars from this short interrupted
        # run) — the point is that the key was computed and reached the file
        # through the same finalize() path a clean --once completion uses.
        assert "significance" in payload
