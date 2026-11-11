# Anthropic IPO fade: Slices

## V1: The comp evidence (done)

**Delivers:** R4. **Demo:** `uv run python research/ipo-fade/ipo_comps.py` prints
the 21-name table and bucket stats.

### Test plan
#### End-to-end
- Script exits 0 and prints ALL r20: n=21 (network; not in CI).
#### Integration
- Skips a ticker with no/short data instead of crashing.
#### Unit
- None; research script.

## V2: Pre-register (before ANT's first print)

**Delivers:** R1, R2. **Build plan:** commit PLAN.md and ADR-0076/0077 and
create `journal.md` with a "decided no-trade is a result" line.
**Demo:** `git log` shows the commit timestamp before ANT's first trade.
**Rests on:** Q8-Q9 defaults.

### Test plan
#### End-to-end
- Commit time is earlier than ANT's first trade time.

## V3: Day-3 snapshot and gate check

**Delivers:** R3. **Build plan:** after options list, save the chain
(strikes, bid/ask, IV) to `docs/anthropic-ipo/ant_chain_<date>.csv`; evaluate
rules 2 and 4 in the journal. **Demo:** journal shows each gate, its number, and
pass/fail.

### Test plan
#### End-to-end
- Every gate in PLAN has a recorded number and a verdict.

## V4: Trade or skip, then close

**Delivers:** R0, R2. **Build plan:** place the spread (or record the skip); close
per rules 5-6; journal fills, debit, exit, and P&L. **Demo:** journal row with
debit <= $500.

### Test plan
#### End-to-end
- Debit paid is at most $500, or the entry is "skipped" with the failing gate.
