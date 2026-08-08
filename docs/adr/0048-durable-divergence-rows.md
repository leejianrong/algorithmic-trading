# ADR-0048: The divergence rows are written as they settle

- Status: Accepted
- Date: 2026-08-09
- Deciders: strategy developer (project owner)

## Context

`fill_divergence.csv` is the measurement a live paper session exists to collect
(ADR-0038). It is also the only artifact this bench produces that **cannot be
reconstructed** from what a dead session leaves behind:

- `paper_session.log` carries the realized fills — symbol, quantity, price — and
  nothing about the counterfactual. No reference price, no modelled fill, no
  slippage. That is the "what happened" half without the "what the model said"
  half, and the difference between them *is* the measurement.
- `paper_state.json` is a single snapshot of the latest bar, overwritten every bar.
  There is no curve in it and never was.
- `equity_curve.csv` and `result.json` are both written from
  `result.equity_curve`, which is derived state: re-run the range and you get them
  back.

Until this slice every divergence row lived in memory on the `ShadowBroker` until
`session.finalize()` returned, and the CSV was written once, at the very end.

ADR-0043 fixed the *signalled* half of that loss: SIGTERM now takes the same
finalizing path Ctrl-C has taken since ADR-0033, and it works — re-verified on
today's `main` before writing any of this, with a real signal to a real process:
`rc=0`, all five artifacts, finalization in well under a second. **The specific
SIGTERM symptom KAN-711 was filed against is already fixed.**

What a handler cannot cover is everything that does not deliver a catchable signal:
`kill -9`, an OOM kill, power loss, a laptop suspending mid-session (the Monday
runbook warns about exactly this — there is no supervision and no restart), and any
unhandled exception that unwinds past the one `except KeyboardInterrupt`. Writing
as you go covers all of them at once, which is why the card survives its own fix.

Reproduced on `main @ 6e24bef`, a real `--divergence` session SIGKILLed after 3,868
processed bars:

```
rc=137  (137 = SIGKILL)
bars processed (paper_session.log lines): 3868
survivors:
-rw-r--r-- 1 jianlee jianlee 309501 paper_session.log
-rw-r--r-- 1 jianlee jianlee      0 paper_state.json
```

`fill_divergence.csv`, `equity_curve.csv` and `result.json` are simply not there,
and the session had already measured 500-odd paired fills. Note the second line:
`paper_state.json` came back **zero bytes**. That is not a coincidence of this run
— `_persist_state` did `path.write_text(...)`, which truncates first, and it runs on
*every* bar of a live session, so over a day the odds of a crash landing inside that
window are not small. The one artifact that did survive the old code survived it
empty.

(The methodology matters here and cost an hour: `uv run` forks, so
`kill -9 $(pgrep uv)` kills the wrapper and leaves the session running to
completion. Every measurement above signals the interpreter directly.)

## Decision

### A row is journaled when it settles, and settling means neither side can change it

`DivergenceJournal` appends to `fill_divergence.csv` as rows close.
`ShadowBroker` takes it as an optional `journal` and hands it rows from
**`_flush_journal`, called at the end of `_observe`, over what `_harvest` has just
closed** — never from `submit`, never from attribution.

That placement is the whole of the late-settlement rule, and both awkward cases
fall out of it rather than needing a special case:

- **A partial fill (ADR-0033).** The venue emits a `Fill` *and* a rejection for one
  order, and `_attribute_live` records `filled` first, then amends it to `partial`
  in the same call. `_harvest` runs after attribution, so the intermediate `filled`
  state is never a row on disk. The file shows `partial` or nothing.
- **An order parked at the venue (ADR-0036).** The model filled it at the next open;
  the venue has said nothing and may still fill it on the next poll. It stays in
  `_tracked`, so it is **not** journaled. It reaches the file only if the session
  finalizes, where `pending` is the truthful final answer rather than a guess.

The alternative — journal everything and rewrite rows as they change — was rejected
outright. A row that is published and then contradicted is worse than one published
late: a reader tailing the file (the eventual live dashboard, KAN-712) or an
operator who pooled two sessions by hand would have no way to know which rows were
provisional. **A crashed session's file therefore under-reports; it never
misreports.** The rows it is missing are exactly the ones nobody could have
concluded anything from.

The consequence is stated plainly because it is a real loss: a session killed while
an order is parked loses the knowledge that it was parked. That row carries no
price, no slippage and no latency, so what is lost is a count, not a measurement.

### The file on disk is a byte prefix of the file the run would have finished with

Same header, same columns, same `divergence_rows` rendering, same settlement order
`ShadowBroker.divergences` reports in. So the survivor of a crash needs no separate
tooling and no reconciliation — it is the finished artifact, truncated. Measured,
not asserted: the 243-row survivor of the SIGKILL below is a byte prefix of the
196,649-byte file the same run produces when it is left alone.

At the end of the run `write_divergence_csv` still writes the canonical file, and
it now writes it to a sibling temp file and `os.replace`s it into place. Two
reasons, both about the journal it is replacing: a crash part-way through the final
write must not truncate rows that were already safely on disk (a plain `open("w")`
truncates before it writes a byte), and a reader watching the file as the session
finishes must never see half of one. Because the replace only happens after the
temp file is complete, **the final write cannot destroy the journal** — if it fails,
the journal is still there.

Keeping the full rewrite rather than appending the last few open rows to the
journal is deliberate: it means byte-identity with the pre-slice artifact is
guaranteed by construction, from the one renderer, rather than by two code paths
agreeing.

### `flush` plus `fsync`, and a handle that is never held between bars

Each `append` opens, writes, flushes, `fsync`s and closes. `flush` alone would be
enough to survive a killed *process* — the bytes are the kernel's by then — and
`fsync` is what carries them past a killed *machine*, which is the case that
motivates this ADR and that no signal handler can reach. Not holding a handle open
between bars means there is nothing to leak on an exception path and nothing to
reopen after a crash, which matters because the journal is constructed before the
session's `try` block.

The cost was measured rather than assumed: **+1.6 ms per bar that settles
something**. On the Monday session (~90 fills over 6.5 hours) that is 0.15 s in
total. On an offline `--once` replay it is visible — a 3-month 5-minute replay with
833 settled orders went from 1.70 s to 3.02 s. That is accepted rather than made
conditional on `--live`: a mode branch here would mean the durability guarantee
depends on which mode you are in, and the tests that prove the guarantee would then
only prove it for one of them.

### Journal I/O is shadow work, and obeys the shadow's rule

ADR-0038's structural guarantee is that the shadow cannot perturb the live path:
the live call runs first and unguarded, and all shadow work sits inside
`try/except Exception` that disables the shadow and records the failure. This slice
puts **file I/O** inside that boundary for the first time, so a full disk, a
read-only path, or a revoked permission now has to obey the same rule — and does,
because `_flush_journal` is called from `_observe`, which is already inside it.

Two details make that safe rather than merely true:

- `_flush_journal` runs **last**, after `_harvest` and after `_last_bar_ts` is set.
  If the writer raises, the wrapper's own bookkeeping is already consistent, so the
  now-disabled shadow still reports every row it measured through `divergences` and
  the end-of-run write still emits them.
- The journal cursor advances **only on a successful append**, so a failed write
  never silently drops the rows it was handed.

A journal failure disables the whole shadow rather than only the journal. That is
the more conservative of the two options and it costs something real — a disk that
fills at 10:00 ends the measurement for the day, where a journal-only degradation
would have kept measuring in memory and might still have written the file at 16:00.
It was chosen anyway, because "any exception in shadow code disables the shadow" is
one rule with one already-tested mechanism, and the failure is loud: `errors` is
printed in the report as *"the shadow was disabled mid-run and measured nothing
after"*. A second, quieter degradation mode is how a half-measured run starts
looking like a whole one.

### `paper_state.json` is replaced, not truncated and rewritten

Same idiom, in `cli._persist_state`: temp file in the same directory, then
`os.replace`, which is atomic within a filesystem on POSIX. A reader now sees either
the previous bar's state or this one's, never neither. The zero-byte file in the
reproduction above is what the old code left.

### The equity curve is left alone, deliberately

The same argument does apply to `equity_curve.csv`, and it is much weaker there:

1. **It is reconstructible.** `paper_session.log` already carries one line per bar
   with the timestamp, equity and exposure, written and flushed as the session runs
   — it is the artifact that survived every crash in this ADR. The curve can be
   rebuilt from it (to the two decimals the line prints), and the run's inputs are
   in the log's first record. Divergence rows can be rebuilt from nothing.
2. **The writer is shared with the backtest.** `report.write_equity_csv` is written
   from `result.equity_curve` at finalize and serves both modes; an incremental
   second writer for the same file is either a change to shared code or a duplicate
   renderer that will drift from it. ADR-0038 kept the divergence feature off
   `report.py` for a related reason.
3. **The per-bar row is not final in the same way.** A benchmark column is aligned
   in at the end, so a row appended per bar is a row that changes shape later —
   exactly what the journal rule above refuses to do.

So it is not built here, and this is the record of the judgement rather than an
omission. If it is wanted, the honest version is a separate per-bar `session.csv`
rather than an early `equity_curve.csv`.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Rely on the ADR-0043 SIGTERM handler | It works, and it covers one class. `kill -9`, an OOM kill, power loss, a suspended laptop and an unhandled exception deliver no catchable signal. The card is right that incremental flush is *strictly* more general. |
| Journal every tracked order each bar, rewriting rows as they settle | Publishes rows that are later contradicted. A reader cannot tell a provisional row from a final one, and pooling sessions by hand (which the runbook describes) would silently average intermediate states. |
| Keep an open file handle for the session | A handle held across the session's `try` block is a leak on every exception path, and it buys nothing: the win comes from `fsync`, not from avoiding an `open`. |
| Append the still-open rows to the journal at finalize instead of rewriting | Byte-identity with the pre-slice artifact would then depend on two code paths agreeing rather than on one renderer. The rewrite is a few milliseconds. |
| `flush` without `fsync` | Survives a killed process, not a lost machine — and a suspended laptop is the failure the runbook actually warns about. The measured cost is 1.6 ms per settling bar. |
| Make `fsync` conditional on `--live` | The durability guarantee would then hold in the mode the tests do *not* exercise end to end. One rule, both modes. |
| Let a journal failure disable only the journal, not the shadow | Better for the measurement, worse for honesty: two degradation modes, one of them quiet. Rejected in favour of ADR-0038's single rule and its printed warning. |

## Consequences

- **The measurement survives an uncatchable kill.** Same command, same 1.2 s,
  SIGKILL to the interpreter:

  ```
  before (main @ 6e24bef)          after
  rc=137                           rc=137
  paper_session.log  309501 B      fill_divergence.csv   57717 B  (243 rows)
  paper_state.json        0 B      paper_session.log    122000 B
  (no fill_divergence.csv)         paper_state.json        226 B  (valid JSON)
  ```

  The 243 rows are a byte prefix of the 833-row file the same run finishes with,
  and that finished file is byte-identical to the one `main` produces.
- **The finished artifacts do not move.** A `--once --divergence` replay run on this
  branch and on `origin/main` produces `diff -r`-identical output directories: the
  session log, `paper_state.json`, `equity_curve.csv`, `result.json` and
  `fill_divergence.csv` all match byte for byte, and stdout differs only where it
  prints the `--out` path. `--divergence` stays **off by default**, and the existing
  CLI test asserting `equity_curve.csv` and `result.json` are identical with and
  without the flag still passes.
- **The backtest path is untouched.** `Engine.run`, `broker.py`, `sizing.py`,
  `risk.py`, `metrics.py` and `report.py` are not edited by this slice.
  `SimulatedBroker` is not wrapped in a backtest unless a test asks for it.
- **`RESULT_SCHEMA_VERSION` stays 1.** Nothing about the artifacts changed.
- **The guarantee is proved by watching it fail**, three ways, each restored after.
  Deleting the `_flush_journal()` call from `_observe` turns **10** tests red,
  including the byte-prefix assertion, the parked-order rule and the real-SIGKILL
  subprocess test — while every pre-existing ADR-0038 test stays green, which is
  the other half of the claim. Reverting `_persist_state` to `write_text` turns
  both atomicity tests red, one of them by `PermissionError` on a read-only
  destination: renaming needs the *directory*, `open("w")` needs the *file*, so
  that assertion passes only if the bytes went somewhere else first. Reverting
  `write_divergence_csv` to a truncating `open("w")` turns the mid-write test red
  with the journaled row gone and only a header left — the exact loss the atomic
  replace exists to prevent.
- **A journal that raises is proved not to perturb the live path**, the same way
  ADR-0038 proved it for an exploding shadow: the same strategy run through a plain
  broker and through a `ShadowBroker` whose journal raises on every append produces
  **equal** `BacktestResult`s, and a real order still reaches the venue with the
  journal failing.
- **`fill_divergence.csv` is now readable while the session runs**, which is what
  KAN-712's live dashboard needs. It is not "eventually consistent": the rows in it
  are final.
- **Two small behaviour changes, both stated rather than discovered later.** The
  journal truncates `fill_divergence.csv` when the session *starts* rather than when
  it finishes, so re-running into an `--out` that already holds a run now destroys
  the old file immediately instead of at finalize. The runbook already says to use a
  distinct `--out` per session (they overwrite either way); this only moves the
  moment. And `paper_state.json` is replaced atomically but **not** `fsync`ed: it is
  rewritten every bar, so syncing it would be a per-bar cost for a convenience
  snapshot, where the journal syncs only on bars that settled something.
- **Still open.** `equity_curve.csv` is not incremental (see above). Nothing
  supervises or restarts a session — durability makes a crash cheap, it does not
  make a restart happen, and a restarted session would start a *new* journal rather
  than resuming the old one (each session should keep its own `--out`, which the
  runbook already says).
