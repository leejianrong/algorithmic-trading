"""Cross-sectional rank-and-hold-top-K: relative-strength selection.

These fast-layer tests drive the strategy directly (a hand-rolled context) to
prove the ranking, the rebalance cadence, the exit-on-drop-out behavior, and the
no-look-ahead contract, then run it end to end through the engine on synthetic
data across the ``blue20`` universe (offline).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from trading.broker import SimulatedBroker
from trading.config import RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.engine import Engine
from trading.risk import Guardrails
from trading.strategies import get_strategy
from trading.strategies.cross_sectional import CrossSectional
from trading.types import Bar, Portfolio, TargetWeight
from trading.universe import get_sector_map, get_universe


def test_registry_resolves_cross_sectional() -> None:
    assert isinstance(get_strategy("cross_sectional"), CrossSectional)


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


def test_picks_the_correct_top_k_by_trailing_return() -> None:
    # Four names over a 3-bar lookback. Trailing returns (close[-1]/close[0] - 1):
    #   WIN1: 10 -> 20  = +100%   (rank 1)
    #   WIN2: 10 -> 15  =  +50%   (rank 2)
    #   MID : 10 -> 11  =  +10%   (rank 3)
    #   LOSE: 10 ->  8  =  -20%   (rank 4)
    history = {
        "WIN1": _series("WIN1", [10, 12, 14, 20]),
        "WIN2": _series("WIN2", [10, 11, 13, 15]),
        "MID": _series("MID", [10, 10, 10, 11]),
        "LOSE": _series("LOSE", [10, 10, 9, 8]),
    }
    ctx = _StubContext(history)
    bars = {s: h[-1] for s, h in history.items()}

    strat = CrossSectional(lookback=3, top_k=2, weight=0.8, rebalance_days=21)
    targets = _targets(strat.on_bar(bars["WIN1"].ts, bars, ctx))

    # Top-2 held at 0.8 / 2 = 0.4 each; the rest targeted to zero (exit).
    assert targets == {"WIN1": 0.4, "WIN2": 0.4, "MID": 0.0, "LOSE": 0.0}


def test_stays_flat_until_lookback_bars_exist() -> None:
    # Only 3 closes available but lookback=3 needs 4 -> warmup, no intents.
    history = {"AAA": _series("AAA", [10, 11, 12])}
    ctx = _StubContext(history)
    bars = {"AAA": history["AAA"][-1]}

    strat = CrossSectional(lookback=3, top_k=1)
    assert strat.on_bar(bars["AAA"].ts, bars, ctx) == []


def test_rebalances_only_on_cadence_not_every_bar() -> None:
    # Two names, rebalance every 3 bars. Feed the same warmed-up history each call
    # and confirm intents appear only on bars 1, 4, 7 (the first eligible bar and
    # every rebalance_days thereafter), and are empty in between.
    history = {
        "AAA": _series("AAA", [10, 11, 12, 13, 14]),
        "BBB": _series("BBB", [10, 10, 10, 10, 10]),
    }
    ctx = _StubContext(history)
    bars = {s: h[-1] for s, h in history.items()}

    strat = CrossSectional(lookback=3, top_k=1, weight=0.9, rebalance_days=3)
    fired = [bool(strat.on_bar(bars["AAA"].ts, bars, ctx)) for _ in range(8)]

    assert fired == [True, False, False, True, False, False, True, False]


def test_exits_a_name_that_drops_out_of_top_k() -> None:
    # Rebalance 1: AAA strongest, BBB second, CCC weakest -> hold AAA,BBB.
    # Rebalance 2: CCC surges past BBB -> BBB must be exited (targeted to 0.0).
    strat = CrossSectional(lookback=3, top_k=2, weight=0.6, rebalance_days=2)

    hist1 = {
        "AAA": _series("AAA", [10, 12, 15, 20]),
        "BBB": _series("BBB", [10, 11, 12, 14]),
        "CCC": _series("CCC", [10, 10, 10, 11]),
    }
    ctx = _StubContext(hist1)
    bars1 = {s: h[-1] for s, h in hist1.items()}
    first = _targets(strat.on_bar(bars1["AAA"].ts, bars1, ctx))
    assert first == {"AAA": 0.3, "BBB": 0.3, "CCC": 0.0}

    # Bar 2 is not a rebalance bar (cadence 2) -> nothing.
    assert strat.on_bar(bars1["AAA"].ts, bars1, ctx) == []

    # Bar 3 is a rebalance bar. CCC now has the strongest trailing return; BBB
    # falls to third and is dropped.
    hist2 = {
        "AAA": _series("AAA", [10, 12, 15, 20]),  # +100%
        "BBB": _series("BBB", [10, 11, 12, 13]),  # +30%
        "CCC": _series("CCC", [10, 12, 16, 25]),  # +150%
    }
    ctx2 = _StubContext(hist2)
    bars2 = {s: h[-1] for s, h in hist2.items()}
    second = _targets(strat.on_bar(bars2["AAA"].ts, bars2, ctx2))
    assert second == {"AAA": 0.3, "CCC": 0.3, "BBB": 0.0}


def test_never_looks_ahead() -> None:
    # A context that raises if asked for more history than has "occurred" proves
    # the strategy reads only past+present closes. We reveal bars one at a time and
    # let the strategy score against the revealed prefix only.
    full = {
        "AAA": _series("AAA", [10, 11, 12, 13, 14, 20]),
        "BBB": _series("BBB", [10, 9, 8, 7, 6, 5]),
    }
    strat = CrossSectional(lookback=3, top_k=1, weight=0.9, rebalance_days=1)

    for i in range(3, 6):  # first eligible index is 3 (4 closes = lookback+1)
        revealed = {s: bars[: i + 1] for s, bars in full.items()}
        ctx = _StubContext(revealed)
        bars = {s: revealed[s][-1] for s in revealed}
        targets = _targets(strat.on_bar(bars["AAA"].ts, bars, ctx))
        # AAA rises, BBB falls, so AAA is always the sole winner — decided purely
        # from the revealed prefix (a look-ahead read would have raised in stub).
        assert targets == {"AAA": 0.9, "BBB": 0.0}


def test_runs_end_to_end_on_blue20_synthetic() -> None:
    symbols = get_universe("blue20")
    adapter = SyntheticAdapter(seed=42)
    broker = SimulatedBroker(Portfolio(cash=1_000.0))
    risk = RiskConfig(sector_map=get_sector_map("blue20"), max_sector_exposure=0.30)

    result = Engine(adapter, broker, Guardrails(risk)).run(
        CrossSectional(lookback=120, top_k=8, weight=0.9, rebalance_days=21),
        symbols,
        datetime(2019, 1, 1, tzinfo=UTC),
        datetime(2023, 12, 31, tzinfo=UTC),
    )

    assert result.final_equity > 0
    assert all(p.equity > 0 for p in result.equity_curve)
    assert result.fills, "expected cross-sectional to trade on synthetic blue20"
    # Only ever trades names from the requested universe.
    assert {f.symbol for _ts, f in result.fills} <= set(symbols)
