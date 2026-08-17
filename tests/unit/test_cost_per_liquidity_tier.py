"""Cost model per liquidity tier (KAN-861, ADR-0063).

ADR-0060 made costs a per-*asset-class* property. This is the sharper corollary,
also stated in CLAUDE.md: cost is a function of **liquidity**, not of asset class.
ADR-0052 measured mega-cap (blue20) equity fills at 0.51 bps against the flat 5.0
bps model; KAN-861 measured a genuinely thin S&P 500 tier (ADV $35.6M-$109.3M/day)
at a mean of +4.23 bps / median +5.06 bps on 11 paired fills — close to the flat
5.0 bps default. Both samples are thin (n=60 and n=11, both below
``MIN_PAIRED_FILLS = 30``), so neither justifies re-tuning to its own point
estimate; `LIQUID_TIER_SLIPPAGE_BPS = 2.0` keeps a comparable margin of caution
over 0.51 bps that the *unmoved* 5.0 bps default always kept over itself.

These tests pin the four things that change:

**Additive and opt-in, exactly like ADR-0060's shape.** A ``CostConfig`` built the
old way (``CostConfig()``, ``.equity()``, ``.crypto()``) carries no per-symbol
overrides at all and is priced exactly as before — pinned by a hash-equivalent
"one field differs" style test, mirroring ``test_cost_per_market.py``.

**The classification reuses the ADV screen's own no-look-ahead machinery** —
:func:`~trading.liquidity.classify_liquidity_tier` shares
:func:`~trading.liquidity.formation_window` and
:func:`~trading.liquidity.average_dollar_volume` with :func:`screen_by_adv`, so the
same look-ahead guard applies without a second implementation to keep in sync.

**The broker actually charges the tiered rate**, asserted end to end through
``SimulatedBroker`` and, at the CLI, through a real backtest with and without the
flag on a mixed-liquidity universe.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading.broker import CostModel, SimulatedBroker
from trading.cli import app
from trading.config import LIQUID_TIER_SLIPPAGE_BPS, CostConfig
from trading.data.fake import FakeAdapter
from trading.liquidity import (
    DEFAULT_TIER_ADV_FLOOR,
    classify_liquidity_tier,
    liquidity_tier_rates,
)
from trading.types import Bar, Order, Portfolio, Side

BACKTEST_START = datetime(2024, 6, 1, tzinfo=UTC)


def _bar(symbol: str, ts: datetime, close: float, volume: int) -> Bar:
    return Bar(symbol=symbol, ts=ts, open=close, high=close, low=close, close=close, volume=volume)


def _series(symbol: str, close: float, volume: int, *, days: int, ending: datetime) -> list[Bar]:
    return [
        _bar(symbol, ending - timedelta(days=offset), close, volume)
        for offset in reversed(range(days))
    ]


class TestCostConfigIsAdditive:
    def test_default_config_has_no_tiers(self) -> None:
        assert CostConfig().symbol_slippage_bps is None

    def test_equity_posture_has_no_tiers(self) -> None:
        assert CostConfig.equity().symbol_slippage_bps is None

    def test_crypto_posture_has_no_tiers(self) -> None:
        assert CostConfig.crypto().symbol_slippage_bps is None

    def test_only_the_new_field_differs_from_the_old_default(self) -> None:
        """A tiered config still IS the old one, plus exactly one field."""
        flat = dataclasses.asdict(CostConfig())
        tiered = dataclasses.asdict(CostConfig(symbol_slippage_bps={"MEGA": 2.0}))
        differing = {k for k in flat if flat[k] != tiered[k]}
        assert differing == {"symbol_slippage_bps"}

    def test_a_negative_tier_rate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"symbol_slippage_bps.*non-negative"):
            CostConfig(symbol_slippage_bps={"AAA": -1.0})

    def test_a_zero_tier_rate_is_allowed(self) -> None:
        # Zero slippage is a legitimate (if aggressive) modelling choice for a
        # single symbol; only negative rates are nonsensical.
        assert CostConfig(symbol_slippage_bps={"AAA": 0.0}).symbol_slippage_bps == {"AAA": 0.0}


class TestCostModelFallsBackByDefault:
    """The load-bearing byte-identical guarantee: no tiers, no behavior change."""

    def test_no_symbol_argument_uses_the_flat_rate(self) -> None:
        model = CostModel(CostConfig(slippage_bps=5.0, symbol_slippage_bps={"MEGA": 2.0}))
        assert model.fill_price(Side.BUY, 100.0) == pytest.approx(100.05)

    def test_a_symbol_absent_from_the_map_uses_the_flat_rate(self) -> None:
        model = CostModel(CostConfig(slippage_bps=5.0, symbol_slippage_bps={"MEGA": 2.0}))
        assert model.fill_price(Side.BUY, 100.0, "THIN") == pytest.approx(100.05)

    def test_no_map_at_all_uses_the_flat_rate_even_with_a_symbol(self) -> None:
        model = CostModel(CostConfig(slippage_bps=5.0))
        assert model.fill_price(Side.BUY, 100.0, "AAPL") == pytest.approx(100.05)


class TestCostModelAppliesTheTier:
    def test_a_tiered_symbol_gets_its_own_rate(self) -> None:
        model = CostModel(CostConfig(slippage_bps=5.0, symbol_slippage_bps={"MEGA": 2.0}))
        assert model.fill_price(Side.BUY, 100.0, "MEGA") == pytest.approx(100.02)
        assert model.fill_price(Side.SELL, 100.0, "MEGA") == pytest.approx(99.98)

    def test_buys_and_sells_move_opposite_directions_under_a_tier(self) -> None:
        model = CostModel(CostConfig(symbol_slippage_bps={"MEGA": 2.0}))
        buy = model.fill_price(Side.BUY, 100.0, "MEGA")
        sell = model.fill_price(Side.SELL, 100.0, "MEGA")
        assert buy > 100.0 > sell

    def test_two_symbols_can_carry_two_different_rates(self) -> None:
        model = CostModel(
            CostConfig(slippage_bps=5.0, symbol_slippage_bps={"MEGA": 2.0, "MICRO": 8.0})
        )
        assert model.fill_price(Side.BUY, 100.0, "MEGA") == pytest.approx(100.02)
        assert model.fill_price(Side.BUY, 100.0, "MICRO") == pytest.approx(100.08)
        assert model.fill_price(Side.BUY, 100.0, "UNTIERED") == pytest.approx(100.05)


class TestTheSimulatedBrokerChargesPerSymbol:
    """The path that actually moves a published backtest number."""

    def test_two_orders_on_two_symbols_get_two_different_fills(self) -> None:
        costs = CostConfig(slippage_bps=5.0, symbol_slippage_bps={"MEGA": 2.0})
        broker = SimulatedBroker(Portfolio(cash=100_000.0), costs)
        broker.submit(Order("MEGA", Side.BUY, 1.0))
        broker.submit(Order("MICRO", Side.BUY, 1.0))

        fills = broker.on_bar(
            {
                "MEGA": _bar("MEGA", BACKTEST_START, 100.0, 1_000),
                "MICRO": _bar("MICRO", BACKTEST_START, 100.0, 1_000),
            }
        )
        by_symbol = {f.symbol: f.price for f in fills}

        assert by_symbol["MEGA"] == pytest.approx(100.02)
        assert by_symbol["MICRO"] == pytest.approx(100.05)  # untiered -> flat default

    def test_an_untiered_broker_is_unchanged(self) -> None:
        """The regression guard: CostConfig() behavior must not move at all."""
        broker = SimulatedBroker(Portfolio(cash=100_000.0), CostConfig())
        broker.submit(Order("AAPL", Side.BUY, 1.0))
        (fill,) = broker.on_bar({"AAPL": _bar("AAPL", BACKTEST_START, 100.0, 1_000)})
        assert fill.price == pytest.approx(100.05)


class TestClassifyLiquidityTier:
    """Reuses the ADV screen's own point-in-time, no-look-ahead measurement."""

    def test_measures_pre_run_adv_per_symbol(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(
            _series("MEGA", 1_000.0, 2_000_000, days=10, ending=window_end)  # $2B/day
            + _series("THIN", 10.0, 3_000_000, days=10, ending=window_end)  # $30M/day
        )
        advs = classify_liquidity_tier(adapter, ["MEGA", "THIN"], BACKTEST_START)

        assert advs["MEGA"] == pytest.approx(2_000_000_000.0)
        assert advs["THIN"] == pytest.approx(30_000_000.0)

    def test_never_reads_a_bar_inside_the_backtest_range(self) -> None:
        """The load-bearing look-ahead guard, mirrored from screen_by_adv's own."""
        window_end = BACKTEST_START - timedelta(days=1)
        adapter = FakeAdapter(
            _series("FUTURE", 10.0, 10_000, days=10, ending=window_end)
            + _series(
                "FUTURE", 5_000.0, 50_000_000, days=10, ending=BACKTEST_START + timedelta(days=10)
            )
        )
        advs = classify_liquidity_tier(adapter, ["FUTURE"], BACKTEST_START)
        assert advs["FUTURE"] == pytest.approx(100_000.0)  # pre-start figure only

    def test_requested_range_ends_before_the_backtest_start(self) -> None:
        asked: list[tuple[datetime, datetime]] = []

        class RecordingAdapter:
            def get_bars(
                self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
            ) -> list[Bar]:
                asked.append((start, end))
                return _series(symbol, 100.0, 500_000, days=5, ending=end)

        classify_liquidity_tier(RecordingAdapter(), ["AAA", "BBB"], BACKTEST_START)

        assert len(asked) == 2
        for _start, end in asked:
            assert end < BACKTEST_START

    def test_a_symbol_with_no_bars_is_none_not_zero(self) -> None:
        advs = classify_liquidity_tier(FakeAdapter([]), ["GHOST"], BACKTEST_START)
        assert advs["GHOST"] is None

    def test_one_failing_symbol_does_not_abort_classification(self) -> None:
        window_end = BACKTEST_START - timedelta(days=1)
        good = _series("GOOD", 100.0, 500_000, days=5, ending=window_end)

        class ExplodingAdapter:
            def get_bars(
                self, symbol: str, start: datetime, end: datetime, *, adjusted: bool = True
            ) -> list[Bar]:
                if symbol == "BOOM":
                    raise RuntimeError("upstream 500")
                return [b for b in good if b.symbol == symbol]

        advs = classify_liquidity_tier(ExplodingAdapter(), ["BOOM", "GOOD"], BACKTEST_START)
        assert advs["BOOM"] is None
        assert advs["GOOD"] == pytest.approx(50_000_000.0)


class TestLiquidityTierRates:
    def test_symbols_at_or_above_the_floor_get_the_tier_rate(self) -> None:
        rates = liquidity_tier_rates(
            {"MEGA": 2_000_000_000.0, "THIN": 30_000_000.0},
            tier_adv_floor=1_000_000_000.0,
            tier_slippage_bps=2.0,
        )
        assert rates == {"MEGA": 2.0}

    def test_symbols_below_the_floor_are_omitted_not_zeroed(self) -> None:
        rates = liquidity_tier_rates(
            {"THIN": 30_000_000.0}, tier_adv_floor=1_000_000_000.0, tier_slippage_bps=2.0
        )
        assert "THIN" not in rates
        assert rates == {}

    def test_unmeasured_symbols_are_omitted(self) -> None:
        rates = liquidity_tier_rates({"GHOST": None}, tier_adv_floor=1.0, tier_slippage_bps=2.0)
        assert rates == {}

    def test_exactly_at_the_floor_qualifies(self) -> None:
        rates = liquidity_tier_rates(
            {"EDGE": 1_000_000_000.0}, tier_adv_floor=1_000_000_000.0, tier_slippage_bps=2.0
        )
        assert rates == {"EDGE": 2.0}

    def test_defaults_use_the_named_constants(self) -> None:
        rates = liquidity_tier_rates({"MEGA": DEFAULT_TIER_ADV_FLOOR})
        assert rates == {"MEGA": LIQUID_TIER_SLIPPAGE_BPS}

    def test_negative_floor_rejected(self) -> None:
        with pytest.raises(ValueError, match="tier_adv_floor must be non-negative"):
            liquidity_tier_rates({}, tier_adv_floor=-1.0)

    def test_negative_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="tier_slippage_bps must be non-negative"):
            liquidity_tier_rates({}, tier_slippage_bps=-1.0)


class TestTheCliFlagIsOptInAndAdditive:
    """The claim that matters: the flag changes costs, and only when passed."""

    @staticmethod
    def _write_csv(directory: Path, symbol: str, base_price: float) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        rows = ["ts,open,high,low,close,volume"]
        # Starts well before --from (2021-01-04) so the ADV formation window
        # (90 calendar days ending the day before --from, same as --min-adv) has
        # data to measure — otherwise every symbol reads "unverified" and the
        # tier map is always empty, regardless of the flag.
        start = datetime(2021, 1, 4, tzinfo=UTC) - timedelta(days=150)
        for i in range(190):
            day = start + timedelta(days=i)
            close = base_price * (1.0 + 0.03 * ((-1) ** i))
            rows.append(
                f"{day.date().isoformat()},{close:.4f},{close * 1.01:.4f},"
                f"{close * 0.99:.4f},{close:.4f},50000000"
            )
        (directory / f"{symbol}.csv").write_text("\n".join(rows) + "\n")

    def _backtest(self, data: Path, out: Path, *extra: str) -> float:
        result = CliRunner().invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--source",
                "csv",
                "--cache-dir",
                str(data),
                "--symbols",
                "AAA",
                "--no-plot",
                "--out",
                str(out),
                "--from",
                "2021-01-04",
                "--to",
                "2021-02-13",
                *extra,
            ],
        )
        assert result.exit_code == 0, result.output
        document = json.loads((out.parent / "result.json").read_text())
        equity: float = document["metrics"]["total_return"]
        return equity

    def test_without_the_flag_costs_are_flat_and_unchanged(self, tmp_path: Path) -> None:
        """CSV volume is high enough to clear DEFAULT_TIER_ADV_FLOOR, but without
        the flag that must not matter at all — byte-identical to no tiering.
        """
        data = tmp_path / "data"
        # $50M/day * ~100 price =~ way above any floor, but the flag is absent.
        self._write_csv(data, "AAA", 100.0)

        untiered = self._backtest(data, tmp_path / "untiered" / "e.csv")
        explicit_flat = self._backtest(
            data, tmp_path / "flat" / "e.csv", "--liquidity-tier-adv", "999999999999999"
        )
        # A floor no symbol can ever clear must reproduce the untouched baseline
        # exactly, proving the mechanism is inert until data crosses the floor.
        assert untiered == pytest.approx(explicit_flat)

    def test_a_tiered_symbol_gets_a_different_result_than_flat(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        # close ~100, volume 50,000,000 -> ADV ~ $5B/day, comfortably above the
        # $1B default floor.
        self._write_csv(data, "AAA", 100.0)

        flat = self._backtest(data, tmp_path / "flat" / "e.csv")
        tiered = self._backtest(
            data,
            tmp_path / "tiered" / "e.csv",
            "--liquidity-tier-adv",
            "1000000000",
            "--liquidity-tier-slippage-bps",
            "2.0",
        )
        assert tiered != pytest.approx(flat)

    def test_a_symbol_below_the_floor_is_unaffected_by_the_flag(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        # close ~10, volume 50,000,000 -> ADV ~ $500M/day, below a $1B floor.
        self._write_csv(data, "AAA", 10.0)

        flat = self._backtest(data, tmp_path / "flat" / "e.csv")
        tiered = self._backtest(
            data, tmp_path / "tiered" / "e.csv", "--liquidity-tier-adv", "1000000000"
        )
        assert tiered == pytest.approx(flat)

    def test_the_liquidity_tier_report_is_printed(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        self._write_csv(data, "AAA", 100.0)
        result = CliRunner().invoke(
            app,
            [
                "backtest",
                "--strategy",
                "sma_crossover",
                "--source",
                "csv",
                "--cache-dir",
                str(data),
                "--symbols",
                "AAA",
                "--no-plot",
                "--out",
                str(tmp_path / "e.csv"),
                "--from",
                "2021-01-04",
                "--to",
                "2021-02-13",
                "--liquidity-tier-adv",
                "1000000000",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Liquidity cost tier" in result.output
        assert "AAA" in result.output
