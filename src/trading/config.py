"""Run configuration and cost defaults.

Defaults reflect a small, real account (ADR-0011, Q22): $1,000 of capital and
commission-free trades with a modest, deliberately pessimistic slippage so
backtests don't flatter themselves (Q14).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Alpaca's published **tier-1 taker** rate for crypto spot, in basis points
# (ADR-0060). Source: https://docs.alpaca.markets/us/docs/crypto-fees — the
# maker/taker schedule is tiered by trailing 30-day *crypto* volume, tier 1 is
# "$0-$100K" and charges maker 0.15% / taker **0.25%**. Read 2026-08-14; the page
# itself carries "Updated September 24, 2025".
#
# **Taker, not maker, because this bench cannot be a maker.** Every order it emits
# is a market order (ADR-0004, and `sizing.py` produces nothing else), which by
# definition crosses the spread and takes liquidity. No maker/taker switch is built:
# it would be a knob with exactly one reachable setting.
#
# **Tier 1, because that is where this account sits and where it will stay.** The
# paper account holds ~$100k, and tier 1 covers $0-$100K of trailing 30-day volume.
# Whether Alpaca's *paper* venue simulates tiering at all is unknown and untested —
# it is moot at this size, and tier 1 is the most expensive row, so assuming it is
# the conservative direction.
#
# **The published number and the measured one agree exactly**, which is why this is
# a sourced constant and not a fitted one. KAN-708 measured the paper venue taking
# the fee in the *received* asset at ratios `0.99749936` and `0.99750000` against a
# published `1 - 0.0025 = 0.9975` (ADR-0058 §5). Independent derivation, same
# number, so nothing here was tuned to make a report look right.
CRYPTO_TAKER_FEE_BPS = 25.0

# The more-liquid cost tier's slippage rate, in basis points (KAN-861, ADR-0063).
#
# **Measured, not fitted, and deliberately not the point estimate.** ADR-0052
# measured mega-cap (blue20) fills at a mean of 0.51 bps against the flat 5.0 bps
# model, on 60 paired equity fills — and refused to re-tune the flat model to that
# number, for reasons this constant respects rather than overrides: the measurement
# is the same order as the ~0.4 bps IEX-vs-consolidated reference error, these are
# *paper* fills (our cost model checked against Alpaca's fill model, not routed
# execution), and it is one afternoon, one venue, twenty names. KAN-861 then
# measured the *other* end of the liquidity scale — ten real, verifiably thin S&P
# 500 constituents (ADV $35.6M-$109.3M/day, formation window 2026-05-18..2026-08-16,
# all ten comfortably above `trading.liquidity.DEFAULT_MIN_ADV = $20M/day` but near
# the bottom of the index and three orders of magnitude below blue20's billions) —
# and got a mean of +4.23 bps / median +5.06 bps on 11 paired fills, i.e. close to
# the flat 5.0 bps default. Read together the two measurements
# say the *existing* flat model is approximately right for names this thin and
# probably too pessimistic for names two orders of magnitude more liquid — which is
# the whole premise of a liquidity-tiered cost model (a corollary of ADR-0060: cost
# is a function of liquidity, not of asset class).
#
# So this constant moves the *mega-cap* tier down from the 5.0 bps default, but not
# to 0.51: it keeps roughly the same margin of conservatism the flat 5.0 bps number
# always carried over its own best point estimate (5.0 / 0.51 =~ 9.8x; 2.0 / 0.51
# =~ 3.9x — still several times the measured mean, comfortably outside the ~0.4 bps
# reference-price noise floor, on a sample this bench's own significance floor
# (`MIN_PAIRED_FILLS = 30`) says is not yet a level). The default (below-floor) tier
# is left at 5.0 exactly, unmoved, because KAN-861's own measurement is the evidence
# *for* leaving it alone.
LIQUID_TIER_SLIPPAGE_BPS = 2.0


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Trading cost assumptions applied by the simulated broker.

    Three terms, and they are three *different physical quantities* rather than
    three spellings of one (ADR-0060):

    * ``slippage_bps`` — an adverse move on the fill price. A statement about the
      *price* you get.
    * ``commission_per_share`` — dollars per unit traded. Independent of price.
    * ``taker_fee_bps`` — a fraction of the traded **notional**. Independent of
      quantity.

    ``commission_per_share`` cannot express the third: a percentage-of-notional fee
    is not a per-share amount at any fixed conversion, because the conversion is the
    price. So a term was **added** rather than the existing one restructured — a
    venue may legitimately charge both, and folding them together would have meant
    re-deriving one of them from a price at every call site.

    **The defaults are a US-equity posture** and they do not move: 5 bps of slippage
    and no commission, which is a commission-free equity broker. :meth:`equity`
    names that posture and returns exactly ``CostConfig()``; :meth:`crypto` is the
    24/7 posture and differs in **one field**. ADR-0055's shape, applied to costs.

    ``symbol_slippage_bps`` is a fourth, **optional** term (KAN-861, ADR-0063): a
    per-symbol override of ``slippage_bps``, keyed by symbol. It is ``None`` by
    default, which is the whole point — a ``CostConfig`` built the old way, or via
    :meth:`equity`/:meth:`crypto`, carries no per-symbol overrides and prices every
    symbol at the flat rate exactly as before. The map is populated exactly once,
    before a run, by classifying each traded symbol's *pre-run* average dollar
    volume into a liquidity tier (``trading.liquidity.classify_liquidity_tier`` +
    ``liquidity_tier_rates``) — never per-bar and never from in-run data, the same
    look-ahead discipline :func:`trading.liquidity.screen_by_adv` already enforces.
    A symbol absent from the map (or the map itself being ``None``) falls back to
    ``slippage_bps``, so an un-tiered symbol is priced exactly as it always was.
    """

    commission_per_share: float = 0.0
    slippage_bps: float = 5.0  # 5 basis points = 0.05% adverse move on each fill.
    # A proportional fee on the traded notional, charged by the venue on top of
    # whatever the price already cost. Zero for commission-free US equities, which
    # is why every existing run is arithmetically untouched (ADR-0060).
    taker_fee_bps: float = 0.0
    # Per-symbol slippage override, classified once from pre-run ADV (KAN-861). Kept
    # out of `equity()`/`crypto()` entirely, so choosing a market never silently
    # enables tiering — that is a separate, explicit opt-in at the CLI.
    symbol_slippage_bps: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        if self.commission_per_share < 0:
            raise ValueError("commission_per_share must be non-negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if self.taker_fee_bps < 0:
            raise ValueError("taker_fee_bps must be non-negative")
        if self.symbol_slippage_bps is not None:
            for symbol, rate in self.symbol_slippage_bps.items():
                if rate < 0:
                    raise ValueError(f"symbol_slippage_bps[{symbol!r}] must be non-negative")

    @classmethod
    def equity(cls) -> CostConfig:
        """The US-equity cost posture — exactly the field defaults, named.

        Returns ``cls()``. It exists so a caller choosing a market chooses one
        *explicitly*, instead of the equity assumption being the unnamed thing that
        happens when nobody chooses (ADR-0055's argument, ADR-0060's application).
        Pinned equal to ``CostConfig()`` by a test.
        """
        return cls()

    @classmethod
    def crypto(cls, *, taker_fee_bps: float = CRYPTO_TAKER_FEE_BPS) -> CostConfig:
        """The 24/7 cost posture: the equity slippage, plus the venue's taker fee.

        Differs from :meth:`equity` in **exactly one field** — ``taker_fee_bps`` —
        and a test diffs the two configs so a second change here turns red.

        **``slippage_bps`` deliberately stays at 5.0**, and that restraint is the
        point. ADR-0052 refused to re-tune it on **60** paired equity fills that
        measured 0.51 bps against the model, on the grounds that the measurement was
        the same order as the reference error, that Alpaca paper fills are
        *simulated* rather than routed, and that one afternoon on one venue is not a
        level. The crypto evidence available here is **three** paired fills (8.03,
        35.29, 44.34 bps — ADR-0058), an eighth of ``MIN_PAIRED_FILLS``. Less
        evidence cannot justify more tuning. KAN-710 owns that measurement.

        So the one number that *does* move is the one that is **published and
        independently confirmed**: ``CRYPTO_TAKER_FEE_BPS``, Alpaca's tier-1 taker
        rate, sourced from the fee schedule and separately observed on the account
        at the same value. See that constant for the URL, the date, and the tier.

        ``taker_fee_bps`` may be overridden for a different volume tier, but it may
        **not** be zero: a 24/7 posture that models a venue charging 25 bps as free
        is precisely the flattering number this preset exists to prevent, and it is
        not reachable from the published schedule either — the cheapest row on it is
        tier 8's 0.10% taker, and only a *maker* ever pays 0.00%. It is a
        ``ValueError``, not a silently free venue. Use ``CostConfig()`` if what you
        want is the equity posture.
        """
        if taker_fee_bps <= 0:
            raise ValueError(
                "the 24/7 posture requires a positive taker fee: Alpaca's crypto "
                f"venue charges {CRYPTO_TAKER_FEE_BPS:g} bps at tier 1 and takes it "
                "in the received asset (ADR-0058 §5, ADR-0060). Modelling that as "
                "free is the flattering number this preset exists to prevent. Use "
                "CostConfig() for the commission-free equity posture instead."
            )
        return cls(taker_fee_bps=taker_fee_bps)


# The one number the 24/7 (crypto) posture changes (ADR-0055). Everything else in
# :meth:`RiskConfig.crypto` is the equity default, deliberately: the measurement
# behind this card found the *latch*, not the levels, is what breaks at crypto
# volatility, and widening a cap until nothing trips is a disabled guardrail with
# extra steps. The floor is arithmetic — a cooldown shorter than
# ``(max_drawdown_pct / per-bar sigma)²`` bars re-arms inside the same move that
# tripped the switch; at 80% annualized vol on daily bars that is ~16 bars, and 30
# is the next legible unit above it (a month of a market that never closes).
CRYPTO_HALT_COOLDOWN_BARS = 30


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Enforced risk limits (ADR-0009, ADR-0013).

    Guardrails are on by default with a small, real-account posture: no single
    symbol over a quarter of equity, no leverage (gross ≤ 100%), and a hard halt
    once drawdown from the equity peak reaches a fifth. ``max_daily_loss_pct`` is
    an optional single-bar circuit breaker, off by default. ``target_volatility``
    is an optional annualized volatility target (e.g. 0.10 for 10%) that scales the
    effective gross-exposure cap up or down toward that target (ADR-0015); off by
    default, so behavior is unchanged unless it is set. ``max_sector_exposure`` with
    a ``sector_map`` is an optional per-sector gross cap (ADR-0019) that limits how
    much of equity may sit in any one sector; off by default. Every limit is
    overridable per run; :meth:`unlimited` returns the permissive opt-out.

    ``halt_recovery_drawdown_pct`` and ``halt_cooldown_bars`` are the two optional
    **halt-recovery** knobs (ADR-0031). Both are ``None`` by default, which keeps the
    kill switch **latching for the whole run** exactly as ADR-0013 decided. Set
    either (or both) and a tripped halt can *re-arm*:

    * ``halt_recovery_drawdown_pct`` — re-arm once drawdown from the peak has
      recovered back to at most this fraction. It must be strictly **below**
      ``max_drawdown_pct``: that gap is the hysteresis band, and validating it here
      is the first of the two anti-flap guarantees (a config where the trip and
      re-arm levels coincide is rejected, not silently allowed to oscillate).
    * ``halt_cooldown_bars`` — re-arm only after the halt has been in force for this
      many bars (counting the bar it fired on). Must be a positive integer.

    With both set the **earlier** trigger re-arms the switch (OR). ADR-0031 explains
    why the more conservative AND was rejected: a halted long-or-flat strategy may
    exit but not enter, so it drains to cash and its equity — hence its drawdown —
    freezes, and a drawdown condition not already met at that moment can never be
    met. AND would therefore silently reinstate the permanent latch.

    **The defaults above are an equity posture (ADR-0055).** They are calibrated for
    mega-cap US equities at roughly 20% annualized volatility, and the field defaults
    do not move. :meth:`equity` names that posture explicitly and returns exactly
    ``RiskConfig()``; :meth:`crypto` is the 24/7 posture, which differs in **one
    field** — it makes halt recovery mandatory rather than optional. Measured, the
    levels are not what breaks at four times the volatility; the *latch* is.
    """

    max_position_pct: float = 0.25
    max_gross_exposure: float = 1.0
    max_drawdown_pct: float = 0.20
    max_daily_loss_pct: float | None = None
    target_volatility: float | None = None
    sector_map: Mapping[str, str] | None = None
    max_sector_exposure: float | None = None
    halt_recovery_drawdown_pct: float | None = None
    halt_cooldown_bars: int | None = None

    @property
    def halt_recovery_enabled(self) -> bool:
        """Whether any recovery mechanism is configured (ADR-0031).

        ``False`` — the default — means the halt latches for the run, the ADR-0013
        behavior. The monitor checks this once per bar and skips the whole re-arm
        path when it is off, so the latching path is untouched.
        """
        return self.halt_recovery_drawdown_pct is not None or self.halt_cooldown_bars is not None

    def __post_init__(self) -> None:
        if self.max_position_pct <= 0:
            raise ValueError("max_position_pct must be positive")
        if self.max_gross_exposure <= 0:
            raise ValueError("max_gross_exposure must be positive")
        if not 0 < self.max_drawdown_pct <= 1.0:
            raise ValueError("max_drawdown_pct must be in (0, 1]")
        if self.max_daily_loss_pct is not None and not 0 < self.max_daily_loss_pct <= 1.0:
            raise ValueError("max_daily_loss_pct must be None or in (0, 1]")
        if self.target_volatility is not None and self.target_volatility <= 0:
            raise ValueError("target_volatility must be None or positive")
        if self.max_sector_exposure is not None and not 0 < self.max_sector_exposure <= 1.0:
            raise ValueError("max_sector_exposure must be None or in (0, 1]")
        recovery = self.halt_recovery_drawdown_pct
        if recovery is not None:
            if not 0 <= recovery < 1.0:
                raise ValueError("halt_recovery_drawdown_pct must be None or in [0, 1)")
            # The hysteresis band must be non-empty: re-arming at (or above) the
            # trip level would let the switch halt and re-arm on adjacent bars
            # forever (ADR-0031, anti-flap guarantee 1).
            if recovery >= self.max_drawdown_pct:
                raise ValueError(
                    "halt_recovery_drawdown_pct must be strictly below max_drawdown_pct "
                    f"(got {recovery} >= {self.max_drawdown_pct}); the gap is the "
                    "hysteresis band that stops the kill switch from flapping"
                )
        if self.halt_cooldown_bars is not None and self.halt_cooldown_bars < 1:
            raise ValueError("halt_cooldown_bars must be None or a positive integer")

    @classmethod
    def unlimited(cls) -> RiskConfig:
        """A fully permissive config — the explicit opt-out from enforcement.

        Position and gross caps are infinite (never clamp), the drawdown halt is
        unreachable (fires only at total wipe-out), and the daily-loss breaker is
        off. Pass this to disable guardrails without forking the engine's path.
        """
        return cls(
            max_position_pct=float("inf"),
            max_gross_exposure=float("inf"),
            max_drawdown_pct=1.0,
            max_daily_loss_pct=None,
        )

    @classmethod
    def equity(cls) -> RiskConfig:
        """The equity posture — exactly the field defaults, named (ADR-0055).

        Returns ``cls()``. It exists so a caller choosing a market chooses one
        *explicitly*, instead of the equity assumption being the unnamed thing that
        happens when nobody chooses. Pinned equal to ``RiskConfig()`` by a test, so
        this can never quietly become a third posture.
        """
        return cls()

    @classmethod
    def crypto(cls, *, halt_cooldown_bars: int | None = CRYPTO_HALT_COOLDOWN_BARS) -> RiskConfig:
        """The 24/7 posture: the equity levels, but the kill switch may not latch.

        ADR-0055. This differs from :meth:`equity` in **exactly one field** —
        ``halt_cooldown_bars`` — and a test asserts that, so widening a cap here
        turns red. Nothing is loosened: ``max_position_pct`` (25%),
        ``max_gross_exposure`` (100%, i.e. no leverage) and ``max_drawdown_pct``
        (20%) are the equity numbers, unchanged.

        The reason is measured rather than assumed. Driven through the real
        :class:`~trading.engine.Engine` on a synthetic series at 80% annualized
        volatility (four times the equity default, drift held equal so volatility is
        the only change), the default latching config halted in **20 of 20 seeds**,
        typically about 250 bars into a 2,610-bar run, and then spent a median
        **90.5%** of the run refusing entries: median total return **+8.95%** against
        **+561.93%** with the drawdown halt neutralized. That is ADR-0031's measured
        failure — the same one that turned ``cross_sectional`` into -3.91% on
        2000-2020 equities — arriving in the first year instead of the second.

        The fix is therefore *bounding* the halt, not raising the bar it trips over.
        A 20% drawdown genuinely is ordinary here (84% of bars in that series sit at
        or below 20% off their running peak, against 30% of the equity-volatility
        bars), so the switch fires often — roughly 7-8 bounded episodes per ten
        years, holding a median 8.6% of the run — and that is a working circuit
        breaker rather than a broken kill switch. Raising the threshold to the same
        *tail rank* that 20% occupies for equities would mean **78%**, past even a
        2022-style crypto drawdown: a number that never fires, which is a disabled
        guardrail, not a calibrated one.

        ``halt_recovery_drawdown_pct`` stays ``None`` deliberately, also on evidence:
        alone, at this volatility, it re-armed **nothing** (1 halt, never resumed,
        +11.72% — the permanent latch in disguise), because a halted long-or-flat
        book drains to cash and freezes its drawdown above the threshold, exactly the
        deadlock ADR-0031 §2 measured. The cooldown is the liveness guarantee; a
        recovery threshold is an early re-arm a caller may add.

        ``halt_cooldown_bars`` may be overridden for a different volatility or bar
        interval, but it may **not** be ``None``: a 24/7 posture whose halt latches
        for the whole run is the thing this preset exists to prevent. The parameter
        admits ``None`` in its type only so the refusal is expressible — the field
        itself is ``int | None``, and an untyped caller (a future CLI flag, a config
        file) can hand one over. It is a ``ValueError``, not a silently latching
        config.
        """
        if halt_cooldown_bars is None:
            raise ValueError(
                "the 24/7 posture requires a halt cooldown: halt recovery is not "
                "optional there (ADR-0055). A latching kill switch was measured to "
                "spend ~90% of a crypto-volatility run refusing entries. Use "
                "RiskConfig() for the latching equity posture instead."
            )
        return cls(halt_cooldown_bars=halt_cooldown_bars)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything a backtest run needs beyond the strategy and the data."""

    starting_cash: float = 1_000.0
    costs: CostConfig = CostConfig()

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
