"""Per-asset-class trading costs: 5 bps is an equity number (ADR-0060, KAN-707).

``CostConfig`` modelled one venue — commission-free US equities — and
``commission_per_share`` cannot express a percentage-of-notional fee at all, so a
crypto backtest was priced as if Alpaca's 25 bps taker fee did not exist. These
tests pin the four things that fixes:

**The posture is a value, not a branch** (ADR-0055's shape). ``CostConfig.crypto()``
differs from ``CostConfig.equity()`` in exactly one field, asserted by diffing the
two dataclasses, so a later "while we're here" widening of the slippage turns red.

**The number is sourced, not fitted.** ``CRYPTO_TAKER_FEE_BPS`` is Alpaca's
published tier-1 taker rate, and the test asserts it reproduces the *independently
measured* position ratio from ADR-0058 §5 — two derivations, one number.

**The fee is charged, and charged in the right places.** On notional rather than per
unit, on both sides, and — the one that matters for honesty — **not** inside the
fill price, because ADR-0038's divergence statistic is a price ratio and folding the
fee in would fabricate a divergence against every real fill.

**The equity path does not move.** Every default is unchanged and the arithmetic
reduces exactly to what it was, which is what lets the ADR-0060 hash check pass.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading.broker import CostModel, SimulatedBroker
from trading.calendar import CRYPTO_24_7
from trading.cli import app
from trading.config import CRYPTO_TAKER_FEE_BPS, CostConfig, RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.divergence import render_report, summarize
from trading.engine import Engine
from trading.frequency import Frequency
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.types import Bar, Order, Portfolio, Side

# The ratio KAN-708 measured on the live paper account: a BUY of `q` credited
# `q * RATIO` of the coin, twice, independently (ADR-0058 §5).
MEASURED_POSITION_RATIO = 0.99750000


def _bar(price: float) -> Bar:
    return Bar(
        symbol="BTC/USD",
        ts=datetime(2021, 1, 2, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1_000,
    )


class TestTheCostPostures:
    """What the two named cost models are, and what they are not."""

    def test_equity_posture_is_exactly_the_field_defaults(self) -> None:
        assert CostConfig.equity() == CostConfig()

    def test_crypto_posture_changes_exactly_one_field(self) -> None:
        """ADR-0055's shape, applied to costs: one field, and it is the fee.

        Specifically it does **not** re-tune ``slippage_bps``. ADR-0052 refused to
        move that on 60 paired equity fills; the crypto evidence is 3 (ADR-0058), so
        less evidence cannot justify more tuning.
        """
        equity = dataclasses.asdict(CostConfig.equity())
        crypto = dataclasses.asdict(CostConfig.crypto())
        differing = {k for k in equity if equity[k] != crypto[k]}

        assert differing == {"taker_fee_bps"}
        assert crypto["slippage_bps"] == 5.0
        assert crypto["commission_per_share"] == 0.0

    def test_the_published_rate_reproduces_the_measured_ratio(self) -> None:
        """The card's actual demand: sourced, and reconciled against observation.

        Alpaca's published **tier-1 taker** rate is 0.25%
        (https://docs.alpaca.markets/us/docs/crypto-fees, read 2026-08-14), and
        KAN-708 measured the paper venue crediting `0.99750000` of a BUY while the
        account sat in tier 1.

        ADR-0060 went further and measured **tier 2** as well, after the account's
        trailing 30-day volume crossed $100K: ETH/USD 22.0005 bps and BTC/USD 22.0012
        bps, against a published 0.22%. So the venue implements the schedule at two
        tiers on two pairs — which is why 25.0 is *sourced* rather than fitted.

        This pins the **tier-1** row, which is what the preset models: a fresh
        account starts there and it is the most expensive taker row, so the model is
        conservative for anyone who has traded down a tier. An account's actual tier
        moves with its own volume and is corrected with ``--taker-fee-bps``.
        """
        assert CRYPTO_TAKER_FEE_BPS == 25.0
        implied_ratio = 1.0 - CRYPTO_TAKER_FEE_BPS / 10_000.0
        assert implied_ratio == pytest.approx(MEASURED_POSITION_RATIO, abs=1e-9)

    def test_crypto_posture_refuses_a_free_venue(self) -> None:
        """ "Off" is a refusal, not a config — ADR-0055's rule for a preset.

        Nor is zero reachable from the published schedule: the cheapest taker row is
        tier 8's 0.10%, and only a *maker* ever pays 0.00%.
        """
        with pytest.raises(ValueError, match="requires a positive taker fee"):
            CostConfig.crypto(taker_fee_bps=0.0)

    def test_a_higher_volume_tier_is_expressible(self) -> None:
        """Tier 8 taker is 0.10%. The preset is a default, not a ceiling."""
        assert CostConfig.crypto(taker_fee_bps=10.0).taker_fee_bps == 10.0

    def test_a_negative_fee_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="taker_fee_bps must be non-negative"):
            CostConfig(taker_fee_bps=-1.0)


class TestTheFeeIsChargedCorrectly:
    """Where the fee lands, and — just as load-bearing — where it does not."""

    def test_equity_commission_is_still_zero(self) -> None:
        model = CostModel(CostConfig.equity())

        assert model.commission(10.0, 100.0) == 0.0

    def test_the_fee_is_a_fraction_of_notional_not_a_per_unit_amount(self) -> None:
        """The card's "the commission model does not have the right shape".

        Double the price at the same quantity and a per-share commission is
        unchanged while this one doubles. That is why a term was added rather than
        ``commission_per_share`` reused.
        """
        model = CostModel(CostConfig.crypto())

        # $200 of notional at 25 bps is $0.50, and it is the *notional* that decides:
        # doubling either factor doubles the fee, which a per-share commission does
        # not do for the price.
        assert model.commission(2.0, 100.0) == pytest.approx(0.50)
        assert model.commission(2.0, 200.0) == pytest.approx(1.00)
        assert model.commission(4.0, 100.0) == pytest.approx(1.00)

    def test_both_sides_pay_the_same_fee(self) -> None:
        """Alpaca charges on the *credited* asset — coin on a buy, fiat on a sell.

        Both are ``qty * price * f`` at the fill price, so this is an equality
        rather than an approximation.
        """
        model = CostModel(CostConfig.crypto())
        buy = model.commission(1.5, 40_000.0)
        sell = model.commission(1.5, 40_000.0)

        assert buy == sell == pytest.approx(1.5 * 40_000.0 * 0.0025)

    def test_the_fee_is_not_inside_the_fill_price(self) -> None:
        """The honesty invariant, and the reason this is a separate term.

        ADR-0038's statistic is ``fill_price / reference_price``. The venue's real
        fee is taken out of the received asset and is genuinely *not* in the price
        it reports, so a model that priced it in would show a permanent ~25 bps gap
        against every real fill and invite someone to "fix" a cost model that was
        right. Slippage alone moves the price, on both postures identically.
        """
        equity = CostModel(CostConfig.equity())
        crypto = CostModel(CostConfig.crypto())

        for side in (Side.BUY, Side.SELL):
            assert crypto.fill_price(side, 100.0) == equity.fill_price(side, 100.0)
        assert crypto.fill_price(Side.BUY, 100.0) == pytest.approx(100.05)
        assert crypto.fill_price(Side.SELL, 100.0) == pytest.approx(99.95)


class TestTheSimulatedBrokerCharges:
    """The backtest path, which is where a cost assumption changes a published number."""

    def test_a_crypto_buy_pays_the_fee_out_of_cash(self) -> None:
        portfolio = Portfolio(cash=10_000.0)
        broker = SimulatedBroker(portfolio, CostConfig.crypto())
        broker.submit(Order("BTC/USD", Side.BUY, 1.0))

        (fill,) = broker.on_bar({"BTC/USD": _bar(1_000.0)})

        # Fill price carries slippage only; the fee is the commission.
        assert fill.price == pytest.approx(1_000.5)
        assert fill.commission == pytest.approx(1_000.5 * 0.0025)
        assert portfolio.cash == pytest.approx(10_000.0 - 1_000.5 - 1_000.5 * 0.0025)

    def test_a_crypto_sell_receives_less_than_the_gross_proceeds(self) -> None:
        # Fund the buy leg, establish the position, then zero the cash so the only
        # thing the assertion below can be measuring is the sell's own proceeds.
        portfolio = Portfolio(cash=10_000.0)
        broker = SimulatedBroker(portfolio, CostConfig.crypto())
        broker.submit(Order("BTC/USD", Side.BUY, 1.0))
        assert len(broker.on_bar({"BTC/USD": _bar(1_000.0)})) == 1
        portfolio.cash = 0.0

        broker.submit(Order("BTC/USD", Side.SELL, 1.0))
        (fill,) = broker.on_bar({"BTC/USD": _bar(1_000.0)})

        assert fill.commission > 0.0
        assert portfolio.cash == pytest.approx(999.5 - 999.5 * 0.0025)

    def test_the_equity_broker_is_arithmetically_unchanged(self) -> None:
        """Why the ADR-0060 hash check can pass at all."""
        portfolio = Portfolio(cash=10_000.0)
        broker = SimulatedBroker(portfolio, CostConfig.equity())
        broker.submit(Order("AAA", Side.BUY, 10.0))

        (fill,) = broker.on_bar(
            {"AAA": Bar("AAA", datetime(2021, 1, 2, tzinfo=UTC), 100.0, 100.0, 100.0, 100.0, 1)}
        )

        assert fill.commission == 0.0
        assert portfolio.cash == pytest.approx(10_000.0 - 10.0 * 100.05)

    def test_the_fee_can_cost_a_fully_invested_buy_its_funding(self) -> None:
        """The one real cost of charging the fee in cash, asserted rather than argued.

        A cash fee needs cash the sizer never reserved. This is the same class as
        ADR-0037's benchmark-flatness bug, so it is pinned here deliberately: an
        order that fits without the fee is *rejected* with it, recorded and never
        raised. ADR-0060 measures what that costs in practice (entry rejections
        roughly double; no run is left flat, because ADR-0037's retry absorbs it).
        """
        order = Order("BTC/USD", Side.BUY, 1.0)
        bar = {"BTC/USD": _bar(1_000.0)}
        # Exactly enough cash for the slipped price and not a cent more.
        cash = 1_000.5

        free = SimulatedBroker(Portfolio(cash=cash), CostConfig.equity())
        free.submit(order)
        assert len(free.on_bar(bar)) == 1
        assert free.rejections == []

        charged = SimulatedBroker(Portfolio(cash=cash), CostConfig.crypto())
        charged.submit(order)
        assert charged.on_bar(bar) == []
        assert len(charged.rejections) == 1
        assert "insufficient cash" in charged.rejections[0][1]


class TestTheDivergenceReportStatesWhatItCannotSee:
    """A cost model whose largest term the instrument cannot observe (ADR-0060)."""

    def test_the_summary_carries_the_modelled_fee(self) -> None:
        assert summarize([], costs=CostConfig.crypto()).modelled_taker_fee_bps == 25.0

    def test_an_equity_summary_reports_no_fee(self) -> None:
        assert summarize([], costs=CostConfig.equity()).modelled_taker_fee_bps == 0.0

    def test_a_crypto_report_says_the_fee_is_unmeasured(self) -> None:
        """It must not print a bare number under a heading that reads as "checked"."""
        report = render_report(summarize([], costs=CostConfig.crypto()), [])

        assert "NOT MEASURED BY THIS REPORT" in report
        assert "25.00 bps of notional" in report

    def test_an_equity_report_is_unchanged(self) -> None:
        """No fee, no line — so an equity divergence block keeps its exact bytes."""
        assert "Venue fee" not in render_report(summarize([], costs=CostConfig.equity()), [])


class TestEndToEndThroughTheEngine:
    """The claim that actually matters: a crypto backtest is priced differently."""

    @staticmethod
    def _run(costs: CostConfig) -> float:
        freq = Frequency.parse("1d", calendar=CRYPTO_24_7)
        adapter = SyntheticAdapter(seed=7, frequency=freq)
        broker = SimulatedBroker(Portfolio(cash=1_000.0), costs)
        result = Engine(adapter, broker, Guardrails(RiskConfig.crypto())).run(
            get_strategy("sma_crossover"),
            ["BTC/USD", "ETH/USD"],
            datetime(2021, 1, 1, tzinfo=UTC),
            datetime(2021, 12, 31, tzinfo=UTC),
        )
        return result.equity_curve[-1].equity

    def test_charging_the_fee_lowers_the_reported_return(self) -> None:
        """A turnover cost must show up as a worse number, or it is not being charged."""
        assert self._run(CostConfig.crypto()) < self._run(CostConfig.equity())


class TestTheFeeReachesTheCommandsThemselves:
    """That ``--market crypto`` really *charges*, asserted through the CLI.

    These exist because mutation testing said they had to. Three mutations
    survived the first pass of this file with **zero** red tests — pointing
    ``_MARKET_COSTS[crypto]`` at the equity model, dropping ``costs`` from the
    backtest's broker, and dropping it from every sweep trial — because everything
    above builds its broker directly and never exercises the wiring in ``cli.py``.
    A cost model nothing charges is the exact defect this card exists to fix, so
    the wiring gets its own end-to-end assertions.

    ``--source csv`` is load-bearing for the same reason it is in
    ``test_cli_market``: the bars are identical on both markets by construction, so
    the *only* thing that can move the equity curve is the cost model. The control
    is the same command with ``--taker-fee-bps 0``, which isolates the fee from the
    calendar and the risk posture, both of which also change with ``--market``.
    """

    @staticmethod
    def _write_csv(directory: Path, symbol: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        rows = ["ts,open,high,low,close,volume"]
        for i in range(120):
            day = datetime(2021, 1, 4, tzinfo=UTC) + timedelta(days=i)
            # A wave with enough turns to make sma_crossover trade repeatedly, so a
            # per-trade fee compounds into a visible difference.
            close = 100.0 * (1.0 + 0.05 * math.sin(i / 4.0) + 0.002 * i)
            rows.append(
                f"{day.date().isoformat()},{close:.4f},{close * 1.01:.4f},"
                f"{close * 0.99:.4f},{close:.4f},1000000"
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
                "--market",
                "crypto",
                "--out",
                str(out),
                "--from",
                "2021-01-04",
                "--to",
                "2021-05-03",
                *extra,
            ],
        )
        assert result.exit_code == 0, result.output
        document = json.loads((out.parent / "result.json").read_text())
        equity: float = document["metrics"]["total_return"]
        return equity

    def test_a_crypto_backtest_is_charged_the_venue_fee(self, tmp_path: Path) -> None:
        """Kills two mutations: the ``_MARKET_COSTS`` entry and the broker's costs."""
        data = tmp_path / "data"
        self._write_csv(data, "AAA")

        charged = self._backtest(data, tmp_path / "charged" / "e.csv")
        free = self._backtest(data, tmp_path / "free" / "e.csv", "--taker-fee-bps", "0")

        assert charged < free, (
            "a crypto backtest must pay the venue fee; identical bars and an "
            "identical strategy differ only by the cost model here"
        )

    def test_a_sweep_trial_is_charged_the_venue_fee(self, tmp_path: Path) -> None:
        """Kills the third: every sweep trial runs its own broker (ADR-0016)."""
        data = tmp_path / "data"
        self._write_csv(data, "AAA")

        def _returns(out: Path, *extra: str) -> list[float]:
            result = CliRunner().invoke(
                app,
                [
                    "sweep",
                    "--strategy",
                    "sma_crossover",
                    "--source",
                    "csv",
                    "--cache-dir",
                    str(data),
                    "--symbols",
                    "AAA",
                    "--param",
                    "fast=3",
                    "--param",
                    "slow=10",
                    "--market",
                    "crypto",
                    "--out",
                    str(out),
                    "--from",
                    "2021-01-04",
                    "--to",
                    "2021-05-03",
                    *extra,
                ],
            )
            assert result.exit_code == 0, result.output
            with out.open(newline="") as handle:
                return [float(row["total_return"]) for row in csv.DictReader(handle)]

        charged = _returns(tmp_path / "charged.csv")
        free = _returns(tmp_path / "free.csv", "--taker-fee-bps", "0")

        assert charged and len(charged) == len(free)
        assert all(c < f for c, f in zip(charged, free, strict=True))
