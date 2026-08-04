"""Unit tests for the per-sector exposure cap (ADR-0019).

Fast, no infrastructure: the sector clamp is exercised directly against
hand-built portfolios, exactly like the position/gross caps in ``test_risk.py``.
The cap scopes the gross clamp to the order symbol's sector — held same-sector
value plus same-bar committed same-sector exposure may not exceed
``max_sector_exposure * equity``. It is off unless both a ``sector_map`` and a
``max_sector_exposure`` are configured, and a symbol absent from the map is
unconstrained by it.
"""

from __future__ import annotations

import pytest

from trading.config import RiskConfig
from trading.risk import Guardrails
from trading.types import Order, Portfolio, Position, Side


def _portfolio(cash: float, positions: list[Position] | None = None) -> Portfolio:
    return Portfolio(cash=cash, positions={p.symbol: p for p in (positions or [])})


def _guard(**overrides: object) -> Guardrails:
    # Position and gross caps wide open so only the sector cap can bind, unless a
    # test overrides them. Drawdown wide so halted() never latches in these tests.
    base: dict[str, object] = {
        "max_position_pct": 1.0,
        "max_gross_exposure": 1.0,
        "max_drawdown_pct": 1.0,
    }
    base.update(overrides)
    return Guardrails(RiskConfig(**base))  # type: ignore[arg-type]


class TestConfigValidation:
    def test_defaults_leave_the_sector_cap_off(self) -> None:
        cfg = RiskConfig()
        assert cfg.sector_map is None
        assert cfg.max_sector_exposure is None

    def test_unlimited_leaves_the_sector_cap_off(self) -> None:
        cfg = RiskConfig.unlimited()
        assert cfg.sector_map is None
        assert cfg.max_sector_exposure is None

    @pytest.mark.parametrize("value", [0.0, -0.1, 1.5])
    def test_out_of_range_sector_exposure_is_rejected(self, value: float) -> None:
        with pytest.raises(ValueError):
            RiskConfig(max_sector_exposure=value)

    def test_sector_map_without_a_cap_is_allowed(self) -> None:
        # A map alone (no cap) is inert but legal — the feature is simply off.
        cfg = RiskConfig(sector_map={"AAA": "tech"})
        assert cfg.max_sector_exposure is None


class TestSectorCap:
    def test_buy_breaching_the_sector_cap_is_clamped_to_the_room_left(self) -> None:
        # $1,000 flat; 30% sector cap allows 0.30 * 1000 / 100 = 3 shares of tech.
        guard = _guard(sector_map={"AAA": "tech"}, max_sector_exposure=0.30)
        checked = guard.check(Order("AAA", Side.BUY, 5.0), _portfolio(1_000.0), {"AAA": 100.0})
        assert checked is not None
        assert checked.qty == pytest.approx(3.0)
        assert "sector cap" in (guard.last_reason or "")
        assert "tech" in (guard.last_reason or "")

    def test_a_buy_within_the_sector_cap_passes_unchanged(self) -> None:
        guard = _guard(sector_map={"AAA": "tech"}, max_sector_exposure=0.30)
        order = Order("AAA", Side.BUY, 2.0)  # 20% < 30% cap
        checked = guard.check(order, _portfolio(1_000.0), {"AAA": 100.0})
        assert checked is order
        assert guard.last_reason is None

    def test_held_same_sector_value_counts_against_the_cap(self) -> None:
        # Already holding 2 tech shares ($200); 30% cap = $300 leaves room for $100
        # = 1 more share of a sibling tech name, not the 5 requested.
        guard = _guard(sector_map={"AAA": "tech", "BBB": "tech"}, max_sector_exposure=0.30)
        pf = _portfolio(800.0, [Position("AAA", qty=2.0, avg_price=100.0)])
        prices = {"AAA": 100.0, "BBB": 100.0}
        checked = guard.check(Order("BBB", Side.BUY, 5.0), pf, prices)
        assert checked is not None
        assert checked.qty == pytest.approx(1.0)
        assert "sector cap" in (guard.last_reason or "")

    def test_two_symbols_in_the_same_sector_share_the_cap_within_a_bar(self) -> None:
        # 30% tech cap = 3 shares of room. First buy takes 2; the sibling wants 5 but
        # only the committed 1 remains, so it is clamped to 1 (same-bar tally).
        guard = _guard(sector_map={"AAA": "tech", "BBB": "tech"}, max_sector_exposure=0.30)
        pf = _portfolio(1_000.0)
        prices = {"AAA": 100.0, "BBB": 100.0}
        guard.halted(pf, prices)  # begin the bar → reset the within-bar tally
        first = guard.check(Order("AAA", Side.BUY, 2.0), pf, prices)
        assert first is not None and first.qty == pytest.approx(2.0)
        second = guard.check(Order("BBB", Side.BUY, 5.0), pf, prices)
        assert second is not None
        assert second.qty == pytest.approx(1.0)
        assert "sector cap" in (guard.last_reason or "")

    def test_different_sectors_do_not_cross_limit(self) -> None:
        # Tech is filled to its 30% cap; an energy buy of the same size is untouched
        # because the caps are per sector, not shared.
        guard = _guard(sector_map={"AAA": "tech", "BBB": "energy"}, max_sector_exposure=0.30)
        pf = _portfolio(1_000.0)
        prices = {"AAA": 100.0, "BBB": 100.0}
        guard.halted(pf, prices)
        first = guard.check(Order("AAA", Side.BUY, 3.0), pf, prices)
        assert first is not None and first.qty == pytest.approx(3.0)
        second = guard.check(Order("BBB", Side.BUY, 3.0), pf, prices)
        assert second is not None
        assert second.qty == pytest.approx(3.0)  # energy has its own full cap
        assert guard.last_reason is None

    def test_symbol_absent_from_the_map_is_unconstrained_by_the_sector_cap(self) -> None:
        # CCC is not in the map; the sector cap does not apply, so a 5-share buy
        # (bounded only by the wide-open position/gross caps) passes unchanged.
        guard = _guard(sector_map={"AAA": "tech"}, max_sector_exposure=0.30)
        order = Order("CCC", Side.BUY, 5.0)
        checked = guard.check(order, _portfolio(1_000.0), {"CCC": 100.0})
        assert checked is order
        assert guard.last_reason is None


class TestOffByDefault:
    def test_no_sector_config_is_a_no_op(self) -> None:
        # No sector config at all: a buy is bounded only by position/gross, and the
        # binding reason never mentions a sector cap (byte-identical to pre-ADR-0019).
        guard = Guardrails(RiskConfig(max_position_pct=0.25, max_gross_exposure=1.0))
        checked = guard.check(Order("AAA", Side.BUY, 6.0), _portfolio(1_000.0), {"AAA": 100.0})
        assert checked is not None
        assert checked.qty == pytest.approx(2.5)  # clamped by the 25% position cap
        assert "position cap" in (guard.last_reason or "")
        assert "sector" not in (guard.last_reason or "")

    def test_sector_map_without_a_cap_does_not_clamp(self) -> None:
        # A map is present but no cap → the feature stays off; a big tech buy is
        # bounded only by the position cap, never by a sector cap.
        guard = Guardrails(
            RiskConfig(
                max_position_pct=0.25,
                max_gross_exposure=1.0,
                sector_map={"AAA": "tech"},
            )
        )
        checked = guard.check(Order("AAA", Side.BUY, 6.0), _portfolio(1_000.0), {"AAA": 100.0})
        assert checked is not None
        assert checked.qty == pytest.approx(2.5)
        assert "sector" not in (guard.last_reason or "")
