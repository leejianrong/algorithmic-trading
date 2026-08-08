"""A crashed session keeps the divergence rows it had already measured (ADR-0048).

``fill_divergence.csv`` is the measurement a live paper session exists to collect,
and it is the one artifact that is **not** reconstructible from the survivors: the
session log carries realized fills but no modelled counterfactual, no reference
price and no slippage, and ``paper_state.json`` is a single overwritten snapshot.
Before this slice every row lived in memory on the :class:`ShadowBroker` until
``finalize()``, so any exit that did not unwind — ``kill -9``, power loss, a
suspended laptop, an unhandled exception — lost the whole day.

Four things these tests hold down:

1. **Rows reach disk as they settle**, and the file a crash leaves behind is a
   byte-for-byte *prefix* of the file the run would have finished with.
2. **A row is never written and then contradicted.** An order parked at the venue
   (ADR-0036) and a fill about to be amended into a partial (ADR-0033) are both
   still open when the journal is offered rows, so neither can appear early.
3. **The finished file is unchanged.** Journaling adds rows to disk sooner; it adds
   nothing to the artifact.
4. **The writer cannot perturb the live path.** ADR-0038's structural guarantee now
   has file I/O inside it, so a journal that raises must still leave the run
   bit-for-bit what it would have been, with the order at the venue.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest
from typer.testing import CliRunner

from trading.broker import SimulatedBroker
from trading.brokers.alpaca import AlpacaBroker
from trading.clock import FakeClock
from trading.config import RiskConfig
from trading.data.alpaca_client import STATUS_CANCELED, FakeAlpacaClient
from trading.data.fake import FakeAdapter
from trading.divergence import (
    OUTCOME_PARTIAL,
    OUTCOME_PENDING,
    DivergenceJournal,
    FillDivergence,
    ShadowBroker,
    write_divergence_csv,
)
from trading.engine import BarOutcome, Engine
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Bar, Order, Portfolio, Position, Side

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trading.interfaces import Broker

runner = CliRunner()

START = datetime(2026, 1, 5, tzinfo=UTC)


def _bars(symbol: str, opens: list[float]) -> list[Bar]:
    """A series indexed by its OPEN, with a deliberately different close.

    Same fixture shape as ``test_divergence.py`` and for the same reason: a bar
    whose close equals its open cannot tell a correct reference price from one
    taken off the close.
    """
    return [
        Bar(
            symbol=symbol,
            ts=START + timedelta(days=i),
            open=price,
            high=price * 1.03,
            low=price * 0.99,
            close=price * 1.02,
            volume=1_000,
        )
        for i, price in enumerate(opens)
    ]


def _slice(bars: list[Bar]) -> dict[str, Bar]:
    return {bar.symbol: bar for bar in bars}


def _oscillating(symbol: str, count: int = 90) -> list[Bar]:
    """A series that crosses its own 10/20 SMA repeatedly, so orders keep settling.

    Several rounds of entry and exit is what makes the prefix assertions mean
    something: one row proves a row can be written, a stream proves the file keeps
    up with the run.
    """
    prices = [100.0 + 18.0 * math.sin(i / 7.0) + 4.0 * math.sin(i / 2.3) for i in range(count)]
    return _bars(symbol, prices)


def _clock(step_seconds: int = 60) -> FakeClock:
    base = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    return FakeClock(base, [base + timedelta(seconds=step_seconds * i) for i in range(1, 500)])


class _BoomJournal:
    """A journal that cannot write. The disk-full / bad-path / read-only case."""

    def __init__(self) -> None:
        self.calls = 0

    def append(self, records: Sequence[FillDivergence]) -> None:
        self.calls += 1
        raise OSError(28, "No space left on device")


class TestRowsReachDiskAsTheySettle:
    """The whole point: what is measured is on disk before the run ends."""

    def test_a_settled_row_is_written_on_the_bar_it_settled(self, tmp_path: Path) -> None:
        path = tmp_path / "fill_divergence.csv"
        journal = DivergenceJournal(path)
        live = SimulatedBroker(Portfolio(cash=10_000.0))
        shadow = ShadowBroker(live, _clock(), journal=journal)

        # Nothing has settled: a well-formed, empty file, not a missing one.
        assert path.read_text().splitlines() == [
            ",".join(
                [
                    "submitted_ts",
                    "submitted_at",
                    "symbol",
                    "side",
                    "order_qty",
                    "reference_price",
                    "live_outcome",
                    "live_ts",
                    "live_qty",
                    "live_price",
                    "live_commission",
                    "live_reason",
                    "model_outcome",
                    "model_qty",
                    "model_price",
                    "model_commission",
                    "model_reason",
                    "realized_slippage_bps",
                    "modelled_slippage_bps",
                    "slippage_error_bps",
                    "price_difference",
                    "qty_divergence",
                    "latency_seconds",
                    "outcome_diverged",
                ]
            )
        ]

        shadow.submit(Order("AAA", Side.BUY, qty=5))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        rows = path.read_text().splitlines()
        assert len(rows) == 2, "the settled order should already be on disk"
        assert rows[1].split(",")[2:4] == ["AAA", "buy"]
        assert journal.rows == 1

    def test_the_journal_is_a_byte_prefix_of_the_finished_file(self, tmp_path: Path) -> None:
        """A crashed run's file is the finished file, truncated — not a variant.

        That is what makes the survivor usable with no special tooling: the same
        header, the same columns, the same rendering, the same settlement order.
        """
        path = tmp_path / "fill_divergence.csv"
        shadow = ShadowBroker(
            SimulatedBroker(Portfolio(cash=100_000.0)),
            _clock(),
            journal=DivergenceJournal(path),
        )
        engine = Engine(
            FakeAdapter(_oscillating("AAA")),
            shadow,
            Guardrails(RiskConfig()),
        )
        engine.run(get_strategy("sma_crossover"), ["AAA"], START, START + timedelta(days=200))

        crashed = path.read_bytes()  # what a kill -9 at this instant would leave
        assert crashed.count(b"\n") > 1, "the run produced no rows to journal"

        finished = tmp_path / "finished.csv"
        write_divergence_csv(shadow.divergences, finished)
        assert finished.read_bytes().startswith(crashed)


class TestLateSettlingRowsAreNotContradicted:
    """A row written early and corrected later is worse than one written late."""

    def test_an_order_parked_at_the_venue_is_not_journaled(self, tmp_path: Path) -> None:
        """ADR-0036: a DAY order placed while the market is shut just sits there.

        The model filled it at the next open; the venue has said nothing. Writing
        that row now would publish ``live=pending`` for an order that may well fill
        on the next poll, so it stays in memory and reaches the file only if the
        session finalizes — where ``pending`` is the truthful final answer.
        """
        path = tmp_path / "fill_divergence.csv"
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        shadow = ShadowBroker(live, _clock(), journal=DivergenceJournal(path))

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        assert path.read_text().splitlines()[1:] == []  # header only
        assert shadow.divergences[0].live.outcome == OUTCOME_PENDING
        assert live.pending_order_ids == ("1",)

    def test_a_parked_order_is_journaled_once_the_venue_answers(self, tmp_path: Path) -> None:
        """...and it is journaled the moment it does settle, not only at the end."""
        path = tmp_path / "fill_divergence.csv"
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock(), poll_timeout=timedelta(0))
        shadow = ShadowBroker(live, _clock(), journal=DivergenceJournal(path))

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))
        assert len(path.read_text().splitlines()) == 1

        client.set_order_status("1", STATUS_CANCELED)
        shadow.on_bar(_slice(_bars("AAA", [101.0])))

        assert len(path.read_text().splitlines()) == 2

    def test_a_partial_fill_reaches_the_file_only_as_partial(self, tmp_path: Path) -> None:
        """ADR-0033: the venue emits a Fill *and* a rejection for the same order.

        Inside one bar the live settlement is first recorded as ``filled`` and then
        amended to ``partial``. Journaling from ``_harvest`` — after attribution,
        not during it — is what keeps the intermediate state off the disk.
        """
        path = tmp_path / "fill_divergence.csv"
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0, auto_fill=False)
        live = AlpacaBroker(client, clock=_clock())
        shadow = ShadowBroker(live, _clock(), journal=DivergenceJournal(path))

        shadow.submit(Order("AAA", Side.BUY, qty=10))
        client.set_order_status("1", STATUS_CANCELED, filled_qty=4.0, filled_avg_price=100.5)
        shadow.on_bar(_slice(_bars("AAA", [100.0])))

        rows = path.read_text().splitlines()
        assert len(rows) == 2
        assert OUTCOME_PARTIAL in rows[1]
        assert shadow.divergences[0].live.outcome == OUTCOME_PARTIAL


class TestTheFinishedArtifactIsUnchanged:
    """Journaling changes *when* rows are on disk, never *what* the run produced."""

    def _run(self, journal_path: Path | None) -> tuple[object, list[FillDivergence]]:
        journal = DivergenceJournal(journal_path) if journal_path is not None else None
        shadow = ShadowBroker(SimulatedBroker(Portfolio(cash=100_000.0)), _clock(), journal=journal)
        engine = Engine(
            FakeAdapter(_oscillating("AAA")),
            shadow,
            Guardrails(RiskConfig()),
        )
        result = engine.run(
            get_strategy("sma_crossover"), ["AAA"], START, START + timedelta(days=200)
        )
        return result, shadow.divergences

    def test_journaled_and_unjournaled_runs_write_identical_bytes(self, tmp_path: Path) -> None:
        plain_result, plain_records = self._run(None)
        kept_result, kept_records = self._run(tmp_path / "journal.csv")

        assert kept_result == plain_result  # BacktestResult is a dataclass: the whole run
        plain_csv, kept_csv = tmp_path / "plain.csv", tmp_path / "kept.csv"
        write_divergence_csv(plain_records, plain_csv)
        write_divergence_csv(kept_records, kept_csv)
        assert kept_csv.read_bytes() == plain_csv.read_bytes()

    def test_the_final_write_never_truncates_what_is_already_there(self, tmp_path: Path) -> None:
        """``os.replace``, not truncate-then-write: a crash mid-finalize costs nothing.

        Rendering is made to explode part-way through, which is what a full disk
        during the final write would look like. The journal on disk must be exactly
        as it was.
        """
        path = tmp_path / "fill_divergence.csv"
        journal = DivergenceJournal(path)
        shadow = ShadowBroker(SimulatedBroker(Portfolio(cash=10_000.0)), _clock(), journal=journal)
        shadow.submit(Order("AAA", Side.BUY, qty=5))
        shadow.on_bar(_slice(_bars("AAA", [100.0])))
        journaled = path.read_bytes()

        class _Exploding(list[FillDivergence]):
            def __iter__(self) -> object:  # type: ignore[override]
                raise OSError(28, "No space left on device")

        with pytest.raises(OSError, match="No space"):
            write_divergence_csv(_Exploding(), path)

        assert path.read_bytes() == journaled
        assert not list(tmp_path.glob("*.tmp")), "the temp file was left behind"


class TestAFailingWriterCannotPerturbTheLivePath:
    """ADR-0038's structural guarantee, now with file I/O inside it."""

    def _run(self, broker: Broker) -> object:
        engine = Engine(
            FakeAdapter(_bars("AAA", [100.0, 101.0, 99.0, 103.0])),
            broker,
            Guardrails(RiskConfig.unlimited()),
        )
        return engine.run(get_strategy("buy_and_hold"), ["AAA"], START, START + timedelta(days=10))

    def test_an_exploding_journal_leaves_the_run_identical(self) -> None:
        plain = self._run(SimulatedBroker(Portfolio(cash=10_000.0)))

        journal = _BoomJournal()
        wrapped = ShadowBroker(SimulatedBroker(Portfolio(cash=10_000.0)), _clock(), journal=journal)
        broken = self._run(wrapped)

        assert broken == plain
        assert journal.calls >= 1, "the journal was never actually asked to write"

    def test_a_journal_failure_disables_the_shadow_and_says_so(self) -> None:
        wrapped = ShadowBroker(
            SimulatedBroker(Portfolio(cash=10_000.0)), _clock(), journal=_BoomJournal()
        )
        self._run(wrapped)

        assert not wrapped.enabled
        assert any("No space left on device" in message for message in wrapped.errors)
        # The report says a disabled shadow measured nothing after — never silent.
        assert wrapped.summary.errors

    def test_rows_measured_before_the_writer_failed_are_still_reported(self) -> None:
        """A failed append must not lose the row it was handed.

        ``_harvest`` has already closed it, and the journal cursor only advances on
        success, so the in-memory record survives for the end-of-run write even
        though it never reached the journal.
        """
        wrapped = ShadowBroker(
            SimulatedBroker(Portfolio(cash=10_000.0)), _clock(), journal=_BoomJournal()
        )
        wrapped.submit(Order("AAA", Side.BUY, qty=5))
        wrapped.on_bar(_slice(_bars("AAA", [100.0])))

        records = wrapped.divergences
        assert len(records) == 1
        assert records[0].symbol == "AAA"

    def test_the_order_still_reaches_the_venue_when_the_journal_fails(self) -> None:
        client = FakeAlpacaClient({"AAA": _bars("AAA", [100.0])}, cash=10_000.0)
        client.set_price("AAA", 100.0)
        live = AlpacaBroker(client, clock=_clock())
        wrapped = ShadowBroker(live, _clock(), journal=_BoomJournal())

        wrapped.submit(Order("AAA", Side.BUY, qty=3))
        fills = wrapped.on_bar(_slice(_bars("AAA", [100.0])))

        assert [f.qty for f in fills] == [pytest.approx(3.0)]
        assert wrapped.portfolio.position("AAA").qty == pytest.approx(3.0)
        assert not wrapped.enabled

    def test_an_unwritable_path_is_a_disabled_shadow_not_a_dead_session(
        self, tmp_path: Path
    ) -> None:
        """The real-world shape of the failure: the out directory cannot be written.

        Constructing the journal is the CLI's problem (it happens before any order),
        so this covers the half that runs *inside* the session: a path that becomes
        unwritable while it is running.
        """
        target = tmp_path / "fill_divergence.csv"
        journal = DivergenceJournal(target)
        wrapped = ShadowBroker(SimulatedBroker(Portfolio(cash=10_000.0)), _clock(), journal=journal)
        target.chmod(0o400)
        try:
            wrapped.submit(Order("AAA", Side.BUY, qty=5))
            fills = wrapped.on_bar(_slice(_bars("AAA", [100.0])))
        finally:
            target.chmod(0o600)

        assert [f.qty for f in fills] == [pytest.approx(5.0)]  # the live fill happened
        assert not wrapped.enabled
        assert wrapped.errors


class TestPaperStateIsAtomic:
    """``paper_state.json`` is rewritten every bar, so a crash lands in one."""

    def _outcome(self) -> BarOutcome:
        return BarOutcome(
            ts=START,
            fills=[],
            intents=[],
            submitted=[],
            clamps=[],
            guardrail_rejections=[],
            broker_rejections=[],
            halted_now=False,
            halted=False,
            equity=1_234.5,
            exposure=0.5,
        )

    def test_a_failed_write_leaves_the_previous_state_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trading import cli

        path = tmp_path / "paper_state.json"
        portfolio = Portfolio(cash=10.0, positions={"AAA": Position("AAA", 1.0, 100.0)})
        cli._persist_state(path, self._outcome(), portfolio)
        before = path.read_bytes()

        def boom(src: object, dst: object) -> None:
            raise OSError(28, "No space left on device")

        # ``os`` is one module object, so patching it here patches the CLI's view.
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="No space"):
            cli._persist_state(path, self._outcome(), Portfolio(cash=999.0))

        assert path.read_bytes() == before
        assert json.loads(path.read_text())["cash"] == pytest.approx(10.0)
        assert not list(tmp_path.glob("*.tmp")), "the temp file was left behind"

    def test_a_normal_write_replaces_the_file(self, tmp_path: Path) -> None:
        from trading import cli

        path = tmp_path / "paper_state.json"
        cli._persist_state(path, self._outcome(), Portfolio(cash=10.0))
        cli._persist_state(path, self._outcome(), Portfolio(cash=20.0))

        assert json.loads(path.read_text())["cash"] == pytest.approx(20.0)
        assert not list(tmp_path.glob("*.tmp"))

    def test_the_state_file_is_never_opened_for_writing_in_place(self, tmp_path: Path) -> None:
        """The property that makes truncation impossible, asserted directly.

        A read-only destination is writable by ``os.replace`` (renaming needs the
        *directory*, not the file) and is not writable by ``open("w")`` — which is
        what the old ``write_text`` did, and which truncates before it writes a
        byte. So this passes only if the bytes went somewhere else first.
        """
        from trading import cli

        path = tmp_path / "paper_state.json"
        cli._persist_state(path, self._outcome(), Portfolio(cash=10.0))
        path.chmod(0o400)

        cli._persist_state(path, self._outcome(), Portfolio(cash=20.0))

        assert json.loads(path.read_text())["cash"] == pytest.approx(20.0)


class TestASessionThatDiesMidFlight:
    """End to end through the real CLI, the way the loss was actually observed."""

    _ARGS: ClassVar[list[str]] = [
        "paper",
        "--strategy",
        "sma_crossover",
        "--symbols",
        "AAA,BBB",
        "--from",
        "2024-01-02",
        "--to",
        "2024-06-01",
        "--source",
        "synthetic",
        "--interval",
        "5m",
        "--seed",
        "7",
        "--cash",
        "10000",
        "--divergence",
    ]

    def test_an_unhandled_exception_mid_session_keeps_the_settled_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash the CLI has no handler for — the class ADR-0043 cannot cover.

        ``_persist_state`` runs inside the per-bar reporter, so raising there is a
        faithful stand-in for anything that kills the loop without unwinding to
        ``finalize()``. Before this slice the divergence rows were still in memory
        on the ``ShadowBroker`` at that moment and went with the process.
        """
        from trading import cli

        out = tmp_path / "crashed"
        real = cli._persist_state
        calls = {"n": 0}

        def failing(path: Path, outcome: BarOutcome, portfolio: Portfolio) -> None:
            calls["n"] += 1
            if calls["n"] > 400:
                raise RuntimeError("the machine went away")
            real(path, outcome, portfolio)

        monkeypatch.setattr(cli, "_persist_state", failing)
        result = runner.invoke(cli.app, [*self._ARGS, "--out", str(out)])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        rows = (out / "fill_divergence.csv").read_text().splitlines()
        assert len(rows) > 1, "the settled rows died with the process"
        assert rows[0].startswith("submitted_ts,")

    @pytest.mark.skipif(os.name != "posix", reason="SIGKILL is a POSIX notion")
    def test_a_real_sigkill_leaves_the_rows_it_had_measured(self, tmp_path: Path) -> None:
        """The card's own standard: a real process, killed uncatchably.

        ``kill -9`` is the case no signal handler can reach — which is the whole
        argument for writing as you go rather than writing on the way out. Readiness
        is the artifact itself: the child is killed as soon as the file it is
        supposed to be keeping up to date has rows in it.
        """
        out = tmp_path / "killed"
        child = subprocess.Popen(
            [sys.executable, "-m", "trading.cli", *self._ARGS, "--out", str(out)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        path = out / "fill_divergence.csv"
        try:
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                if path.exists() and len(path.read_text().splitlines()) > 5:
                    break
                if child.poll() is not None:
                    pytest.fail("the child finished before it could be killed mid-flight")
                time.sleep(0.05)
            else:
                pytest.fail("no divergence rows appeared within the timeout")
            survived = path.read_text().splitlines()
            child.send_signal(signal.SIGKILL)
            assert child.wait(timeout=30.0) == -signal.SIGKILL
        finally:
            if child.poll() is None:  # pragma: no cover - only on an assertion failure
                child.kill()
                child.wait(timeout=30.0)

        # Nothing finalized: no summary, no result.json. The measurement is there.
        assert not (out / "result.json").exists()
        after = path.read_text().splitlines()
        assert after[: len(survived)] == survived  # append-only, never rewritten
        assert len(after) > 5
        assert all(line.count(",") == survived[0].count(",") for line in after)
