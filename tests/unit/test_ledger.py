"""Fast, offline tests for the cross-invocation trial ledger (ADR-0062, KAN-858).

Everything here is plain file I/O against ``tmp_path`` — no engine, no CLI, no
network. Three things are held down:

1. **Append-only durability.** A line, once written, is never rewritten; a
   truncated final line (what a crash mid-``write`` leaves) is dropped silently,
   and a malformed line anywhere else raises rather than corrupting the count an
   operator is about to trust.
2. **Round-tripping.** A written :class:`TrialRecord` reads back equal, including
   the ``symbols`` tuple that has no native JSON representation.
3. **``cumulative_trials`` is a plain sum**, and is what the CLI and
   ``metrics``/``sweep`` widen their deflation with.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trading.ledger import TrialLedger, TrialRecord

_RECORD = TrialRecord(
    timestamp="2026-08-17T00:00:00+00:00",
    command="backtest",
    strategy="sma_crossover",
    symbols=("AAA", "BBB"),
    date_from="2020-01-01",
    date_to="2021-01-01",
    interval="1d",
    market="us_equity",
    trial_count=1,
    observed_sharpe=1.23,
)


class TestEmptyLedger:
    def test_a_ledger_at_a_nonexistent_path_loads_empty(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "does_not_exist.jsonl")
        assert ledger.load() == []
        assert ledger.cumulative_trials() == 0

    def test_constructing_a_ledger_touches_nothing_on_disk(self, tmp_path: Path) -> None:
        """Building one to read a ledger that may not exist yet must be free."""
        path = tmp_path / "sub" / "ledger.jsonl"
        TrialLedger(path)
        assert not path.exists()
        assert not path.parent.exists()


class TestAppendAndLoadRoundTrip:
    def test_a_written_record_reads_back_equal(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        ledger.append(_RECORD)
        assert ledger.load() == [_RECORD]

    def test_symbols_round_trip_as_a_tuple_not_a_list(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        ledger.append(_RECORD)
        loaded = ledger.load()[0]
        assert isinstance(loaded.symbols, tuple)
        assert loaded.symbols == ("AAA", "BBB")

    def test_records_load_in_append_order(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        first = _RECORD
        second = replace(_RECORD, command="sweep", trial_count=24, timestamp="later")
        ledger.append(first)
        ledger.append(second)
        assert ledger.load() == [first, second]

    def test_an_empty_hypothesis_is_the_default_and_round_trips(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        ledger.append(_RECORD)
        assert ledger.load()[0].hypothesis == ""

    def test_a_given_hypothesis_round_trips_verbatim(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        with_hypothesis = replace(_RECORD, hypothesis="expect momentum to persist 5-20d")
        ledger.append(with_hypothesis)
        assert ledger.load()[0].hypothesis == "expect momentum to persist 5-20d"

    def test_a_none_observed_sharpe_round_trips_as_none_not_a_zero(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        ledger.append(replace(_RECORD, observed_sharpe=None))
        assert ledger.load()[0].observed_sharpe is None

    def test_append_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "ledger.jsonl"
        TrialLedger(path).append(_RECORD)
        assert path.exists()

    def test_each_append_is_exactly_one_line(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        ledger = TrialLedger(path)
        ledger.append(_RECORD)
        ledger.append(_RECORD)
        ledger.append(_RECORD)
        lines = path.read_text().splitlines()
        assert len(lines) == 3


class TestCumulativeTrials:
    def test_sums_trial_count_across_records(self, tmp_path: Path) -> None:
        ledger = TrialLedger(tmp_path / "ledger.jsonl")
        ledger.append(replace(_RECORD, trial_count=1))
        ledger.append(replace(_RECORD, trial_count=24, command="sweep"))
        ledger.append(replace(_RECORD, trial_count=5, command="sweep"))
        assert ledger.cumulative_trials() == 30

    def test_a_fresh_ledger_widens_nothing(self, tmp_path: Path) -> None:
        """The very first logged run must behave exactly as an unledgered one."""
        assert TrialLedger(tmp_path / "ledger.jsonl").cumulative_trials() == 0


class TestAppendOnlyDurability:
    """A crashed ledger under-reports; it never misreports (ADR-0048's rule, reused)."""

    def test_a_torn_final_line_is_dropped_silently(self, tmp_path: Path) -> None:
        """What a process killed mid-``write`` leaves behind on its last line."""
        path = tmp_path / "ledger.jsonl"
        ledger = TrialLedger(path)
        ledger.append(_RECORD)
        ledger.append(replace(_RECORD, trial_count=7))
        lines = path.read_text().splitlines()
        # Simulate a crash mid-write of a third record: a torn, unterminated tail.
        torn = lines[0] + "\n" + lines[1] + "\n" + '{"timestamp": "2026-08-17T01:00'
        path.write_text(torn)

        loaded = ledger.load()
        assert len(loaded) == 2
        assert ledger.cumulative_trials() == 1 + 7

    def test_a_torn_line_that_is_also_the_only_line_loads_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        path.write_text('{"timestamp": "2026-08-17T01:00')
        assert TrialLedger(path).load() == []

    def test_a_malformed_line_before_the_last_one_raises(self, tmp_path: Path) -> None:
        """Real corruption — not a crash tail — must not silently vanish."""
        path = tmp_path / "ledger.jsonl"
        ledger = TrialLedger(path)
        ledger.append(_RECORD)
        ledger.append(_RECORD)
        lines = path.read_text().splitlines()
        corrupted = "not json at all\n" + lines[1] + "\n"
        path.write_text(corrupted)

        with pytest.raises(Exception, match=r"(?i)expecting value|json"):
            ledger.load()

    def test_a_finished_file_is_a_prefix_of_a_larger_one(self, tmp_path: Path) -> None:
        """The append-only shape, stated directly: more appends only extend the file."""
        path = tmp_path / "ledger.jsonl"
        ledger = TrialLedger(path)
        ledger.append(_RECORD)
        after_one = path.read_bytes()
        ledger.append(replace(_RECORD, trial_count=2))
        after_two = path.read_bytes()
        assert after_two.startswith(after_one)
