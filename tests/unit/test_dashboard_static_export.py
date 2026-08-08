"""Fast, no-infra tests for the self-contained static HTML export.

Asserts the rendered document embeds the run JSON, draws an inline ``<svg>``,
surfaces the metric values / a fills row / guardrail actions, shows the halt
banner for a halted run, and — the load-bearing offline guarantee — contains NO
``http://`` / ``https://`` external reference. Assertions are on substrings and
structure, never brittle full-HTML equality. No engine, no network, no FastAPI.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading.dashboard.payload import build_payload
from trading.dashboard.static_export import render_html, write_html
from trading.engine import BacktestResult, EquityPoint, HaltEpisode
from trading.metrics import PerformanceMetrics, assess_significance, compute
from trading.report import RESULT_SCHEMA_VERSION, result_to_dict
from trading.types import Fill, Order, Portfolio, Side


def _ts(day: int) -> datetime:
    return datetime(2024, 1, day, tzinfo=UTC)


def _metrics() -> PerformanceMetrics:
    return PerformanceMetrics(
        total_return=0.05,
        annualized_return=0.12,
        sharpe=1.1,
        sortino=1.4,
        calmar=0.9,
        max_drawdown=0.03,
        win_rate=0.6,
        turnover=1.5,
        avg_exposure=0.4,
        peak_exposure=0.8,
    )


def _halted_result() -> BacktestResult:
    curve = [EquityPoint(_ts(1), 1000.0, 0.0), EquityPoint(_ts(2), 1050.0, 0.5)]
    fills = [
        (_ts(1), Fill("AAA", Side.BUY, 10.0, 100.0, 1.0)),
        (_ts(2), Fill("AAA", Side.SELL, 5.0, 110.0, 0.5)),
    ]
    clamps = [
        (Order("BBB", Side.BUY, 20.0), Order("BBB", Side.BUY, 12.0), "gross exposure cap"),
    ]
    rejections = [(Order("CCC", Side.BUY, 3.0), "halted: new entries blocked")]
    return BacktestResult(
        symbols=["AAA", "BBB"],
        starting_cash=1000.0,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=1050.0),
        fills=fills,
        rejections=rejections,
        clamps=clamps,
        halted=True,
        halt_ts=_ts(2),
        halt_reason="max drawdown breached",
    )


def _payload(*, halted: bool = True, benchmark: bool = True) -> dict[str, object]:
    result = _halted_result()
    if not halted:
        result.halted = False
        result.halt_ts = None
        result.halt_reason = None
    bench = (
        [EquityPoint(_ts(1), 1000.0, 1.0), EquityPoint(_ts(2), 1010.0, 1.0)] if benchmark else None
    )
    doc = result_to_dict(
        result,
        mode="backtest",
        frequency="1d",
        metrics=_metrics(),
        benchmark_curve=bench,
    )
    return build_payload(doc)


def test_render_html_is_a_complete_document() -> None:
    html = render_html(_payload())
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "<title>" in html
    assert html.rstrip().endswith("</html>")


def test_render_html_embeds_run_json() -> None:
    html = render_html(_payload())
    assert '<script type="application/json" id="run-data">' in html
    # The embedded document JSON carries the schema + run identity.
    assert '"schema_version"' in html
    assert str(RESULT_SCHEMA_VERSION) in html
    assert "AAA" in html


def test_render_html_draws_inline_svg_with_benchmark() -> None:
    html = render_html(_payload(benchmark=True))
    assert "<svg" in html
    assert 'class="equity"' in html
    assert 'class="benchmark"' in html  # overlay present when a benchmark is supplied


def test_render_html_shows_metric_values() -> None:
    html = render_html(_payload())
    assert "Sharpe" in html and "1.10" in html  # ratio metric
    assert "Max drawdown" in html and "3.00%" in html  # percent metric
    assert "+5.00%" in html  # total return 0.05


def test_render_html_shows_a_fills_row() -> None:
    html = render_html(_payload())
    assert "AAA" in html
    assert "$100.00" in html  # first fill price
    assert 'class="side buy"' in html


def test_render_html_shows_guardrail_actions() -> None:
    html = render_html(_payload())
    assert "gross exposure cap" in html  # clamp reason
    assert "halted: new entries blocked" in html  # rejection reason


def test_render_html_shows_halt_banner_when_halted() -> None:
    # The banner element (not just the CSS class definition) is present when halted.
    html = render_html(_payload(halted=True))
    assert 'class="halt-banner"' in html
    assert "Kill switch fired" in html
    assert "max drawdown breached" in html
    # No episodes recorded (a pre-ADR-0031 document): the latching wording stands.
    assert "latched for the rest of the run" in html


def test_render_html_banner_reports_re_armed_halt_episodes() -> None:
    """A run whose halt re-armed must not be described as latched (ADR-0031)."""
    result = _halted_result()
    result.halt_episodes = [
        HaltEpisode(halt_ts=_ts(2), reason="max drawdown breached", resume_ts=_ts(4)),
        HaltEpisode(halt_ts=_ts(6), reason="max drawdown breached"),
    ]
    doc = result_to_dict(result, mode="backtest", frequency="1d", metrics=_metrics())
    html = render_html(build_payload(doc))
    assert "2 halt episode(s), 1 re-armed." in html
    assert "latched for the rest of the run" not in html


def test_render_html_no_halt_banner_when_not_halted() -> None:
    # The CSS rule for .halt-banner is always defined; the banner element is not.
    html = render_html(_payload(halted=False))
    assert 'class="halt-banner"' not in html


def test_render_html_has_no_external_references() -> None:
    # The strict offline guarantee: nothing is fetched over the network.
    html = render_html(_payload())
    assert "http://" not in html
    assert "https://" not in html
    assert "//cdn" not in html.lower()


def test_write_html_writes_self_contained_file(tmp_path: Path) -> None:
    out = write_html(_payload(), tmp_path / "nested" / "dash.html")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "<svg" in text
    assert "http://" not in text and "https://" not in text


# --- Benchmark-relative panel (ADR-0037) -------------------------------------


def _benchmark_doc(
    strategy: list[float],
    bench: list[float] | None,
    *,
    exposure: float = 0.5,
) -> dict[str, Any]:
    """A result document over two hand-built curves, on shared daily timestamps."""
    curve = [EquityPoint(_ts(i + 1), e, exposure) for i, e in enumerate(strategy)]
    result = BacktestResult(
        symbols=["AAA"],
        starting_cash=strategy[0],
        equity_curve=curve,
        final_portfolio=Portfolio(cash=strategy[-1]),
        fills=[],
    )
    bench_curve = (
        [EquityPoint(_ts(i + 1), e, 1.0) for i, e in enumerate(bench)]
        if bench is not None
        else None
    )
    return result_to_dict(
        result,
        mode="backtest",
        frequency="1d",
        metrics=compute(result),
        benchmark_curve=bench_curve,
    )


_STRATEGY_CURVE = [100.0, 104.0, 101.0, 107.0]
_BENCHMARK_CURVE = [50.0, 51.0, 50.0, 52.0]


def test_benchmark_panel_renders_the_relative_statistics() -> None:
    doc = _benchmark_doc(_STRATEGY_CURVE, _BENCHMARK_CURVE)
    html = render_html(build_payload(doc))
    assert "Benchmark-relative" in html
    for label in ("Beta", "Alpha (annualized)", "Correlation", "Information ratio", "Shared bars"):
        assert label in html
    block = doc["benchmark_metrics"]
    assert block["beta"] is not None
    assert f"{block['beta']:.2f}" in html


def test_benchmark_panel_says_so_plainly_when_no_benchmark_ran() -> None:
    # The key is present and null: this run had no benchmark, and the page says
    # that rather than leaving the reader to wonder whether it was just omitted.
    html = render_html(build_payload(_benchmark_doc(_STRATEGY_CURVE, None)))
    assert "Benchmark-relative" in html
    assert "No benchmark ran for this result" in html
    assert "--benchmark SYMBOL" in html


def test_benchmark_panel_is_absent_for_a_pre_adr_0037_document() -> None:
    # An older result.json has no `benchmark_metrics` key at all; invent nothing.
    doc = _benchmark_doc(_STRATEGY_CURVE, None)
    del doc["benchmark_metrics"]
    assert "Benchmark-relative" not in render_html(build_payload(doc))


def test_undefined_statistics_render_n_a_not_zero() -> None:
    # A flat benchmark leaves beta/alpha/correlation undefined. "0.00" would read
    # as a measured zero, which is a different claim entirely.
    doc = _benchmark_doc(_STRATEGY_CURVE, [50.0, 50.0, 50.0, 50.0])
    block = doc["benchmark_metrics"]
    assert block["beta"] is None
    html = render_html(build_payload(doc))
    assert "n/a" in html


def test_the_nested_benchmark_block_never_leaks_into_the_flat_tile_grid() -> None:
    html = render_html(build_payload(_benchmark_doc(_STRATEGY_CURVE, _BENCHMARK_CURVE)))
    # A raw dict repr in a tile would be the failure mode of appending an unknown
    # structured key to the flat metric grid.
    assert "shared_bars" not in html.split('<script type="application/json"')[0]
    assert "{&#x27;" not in html


def test_return_per_unit_exposure_is_shown_as_a_percentage() -> None:
    doc = _benchmark_doc([100.0, 110.0], _BENCHMARK_CURVE[:2], exposure=0.5)
    html = render_html(build_payload(doc))
    assert "Return / exposure" in html
    value = doc["metrics"]["return_per_unit_exposure"]
    assert value is not None
    assert f"{value * 100:+.2f}%" in html


def test_benchmark_panel_keeps_the_page_self_contained() -> None:
    html = render_html(build_payload(_benchmark_doc(_STRATEGY_CURVE, _BENCHMARK_CURVE)))
    assert "http://" not in html
    assert "https://" not in html


# --- Sharpe significance panel (ADR-0039) ------------------------------------


def _significance_doc(
    *, mean: float = 0.0005, benchmark: bool = False, trials: int = 1, seed: int = 404
) -> dict[str, Any]:
    """A run document long enough to bootstrap, plus its significance block."""
    rng = random.Random(seed)
    equity = 1_000.0
    curve = [EquityPoint(_ts(1), equity, 0.5)]
    for i in range(400):
        equity *= 1.0 + rng.gauss(mean, 0.01)
        curve.append(EquityPoint(_ts(1) + timedelta(days=i + 1), equity, 0.5))
    result = BacktestResult(
        symbols=["AAA"],
        starting_cash=curve[0].equity,
        equity_curve=curve,
        final_portfolio=Portfolio(cash=curve[-1].equity),
        fills=[],
    )
    bench = [EquityPoint(p.ts, p.equity * 0.5, 1.0) for p in curve] if benchmark else None
    report = assess_significance(
        curve,
        bench,
        resamples=100,
        trial_sharpes=[0.2 * i for i in range(trials)] if trials > 1 else None,
    )
    return result_to_dict(
        result,
        mode="backtest",
        frequency="1d",
        metrics=compute(result),
        benchmark_curve=bench,
        significance=report,
    )


def test_significance_panel_renders_the_interval_and_its_provenance() -> None:
    html = render_html(build_payload(_significance_doc()))
    assert "Sharpe significance" in html
    for label in ("Point Sharpe", "Block length", "Resamples", "Seed"):
        assert label in html


def test_significance_panel_warns_when_the_interval_straddles_zero() -> None:
    # A zero-drift draw whose 95% interval really does contain zero — asserted on
    # the document first, so the test is about the *rendering*, not about the draw.
    doc = _significance_doc(mean=0.0, seed=1)
    interval = doc["significance"]["sharpe_interval"]
    assert interval["low"] <= 0.0 <= interval["high"]
    assert "straddles zero" in render_html(build_payload(doc))


def test_significance_panel_shows_the_paired_win_rate_when_a_benchmark_ran() -> None:
    html = render_html(build_payload(_significance_doc(benchmark=True)))
    assert "Beats benchmark" in html
    assert "Observed edge" in html


def test_significance_panel_shows_the_trial_deflation() -> None:
    html = render_html(build_payload(_significance_doc(trials=24)))
    assert "Trials" in html
    assert "Null best Sharpe" in html
    assert "Deflated P" in html
    assert "not distinguishable from the best of that many skill-free runs" in html


def test_significance_panel_says_so_plainly_when_no_bootstrap_ran() -> None:
    doc = _benchmark_doc(_STRATEGY_CURVE, _BENCHMARK_CURVE)
    assert doc["significance"] is None
    html = render_html(build_payload(doc))
    assert "No bootstrap was run for this result" in html


def test_significance_panel_is_absent_for_a_pre_adr_0039_document() -> None:
    """A result.json written before this feature has no key; say nothing about it."""
    doc = _benchmark_doc(_STRATEGY_CURVE, _BENCHMARK_CURVE)
    del doc["significance"]
    html = render_html(build_payload(doc))
    assert "Sharpe significance" not in html


def test_significance_notes_reach_the_page() -> None:
    html = render_html(build_payload(_significance_doc()))
    assert "LOWER BOUND" in html


def test_the_significance_block_never_leaks_into_the_flat_metric_grid() -> None:
    html = render_html(build_payload(_significance_doc()))
    assert "Sharpe Interval" not in html


def test_significance_panel_keeps_the_page_self_contained() -> None:
    html = render_html(build_payload(_significance_doc(benchmark=True, trials=24)))
    assert "http://" not in html
    assert "https://" not in html
