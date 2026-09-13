"""Fast, no-infra tests for the live-session dashboard reader (ADR-0075).

Hand-writes ``paper_state.json`` / ``paper_session.log`` / ``fill_divergence.csv``
fixtures into a temp directory exactly as `trading.cli`'s ``_persist_state`` /
``_format_bar`` / `trading.divergence.DivergenceJournal` would, and asserts
:mod:`trading.dashboard.live_payload` normalizes them the same way a real
session's artifacts would be read — including every "this file is momentarily
absent or incomplete" case the module promises never to crash on. No engine, no
network, no FastAPI, and no real `PaperSession`.
"""

from __future__ import annotations

import json
from pathlib import Path

from trading.dashboard.live_payload import (
    build_live_payload,
    parse_log_line,
    read_divergence_summary,
    read_log_lines,
    read_state,
)
from trading.divergence import CSV_COLUMNS, MIN_PAIRED_FILLS

_STATE = {
    "ts": "2026-09-14T13:35:00+00:00",
    "equity": 100234.56,
    "exposure": 0.254,
    "cash": 74829.10,
    "halted": False,
    "positions": {"AAPL": {"qty": 12.5, "avg_price": 231.40}},
}

_LOG_LINES = [
    "2026-09-14 13:30  decision: (hold)  |  equity: $100,000.00  exposure: 0.0%",
    (
        "2026-09-14 13:35  decision: target AAPL 25%  |  "
        "fills: BUY 12.5000 AAPL @ 231.4000  |  equity: $100,234.56  exposure: 25.4%"
    ),
]


def _write_state(out: Path, state: dict[str, object]) -> None:
    (out / "paper_state.json").write_text(json.dumps(state, indent=2) + "\n")


def _write_log(out: Path, lines: list[str]) -> None:
    (out / "paper_session.log").write_text("\n".join(lines) + "\n")


def _write_divergence_csv(out: Path, rows: list[dict[str, str]]) -> None:
    import csv

    with (out / "fill_divergence.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _divergence_row(realized_bps: float, modelled_bps: float = 5.0) -> dict[str, str]:
    row = dict.fromkeys(CSV_COLUMNS, "")
    row.update(
        {
            "submitted_ts": "2026-09-14T13:30:00+00:00",
            "symbol": "AAPL",
            "side": "buy",
            "live_outcome": "filled",
            "model_outcome": "filled",
            "realized_slippage_bps": repr(realized_bps),
            "modelled_slippage_bps": repr(modelled_bps),
            "outcome_diverged": "false",
        }
    )
    return row


# --- read_state ---------------------------------------------------------------


def test_read_state_normal_mid_session(tmp_path: Path) -> None:
    _write_state(tmp_path, _STATE)
    state = read_state(tmp_path)
    assert state is not None
    assert state["equity"] == 100234.56
    assert state["halted"] is False
    assert state["positions"]["AAPL"]["qty"] == 12.5


def test_read_state_halted(tmp_path: Path) -> None:
    halted_state = dict(_STATE, halted=True)
    _write_state(tmp_path, halted_state)
    state = read_state(tmp_path)
    assert state is not None
    assert state["halted"] is True


def test_read_state_missing_file_returns_none(tmp_path: Path) -> None:
    # Simulates the race before the session's first completed bar: the
    # directory exists (created by --out) but the state file does not yet.
    assert read_state(tmp_path) is None


def test_read_state_missing_directory_returns_none(tmp_path: Path) -> None:
    assert read_state(tmp_path / "does-not-exist") is None


def test_read_state_corrupt_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / "paper_state.json").write_text('{"ts": "2026-01-01", "equity": 100.0, "posi')
    assert read_state(tmp_path) is None


def test_read_state_non_object_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / "paper_state.json").write_text("[1, 2, 3]")
    assert read_state(tmp_path) is None


# --- read_log_lines / parse_log_line -------------------------------------------


def test_read_log_lines_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_log_lines(tmp_path) == []


def test_read_log_lines_reads_all_non_blank_lines(tmp_path: Path) -> None:
    _write_log(tmp_path, _LOG_LINES)
    assert read_log_lines(tmp_path) == _LOG_LINES


def test_parse_log_line_hold_bar() -> None:
    parsed = parse_log_line(_LOG_LINES[0])
    assert parsed is not None
    assert parsed["ts"] == "2026-09-14 13:30"
    assert parsed["decision"] == "(hold)"
    assert parsed["fills"] is None
    assert parsed["equity"] == 100000.00
    assert parsed["exposure"] == 0.0
    assert parsed["halted_now"] is False
    assert parsed["resumed_now"] is False


def test_parse_log_line_with_fill() -> None:
    parsed = parse_log_line(_LOG_LINES[1])
    assert parsed is not None
    assert parsed["decision"] == "target AAPL 25%"
    assert parsed["fills"] == "BUY 12.5000 AAPL @ 231.4000"
    assert parsed["equity"] == 100234.56
    assert abs(parsed["exposure"] - 0.254) < 1e-9


def test_parse_log_line_daily_stamp_has_no_time() -> None:
    line = "2026-09-14  decision: (hold)  |  equity: $1,000.00  exposure: 0.0%"
    parsed = parse_log_line(line)
    assert parsed is not None
    assert parsed["ts"] == "2026-09-14"


def test_parse_log_line_halt_and_resume() -> None:
    halt_line = (
        "2026-09-14 13:35  decision: (hold)  |  "
        "HALT: kill switch tripped — new entries blocked  |  "
        "equity: $900.00  exposure: 10.0%"
    )
    parsed = parse_log_line(halt_line)
    assert parsed is not None
    assert parsed["halted_now"] is True

    resume_line = (
        "2026-09-15 09:00  decision: (hold)  |  "
        "RESUME: kill switch re-armed — new entries allowed again  |  "
        "equity: $910.00  exposure: 0.0%"
    )
    parsed = parse_log_line(resume_line)
    assert parsed is not None
    assert parsed["resumed_now"] is True


def test_parse_log_line_clamp_and_reject() -> None:
    line = (
        "2026-09-14 13:35  decision: target AAPL 50%  |  "
        "CLAMP AAPL→10.0000 (gross cap)  |  "
        "REJECT BUY AAPL (halted: new entries blocked)  |  "
        "equity: $1,000.00  exposure: 10.0%"
    )
    parsed = parse_log_line(line)
    assert parsed is not None
    assert parsed["clamps"] == ["CLAMP AAPL→10.0000 (gross cap)"]
    assert parsed["rejections"] == ["REJECT BUY AAPL (halted: new entries blocked)"]


def test_parse_log_line_unparsable_returns_none() -> None:
    assert parse_log_line("not a bar line at all") is None
    # A torn write missing the trailing equity/exposure segment.
    assert parse_log_line("2026-09-14 13:35  decision: (hold)  |  fil") is None


# --- read_divergence_summary ----------------------------------------------------


def test_read_divergence_summary_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_divergence_summary(tmp_path) is None


def test_read_divergence_summary_header_only(tmp_path: Path) -> None:
    _write_divergence_csv(tmp_path, [])
    summary = read_divergence_summary(tmp_path)
    assert summary is not None
    assert summary["row_count"] == 0
    assert summary["paired_count"] == 0
    assert summary["sufficient_sample"] is False
    assert summary["mean_realized_slippage_bps"] is None


def test_read_divergence_summary_computes_paired_stats(tmp_path: Path) -> None:
    rows = [_divergence_row(4.0), _divergence_row(6.0), _divergence_row(5.0)]
    _write_divergence_csv(tmp_path, rows)
    summary = read_divergence_summary(tmp_path)
    assert summary is not None
    assert summary["row_count"] == 3
    assert summary["paired_count"] == 3
    assert summary["min_paired_fills"] == MIN_PAIRED_FILLS
    assert summary["sufficient_sample"] is False  # 3 < MIN_PAIRED_FILLS (30)
    assert abs(summary["mean_realized_slippage_bps"] - 5.0) < 1e-9
    assert abs(summary["median_realized_slippage_bps"] - 5.0) < 1e-9
    assert abs(summary["mean_modelled_slippage_bps"] - 5.0) < 1e-9


def test_read_divergence_summary_skips_unpaired_rows(tmp_path: Path) -> None:
    paired = _divergence_row(4.0)
    pending = dict.fromkeys(CSV_COLUMNS, "")
    pending.update(
        {"symbol": "AAPL", "side": "buy", "live_outcome": "pending", "model_outcome": "pending"}
    )
    _write_divergence_csv(tmp_path, [paired, pending])
    summary = read_divergence_summary(tmp_path)
    assert summary is not None
    assert summary["row_count"] == 2
    assert summary["paired_count"] == 1


# --- build_live_payload ---------------------------------------------------------


def test_build_live_payload_normal_mid_session(tmp_path: Path) -> None:
    _write_state(tmp_path, _STATE)
    _write_log(tmp_path, _LOG_LINES)
    _write_divergence_csv(tmp_path, [_divergence_row(4.5)])

    payload = build_live_payload(tmp_path)
    assert payload["out_dir"] == str(tmp_path)
    assert payload["state"]["equity"] == 100234.56
    assert payload["log_tail"] == _LOG_LINES
    assert payload["chart"]["start_ts"] == "2026-09-14 13:30"
    assert payload["chart"]["end_ts"] == "2026-09-14 13:35"
    assert payload["chart"]["equity_points"] != ""
    assert payload["divergence"]["row_count"] == 1


def test_build_live_payload_halted_session(tmp_path: Path) -> None:
    _write_state(tmp_path, dict(_STATE, halted=True))
    halt_lines = [
        *_LOG_LINES,
        (
            "2026-09-14 13:40  decision: (hold)  |  "
            "HALT: kill switch tripped — new entries blocked  |  "
            "equity: $80,000.00  exposure: 10.0%"
        ),
    ]
    _write_log(tmp_path, halt_lines)

    payload = build_live_payload(tmp_path)
    assert payload["state"]["halted"] is True
    assert payload["chart"]["end_ts"] == "2026-09-14 13:40"


def test_build_live_payload_no_divergence_csv(tmp_path: Path) -> None:
    _write_state(tmp_path, _STATE)
    _write_log(tmp_path, _LOG_LINES)
    # No fill_divergence.csv written at all: a session run without --divergence.

    payload = build_live_payload(tmp_path)
    assert payload["divergence"] is None


def test_build_live_payload_directory_with_nothing_written_yet(tmp_path: Path) -> None:
    # Simulates the moment right after --out is created and before the first
    # completed bar: no state, no log, no divergence CSV.
    payload = build_live_payload(tmp_path)
    assert payload["state"] is None
    assert payload["log_tail"] == []
    assert payload["chart"]["equity_points"] == ""
    assert payload["chart"]["start_ts"] is None
    assert payload["divergence"] is None


def test_build_live_payload_tolerates_torn_log_tail(tmp_path: Path) -> None:
    # Simulate catching an append mid-write: the last line has no trailing
    # equity/exposure segment yet.
    _write_state(tmp_path, _STATE)
    torn = [*_LOG_LINES, "2026-09-14 13:40  decision: (hold)  |  fil"]
    _write_log(tmp_path, torn)

    payload = build_live_payload(tmp_path)
    # The well-formed lines still parse into the chart; the torn one is dropped.
    assert payload["chart"]["end_ts"] == "2026-09-14 13:35"
    # But the raw tail still surfaces every line verbatim, torn or not — the
    # log-tail panel is a plain text view, not something we silently truncate.
    assert payload["log_tail"] == torn


def test_build_live_payload_respects_log_tail_lines_limit(tmp_path: Path) -> None:
    _write_state(tmp_path, _STATE)
    many_lines = [
        f"2026-09-14 {9 + i // 60:02d}:{i % 60:02d}  decision: (hold)  |  "
        f"equity: $1,000.00  exposure: 0.0%"
        for i in range(10)
    ]
    _write_log(tmp_path, many_lines)

    payload = build_live_payload(tmp_path, log_tail_lines=3)
    assert payload["log_tail"] == many_lines[-3:]


def test_build_live_payload_respects_max_chart_points(tmp_path: Path) -> None:
    _write_state(tmp_path, _STATE)
    many_lines = [
        f"2026-09-14 {9 + i // 60:02d}:{i % 60:02d}  decision: (hold)  |  "
        f"equity: $1,000.00  exposure: 0.0%"
        for i in range(10)
    ]
    _write_log(tmp_path, many_lines)

    payload = build_live_payload(tmp_path, max_chart_points=4)
    # Only the last 4 parsed points feed the chart geometry.
    assert payload["chart"]["start_ts"] == "2026-09-14 09:06"
    assert payload["chart"]["end_ts"] == "2026-09-14 09:09"
