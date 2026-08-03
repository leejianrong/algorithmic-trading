# ADR-0013: Guardrail enforcement semantics

- Status: Accepted
- Date: 2026-08-03
- Deciders: strategy developer (project owner)

## Context

ADR-0009 decided *that* risk guardrails are enforced in-engine and on by default:
a pre-trade check (position and gross-exposure caps) and a portfolio monitor
(drawdown / daily-loss kill switch). Implementing it (slice V3) surfaced several
smaller choices ADR-0009 left open — how an over-cap order is trimmed, what the
halt does to orders already in flight, how long it lasts, and which component
still owns cash. This ADR records those implementation-level semantics so the
behavior is a decision, not an accident of the code.

## Decision

The `Guardrails` class (`risk.py`) implements the `RiskGuardrails` seam with these
semantics:

1. **Clamp, don't reject, for over-cap buys.** Target-weight sizing routinely
   overshoots a cap by a sliver (slippage headroom, rounding). A buy is trimmed
   down to the tighter of the per-symbol position cap and the gross-exposure cap
   rather than thrown away, so the strategy still gets the exposure it's allowed.
   A clamp that collapses to ~nothing (≤ `SHARE_EPS`) becomes a rejection — there
   is no room left to trade.

2. **Exits are always allowed; the halt blocks only new risk.** A sell that
   reduces an existing holding never breaches a long cap and is never blocked,
   including while halted — you can always get *out*. The kill switch blocks new
   entries and increases only. (Auto-flatten on halt is deliberately not done; it
   is offered as a future option in ADR-0009, not the default.)

3. **The halt latches for the session.** Once drawdown-from-peak (or, if
   configured, a single-bar loss) reaches its threshold, the monitor stays halted
   for the rest of the run even if equity later recovers. A rehearsal for real
   capital should stop and be looked at, not silently un-halt on a dead-cat
   bounce. The monitor is stateful — one `Guardrails` instance per run — tracking
   the running equity peak and the previous bar's equity.

4. **Cash sufficiency stays with the broker.** The caps keep buys within *equity*;
   the authoritative "can this fill be paid for?" check remains in
   `SimulatedBroker` (ADR-0004), the single accounting authority. Guardrails and
   broker are complementary: a cap can clamp a legal-but-oversized order, and the
   broker still rejects anything that can't be funded at the actual fill price.

5. **Enforced by default, opt out explicitly.** The engine constructs
   `Guardrails(RiskConfig())` when none is injected. `RiskConfig.unlimited()`
   (infinite caps, unreachable drawdown, no daily-loss breaker) is the one
   sanctioned way to disable enforcement — used by the pre-V3 accounting/plumbing
   tests and the CLI's `--no-guardrails` flag — so "off" is always deliberate and
   greppable, never a silent default.

6. **Caps account for exposure committed earlier in the same bar.** A multi-symbol
   rebalance emits several orders on one bar; they all queue and fill next bar, so
   the pre-trade portfolio doesn't change between `check()` calls. Checking each
   order against that same frozen book would let N orders, each individually under
   the gross cap, collectively breach it (and two raw buys of one symbol slip past
   the position cap the same way). So `Guardrails` keeps a **within-bar committed
   tally** — running committed gross notional and per-symbol committed quantity —
   and subtracts it from the room each cap allows. When an order is approved
   (clamped or not) its accepted notional/quantity is added to the tally so later
   same-bar orders see the reduced room. The equity denominator stays the
   pre-trade snapshot (consistent with how sizing snapshots equity); only the
   *available room* is reduced by the tally.

   The tally is reset once per bar at the top of `halted()`, which the engine calls
   exactly once per bar immediately before the check loop — the natural bar
   boundary. `RiskConfig.unlimited()` leaves the tally harmless: infinite caps mean
   the room is always infinite, so nothing ever binds.

The engine calls `halted()` once per bar (after marking the book) to update and
latch the switch and to open a fresh within-bar tally, then `check()` on each
sized order. Because the seam returns only `Order | None`, the reason for a
clamp/rejection is read back from the guardrails' `last_reason`, and the halt's
cause/first-trip timestamp are recorded on `BacktestResult` (`clamps`,
`rejections`, `halted`, `halt_ts`, `halt_reason`).

## Alternatives considered

| Option | Why not |
|--------|---------|
| Reject over-cap orders outright | Loses the exposure the strategy is actually allowed; a 1% overshoot shouldn't drop the whole trade. |
| Un-halt when drawdown recovers | A recovering curve mid-crash is exactly when discipline matters; a latching stop is the safer real-capital habit. |
| Block exits while halted too | Traps you in a losing position — the opposite of a kill switch; you must always be able to reduce risk. |
| Move cash checks into the guardrails | Duplicates accounting the broker already owns (ADR-0004) and risks the two drifting; caps and funding are separate concerns. |
| Check each order against only the frozen pre-trade book | Same-bar orders queue and don't fill until the next bar, so the book doesn't move between checks; N orders each under the cap would collectively breach it. The within-bar tally closes that hole without joint sizing. |

## Consequences

- Buys: predictable, greppable enforcement; strategies keep the exposure they're
  entitled to; a crash reliably stops new risk while leaving the exit open; the
  report surfaces every clamp, rejection, and halt.
- Costs: the guardrails now carry per-bar state (the committed tally) on top of
  the session state (peak, previous equity, latch), and correctness depends on the
  engine's once-per-bar `halted()` call as the reset boundary — a contract paper
  mode must honor when it wires in (V5).
- Forecloses: nothing; auto-flatten, per-batch exposure sizing, and richer
  monitors (volatility targeting, per-sector caps) remain additive later.
