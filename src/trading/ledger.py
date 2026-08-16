"""Cross-invocation trial ledger (ADR-0062, KAN-858).

The gap this closes: :func:`~trading.metrics.deflated_sharpe` and
:meth:`~trading.sweep.SweepSummary.deflated_winner` (ADR-0039, KAN-619) deflate a
winning Sharpe against ``trials`` — the number of candidates that competed for it —
but ``trials`` only ever counts what *one process* can see. A lone ``trading
backtest`` is 1 trial; a 24-combo ``trading sweep`` is 24. An operator who hand-tried
six strategies across twenty sessions has made far more trials than any single
invocation can observe, so the printed "LOWER BOUND" caveat
(:func:`~trading.metrics.trial_count_note`) is not decoration — it is the tool
admitting it cannot see its own history. This module is that history.

``TrialLedger`` is an **append-only** JSONL file, one line per invocation
("experiment"). No SQLite, no rewriting: append-only is what makes the durability
story trivial rather than merely convenient. A line is written whole
(:meth:`TrialLedger.append` opens, writes one line, flushes, ``fsync``s, and closes —
the same three-call shape :class:`~trading.divergence.DivergenceJournal` uses for
ADR-0048, and for the same reason: a process killed mid-append leaves the file a
*prefix* of what it would have been, never a corrupted middle. A crashed ledger
under-reports; it never misreports. Unlike the divergence journal, no atomic
temp-file-plus-``os.replace`` dance is needed for the final artifact, because there
is no final artifact to protect — every line already on disk when a reader calls
:meth:`TrialLedger.load` is as durable as the append that wrote it.

What the ledger does **not** store is each historical trial's own Sharpe ratio —
only a per-invocation *count*. That is a real, stated limitation, not an oversight:
:func:`~trading.metrics.expected_max_sharpe`'s correction needs both the trial
*count* (``N``) and the trial *spread* (``sharpe_stdev``) to place the null, and the
spread can only ever be estimated from the trials whose individual Sharpes are still
in memory — this invocation's. A ledger of per-trial Sharpes would grow without
bound and would still be silently truncated the day someone deletes an old
experiment; a ledger of counts is small, forever append-only, and honest about what
it cannot supply. So :func:`~trading.metrics.deflated_sharpe`'s ``prior_trials``
widens ``N`` without widening the spread estimate — a real gap between what the
count says and what the correction can prove, documented there and here rather than
hidden.

The schema also carries a ``hypothesis`` field, unused by anything in this card. It
exists because KAN-862 (a pre-registration playbook, not built here) needs a place
to record "the hypothesis and kill criteria were written before the result was
seen" — and a field bolted on after the fact could never carry a promise made before
this card shipped. Recording it now, always present and defaulting to ``""``
("none given"), is what makes that later enforcement possible instead of aspirational.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """One invocation's worth of trials, as the ledger records it.

    ``timestamp`` is passed in by the caller (the CLI uses
    ``datetime.now(timezone.utc).isoformat()``) rather than computed inside this
    module — the same discipline :mod:`trading.clock` enforces on the engine: a
    module that reads the wall clock itself cannot be tested deterministically, and
    a ledger is exactly the kind of thing whose tests want to hand-build records
    with fixed timestamps.

    ``symbols`` should be passed **sorted** by the caller, so the same trading
    universe always serializes to the same JSON line regardless of the order
    ``--symbols`` happened to list it in — this module does not enforce the sort
    itself (a frozen dataclass has no cheap place to normalize a field without
    surprising a caller who deliberately wants insertion order preserved for some
    other use), so an out-of-order caller gets an out-of-order ledger line rather
    than a silent correction.

    ``trial_count`` is 1 for a plain backtest and the grid size for a sweep or
    walk-forward — the same granularity :attr:`~trading.sweep.SweepSummary.trial_count`
    already uses (one completed ``(combination, window)`` run is one trial).

    ``observed_sharpe`` is ``None`` rather than ``0.0`` when the run produced no
    measurable Sharpe (e.g. too few bars) — the same "absence is not a zero" rule
    ADR-0029/ADR-0037 apply everywhere else in this codebase.

    ``hypothesis`` defaults to ``""`` — never omitted — so "no hypothesis was
    given" and "the hypothesis was an empty string" are the same, honest, fact
    rather than a field that sometimes exists and sometimes does not.
    """

    timestamp: str
    command: str
    strategy: str
    symbols: tuple[str, ...]
    date_from: str
    date_to: str
    interval: str
    market: str
    trial_count: int
    observed_sharpe: float | None
    hypothesis: str = ""


class TrialLedger:
    """An append-only JSONL store of :class:`TrialRecord` lines at ``path``.

    Constructing one does nothing to the filesystem — no file is created until the
    first :meth:`append` — so building a :class:`TrialLedger` purely to call
    :meth:`load`/:meth:`cumulative_trials` against a ledger that may not exist yet
    is free and side-effect-free, exactly what the CLI needs to do before every run
    that might contribute to it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: TrialRecord) -> None:
        """Append ``record`` as one JSON line and make it durable before returning.

        ``json.dumps`` serializes the ``symbols`` tuple as a JSON array (the wire
        format has no tuple type); :meth:`load` converts it back. One line, no
        pretty-printing — a human reads this file with ``jq``, not by eye.

        The three-call durability shape — write, ``flush``, ``os.fsync`` — is
        :class:`~trading.divergence.DivergenceJournal`'s (ADR-0048): ``flush``
        alone survives a killed *process* (the bytes are already the kernel's),
        and ``fsync`` is what survives a killed *machine*. No handle is held
        between calls, so there is nothing to leak on an exception path.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(record))
        with self._path.open("a") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load(self) -> list[TrialRecord]:
        """Every record in the ledger, in append order; ``[]`` if it does not exist.

        A malformed **final** line is tolerated and dropped silently — that is
        exactly what a process killed mid-``write`` leaves behind (a torn JSON
        object with no trailing newline captured, or a newline with nothing after
        it), and "a crashed file under-reports, it never misreports" is the same
        rule :class:`~trading.divergence.DivergenceJournal` was built to satisfy.
        A malformed line **anywhere else** is real corruption — a hand-edited file,
        a disk error, a bug — and raises rather than silently discarding a
        cumulative trial count an operator is about to trust.
        """
        if not self._path.exists():
            return []
        lines = self._path.read_text().splitlines()
        records: list[TrialRecord] = []
        last_index = len(lines) - 1
        for index, line in enumerate(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                if index == last_index:
                    break  # a crash mid-write: an honest, silent under-report
                raise
            payload["symbols"] = tuple(payload["symbols"])
            records.append(TrialRecord(**payload))
        return records

    def cumulative_trials(self) -> int:
        """Sum of ``trial_count`` across every record — the ledger's whole point.

        ``0`` when the ledger does not exist yet or holds no records: a fresh
        ledger widens nothing, so the very first logged run behaves exactly as an
        unledgered one would.
        """
        return sum(record.trial_count for record in self.load())
