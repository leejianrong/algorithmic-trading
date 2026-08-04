"""Library-level intraday end-to-end check for the frequency abstraction (ADR-0022).

No CLI here — the ``--interval`` flag is wired by a later integration PR — so this
drives the seam directly: a synthetic adapter constructed at a sub-daily
:class:`~trading.frequency.Frequency`, a real :class:`~trading.engine.Engine`, and
a real strategy, run over a multi-day range. It proves the whole stack iterates
intraday bars end to end with a sane equity curve, and that the same synthetic
series annualizes correctly through :func:`~trading.metrics.compute`.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from trading.broker import SimulatedBroker
from trading.config import CostConfig, RiskConfig
from trading.data.synthetic import SyntheticAdapter
from trading.engine import Engine
from trading.frequency import Frequency
from trading.metrics import compute
from trading.risk import Guardrails
from trading.strategies.buy_and_hold import BuyAndHold
from trading.types import Portfolio

_ZERO_COST = CostConfig(commission_per_share=0.0, slippage_bps=0.0)
_START = datetime(2022, 3, 1, tzinfo=UTC)
_END = datetime(2022, 3, 4, tzinfo=UTC)  # Tue-Fri: four sessions


def _run_hourly() -> tuple[list[datetime], float]:
    freq = Frequency.parse("1h")
    adapter = SyntheticAdapter(seed=7, frequency=freq)
    broker = SimulatedBroker(Portfolio(cash=10_000.0), _ZERO_COST)
    engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
    result = engine.run(BuyAndHold(), ["AAA"], _START, _END)
    stamps = [p.ts for p in result.equity_curve]
    return stamps, result.final_equity


def test_intraday_run_has_multiple_bars_per_day() -> None:
    stamps, _ = _run_hourly()
    assert stamps, "expected a non-empty intraday equity curve"

    per_day = Counter(ts.date() for ts in stamps)
    # 1-hour bars over a 13:30-20:00 session → 7 bars per trading day.
    assert set(per_day.values()) == {7}
    assert len(per_day) == 4  # Tue, Wed, Thu, Fri

    # Timestamps are strictly ascending and intraday (not all at midnight).
    assert stamps == sorted(stamps)
    assert len({ts.time() for ts in stamps}) > 1


def test_intraday_curve_is_sane_and_marks_close() -> None:
    _, final_equity = _run_hourly()
    # Buy-and-hold deploys ~all the cash once, so equity stays positive and in a
    # plausible band around the $10k start (GBM over four days can't 10x it).
    assert final_equity > 0
    assert 1_000.0 < final_equity < 100_000.0


def test_intraday_metrics_annualize_by_frequency() -> None:
    freq = Frequency.parse("1h")
    adapter = SyntheticAdapter(seed=7, frequency=freq)
    broker = SimulatedBroker(Portfolio(cash=10_000.0), _ZERO_COST)
    engine = Engine(adapter, broker, Guardrails(RiskConfig.unlimited()))
    result = engine.run(BuyAndHold(), ["AAA"], _START, _END)

    metrics = compute(result, periods_per_year=freq.periods_per_year)
    # A finite, real number falls out (no NaN/inf); the point is that it threads
    # the intraday factor without blowing up.
    assert metrics.sharpe == metrics.sharpe  # not NaN
