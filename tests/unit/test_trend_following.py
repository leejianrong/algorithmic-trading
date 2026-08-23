"""Time-series (absolute) momentum, managed-futures style: per-asset, long-or-cash.

These fast-layer tests drive the strategy directly (a hand-rolled context) to
prove the per-asset independence (no cross-sectional ranking), the skip-recent
window, the rebalance cadence, the all-cash failure mode, and the
no-look-ahead contract, then run it end to end through the engine on synthetic
data across the ``trend_etfs`` universe (offline).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.engine import Engine
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.strategies.trend_following import TrendFollowing
from trading.types import Bar, Portfolio, TargetWeight
from trading.universe import get_sector_map, get_universe


def test_registry_resolves_trend_following() -> None:
    assert isinstance(get_strategy("trend_following"), TrendFollowing)


class _StubContext:
    """A minimal StrategyContext over pre-built per-symbol history."""

    def __init__(self, history: dict[str, list[Bar]]) -> None:
        self._history = history
        self.portfolio = Portfolio(cash=1_000.0)

    def history(self, symbol: str, lookback: int) -> list[Bar]:
        return self._history.get(symbol, [])[-lookback:]


def _series(symbol: str, closes: list[float]) -> list[Bar]:
    """Build a bar series for ``symbol`` from a list of closes (one per day)."""
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        Bar(symbol, base + timedelta(days=i), open=c, high=c, low=c, close=c, volume=1_000)
        for i, c in enumerate(closes)
    ]


def _targets(intents: Sequence[object]) -> dict[str, float]:
    """Collapse a list of TargetWeight intents into a symbol -> weight map."""
    out: dict[str, float] = {}
    for intent in intents:
        assert isinstance(intent, TargetWeight)
        out[intent.symbol] = intent.weight
    return out


class TestConstructorValidation:
    def test_rejects_non_positive_lookback(self) -> None:
        with pytest.raises(ValueError, match="lookback"):
            TrendFollowing(lookback=0)

    def test_rejects_negative_skip_recent(self) -> None:
        with pytest.raises(ValueError, match="skip_recent"):
            TrendFollowing(skip_recent=-1)

    def test_rejects_skip_recent_at_or_above_lookback(self) -> None:
        with pytest.raises(ValueError, match="skip_recent"):
            TrendFollowing(lookback=10, skip_recent=10)

    def test_rejects_weight_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="weight"):
            TrendFollowing(weight=0.0)
        with pytest.raises(ValueError, match="weight"):
            TrendFollowing(weight=1.5)

    def test_rejects_non_positive_rebalance_days(self) -> None:
        with pytest.raises(ValueError, match="rebalance_days"):
            TrendFollowing(rebalance_days=0)


def test_each_asset_scored_independently_not_ranked() -> None:
    # Three names, lookback=3, skip_recent=0 -> window of 4 closes.
    # AAA and BBB both trend up (positive trailing return); CCC trends down.
    # A cross-sectional strategy would pick a top-K; this one holds BOTH
    # trending names, weighted 1/2 each (not 1/3 of the whole universe).
    history = {
        "AAA": _series("AAA", [10, 12, 14, 20]),  # +100%
        "BBB": _series("BBB", [10, 10, 10, 11]),  # +10%
        "CCC": _series("CCC", [10, 10, 9, 8]),  # -20%
    }
    ctx = _StubContext(history)
    bars = {s: h[-1] for s, h in history.items()}

    strat = TrendFollowing(lookback=3, skip_recent=0, weight=0.8, rebalance_days=21)
    targets = _targets(strat.on_bar(bars["AAA"].ts, bars, ctx))

    assert targets == {"AAA": 0.4, "BBB": 0.4, "CCC": 0.0}


def test_all_trending_splits_weight_across_the_whole_universe() -> None:
    history = {
        "AAA": _series("AAA", [10, 11, 12, 13]),
        "BBB": _series("BBB", [10, 10, 10, 11]),
    }
    ctx = _StubContext(history)
    bars = {s: h[-1] for s, h in history.items()}

    strat = TrendFollowing(lookback=3, skip_recent=0, weight=0.9, rebalance_days=1)
    targets = _targets(strat.on_bar(bars["AAA"].ts, bars, ctx))

    assert targets == {"AAA": 0.45, "BBB": 0.45}


def test_nothing_trending_goes_entirely_to_cash() -> None:
    history = {
        "AAA": _series("AAA", [10, 9, 8, 7]),
        "BBB": _series("BBB", [10, 9, 8, 6]),
    }
    ctx = _StubContext(history)
    bars = {s: h[-1] for s, h in history.items()}

    strat = TrendFollowing(lookback=3, skip_recent=0, weight=0.9, rebalance_days=1)
    targets = _targets(strat.on_bar(bars["AAA"].ts, bars, ctx))

    assert targets == {"AAA": 0.0, "BBB": 0.0}


def test_skip_recent_ignores_the_most_recent_bars() -> None:
    # lookback=3, skip_recent=2: signal = close[-3] / close[-6] - 1, i.e. the
    # last two closes are excluded from the signal entirely. Series rises then
    # reverses hard in the excluded tail; skip_recent must not see the reversal.
    closes = [10.0, 11.0, 12.0, 20.0, 5.0, 1.0]  # up to index 3 (20), then a crash
    history = {"AAA": _series("AAA", closes)}
    ctx = _StubContext(history)
    bars = {"AAA": history["AAA"][-1]}

    strat = TrendFollowing(lookback=3, skip_recent=2, weight=0.9, rebalance_days=1)
    # Signal window: start=index0(10), end=index3(20) -> +100%, still "in trend"
    # even though the two most-recent closes (5, 1) collapsed.
    targets = _targets(strat.on_bar(bars["AAA"].ts, bars, ctx))
    assert targets == {"AAA": 0.9}


def test_stays_flat_until_the_full_window_exists() -> None:
    # window = lookback + skip_recent + 1 = 3 + 1 + 1 = 5, but only 4 closes.
    history = {"AAA": _series("AAA", [10, 11, 12, 13])}
    ctx = _StubContext(history)
    bars = {"AAA": history["AAA"][-1]}

    strat = TrendFollowing(lookback=3, skip_recent=1)
    assert strat.on_bar(bars["AAA"].ts, bars, ctx) == []


def test_rebalances_only_on_cadence_not_every_bar() -> None:
    history = {
        "AAA": _series("AAA", [10, 11, 12, 13, 14]),
        "BBB": _series("BBB", [10, 10, 10, 10, 10]),
    }
    ctx = _StubContext(history)
    bars = {s: h[-1] for s, h in history.items()}

    strat = TrendFollowing(lookback=3, skip_recent=0, weight=0.9, rebalance_days=3)
    fired = [bool(strat.on_bar(bars["AAA"].ts, bars, ctx)) for _ in range(8)]

    assert fired == [True, False, False, True, False, False, True, False]


def test_never_looks_ahead() -> None:
    # A context that only ever reveals a growing prefix proves the strategy
    # scores purely off past+present closes: AAA keeps rising, BBB keeps
    # falling, so AAA is always in trend and BBB never is, decided fresh from
    # each revealed prefix (a look-ahead read into the unrevealed tail would
    # change the answer, since both series later reverse).
    full = {
        "AAA": _series("AAA", [10, 11, 12, 13, 14, 1]),  # crashes at the end
        "BBB": _series("BBB", [10, 9, 8, 7, 6, 99]),  # spikes at the end
    }
    strat = TrendFollowing(lookback=3, skip_recent=0, weight=0.9, rebalance_days=1)

    for i in range(3, 5):  # first eligible index is 3 (4 closes = window of 4)
        revealed = {s: bars[: i + 1] for s, bars in full.items()}
        ctx = _StubContext(revealed)
        bars = {s: revealed[s][-1] for s in revealed}
        targets = _targets(strat.on_bar(bars["AAA"].ts, bars, ctx))
        assert targets == {"AAA": 0.9, "BBB": 0.0}


def test_runs_end_to_end_on_trend_etfs_synthetic() -> None:
    symbols = get_universe("trend_etfs")
    adapter = SyntheticAdapter(seed=11)
    broker = SimulatedBroker(Portfolio(cash=1_000.0))
    risk = RiskConfig(sector_map=get_sector_map("trend_etfs"), max_sector_exposure=0.40)

    result = Engine(adapter, broker, Guardrails(risk)).run(
        TrendFollowing(lookback=60, skip_recent=5, weight=0.9, rebalance_days=21),
        symbols,
        datetime(2019, 1, 1, tzinfo=UTC),
        datetime(2023, 12, 31, tzinfo=UTC),
    )

    assert result.final_equity > 0
    assert all(p.equity > 0 for p in result.equity_curve)
    assert result.fills, "expected trend_following to trade on synthetic trend_etfs"
    # Only ever trades names from the requested universe.
    assert {f.symbol for _ts, f in result.fills} <= set(symbols)


def test_trend_etfs_basket_is_registered_and_modest() -> None:
    symbols = get_universe("trend_etfs")
    assert 8 <= len(symbols) <= 15
    assert len(symbols) == len(set(symbols))  # no duplicates
    sectors = get_sector_map("trend_etfs")
    assert set(sectors) == set(symbols)
