"""Fast, no-infra tests for the self-contained static HTML export.

Asserts the rendered document embeds the run JSON, draws an inline ``<svg>``,
surfaces the metric values / a fills row / guardrail actions, shows the halt
banner for a halted run, and — the load-bearing offline guarantee — contains NO
``http://`` / ``https://`` external reference. Assertions are on substrings and
structure, never brittle full-HTML equality. No engine, no network, no FastAPI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading.dashboard.payload import build_payload
from trading.dashboard.static_export import render_html, write_html
from trading.engine import BacktestResult, EquityPoint, HaltEpisode
from trading.metrics import PerformanceMetrics
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
