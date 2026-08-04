"""Render a normalized payload into one self-contained HTML file (ADR-0023).

Pure standard library — **no third-party imports** and, by strict requirement,
**no external/CDN references** of any kind: all CSS and JavaScript are inlined,
the equity chart is an inline ``<svg>`` computed by :mod:`trading.dashboard.payload`,
and the run document is embedded as a ``<script type="application/json">`` block.
The result opens over ``file://`` with no network and is the load-bearing artifact
the fast test layer guards.

:func:`render_html` returns the document as a string; :func:`write_html` writes it.
The page renders entirely server-side (Python), so it is fully legible with
JavaScript disabled; the one small inline script only exposes the parsed run data
on ``window`` for ad-hoc inspection.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# Plot margins around the payload's user-space chart box, leaving room for the
# y-axis value labels (left) and the date labels (bottom).
_MARGIN_LEFT = 64.0
_MARGIN_RIGHT = 16.0
_MARGIN_TOP = 16.0
_MARGIN_BOTTOM = 28.0

# Which metric fields read as percentages (x100) vs. plain ratios. Anything a
# future metrics change adds falls through to the ratio format automatically.
_PERCENT_METRICS = frozenset(
    {
        "total_return",
        "annualized_return",
        "max_drawdown",
        "win_rate",
        "turnover",
        "avg_exposure",
        "peak_exposure",
    }
)

_METRIC_LABELS: dict[str, str] = {
    "total_return": "Total return",
    "annualized_return": "Annualized",
    "sharpe": "Sharpe",
    "sortino": "Sortino",
    "calmar": "Calmar",
    "max_drawdown": "Max drawdown",
    "win_rate": "Win rate",
    "turnover": "Turnover",
    "avg_exposure": "Avg exposure",
    "peak_exposure": "Peak exposure",
    "trade_count": "Trades (entries)",
    "trades_per_parameter": "Trades / parameter",
}

# Metrics that are plain counts, not ratios or percentages.
_COUNT_METRICS = frozenset({"trade_count"})


def _esc(value: Any) -> str:
    """HTML-escape any value's string form (quotes included)."""
    return html.escape(str(value), quote=True)


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return _esc(value)


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return _esc(value)


def _ratio(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return _esc(value)


def _num(value: Any) -> str:
    """Compact number for table cells: drop noise, keep up to 6 significant places."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return _esc(value)


def _embed_json(document: dict[str, Any]) -> str:
    """Serialize the document for a ``<script type="application/json">`` block.

    ``<`` / ``>`` / ``&`` are escaped to their ``\\uXXXX`` forms so no ``</script>``
    or comment sequence in the data can break out of the script element. The result
    is still valid JSON that ``JSON.parse`` reads back unchanged.
    """
    raw = json.dumps(document, indent=2)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _metrics_panel(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return '<p class="muted">No metrics recorded for this run.</p>'
    tiles: list[str] = []
    # Known metrics first (stable order), then any unrecognized extras.
    ordered = list(_METRIC_LABELS) + [k for k in metrics if k not in _METRIC_LABELS]
    for key in ordered:
        if key not in metrics:
            continue
        label = _METRIC_LABELS.get(key, key.replace("_", " ").title())
        value = metrics[key]
        if value is None:
            # An absent metric (e.g. trades-per-parameter on a strategy with no
            # tunable knobs) reads as "not applicable", never as a zero score.
            rendered = "n/a"
        elif key in _PERCENT_METRICS:
            rendered = _pct(value)
        elif key in _COUNT_METRICS:
            rendered = _num(value)
        else:
            rendered = _ratio(value)
        tiles.append(
            f'<div class="tile"><div class="tile-label">{_esc(label)}</div>'
            f'<div class="tile-value">{_esc(rendered)}</div></div>'
        )
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _chart_svg(chart: dict[str, Any]) -> str:
    width = float(chart["width"])
    height = float(chart["height"])
    view_w = width + _MARGIN_LEFT + _MARGIN_RIGHT
    view_h = height + _MARGIN_TOP + _MARGIN_BOTTOM

    equity_points = chart.get("equity_points") or ""
    if not equity_points:
        return '<p class="muted">No equity curve to plot.</p>'

    parts: list[str] = [
        f'<svg class="chart" viewBox="0 0 {view_w:g} {view_h:g}" '
        f'role="img" aria-label="Equity curve" preserveAspectRatio="none">',
        f'<g transform="translate({_MARGIN_LEFT:g},{_MARGIN_TOP:g})">',
        # plot frame
        f'<rect class="frame" x="0" y="0" width="{width:g}" height="{height:g}" />',
    ]
    if chart.get("has_benchmark") and chart.get("benchmark_points"):
        parts.append(f'<polyline class="benchmark" points="{_esc(chart["benchmark_points"])}" />')
    parts.append(f'<polyline class="equity" points="{_esc(equity_points)}" />')
    parts.append("</g>")

    # y-axis value labels (top = max, bottom = min).
    y_max = _money(chart.get("y_max"))
    y_min = _money(chart.get("y_min"))
    parts.append(
        f'<text class="axis" x="{_MARGIN_LEFT - 6:g}" y="{_MARGIN_TOP + 4:g}" '
        f'text-anchor="end">{_esc(y_max)}</text>'
    )
    parts.append(
        f'<text class="axis" x="{_MARGIN_LEFT - 6:g}" y="{_MARGIN_TOP + height:g}" '
        f'text-anchor="end">{_esc(y_min)}</text>'
    )
    # x-axis date labels (start / end).
    start_ts = chart.get("start_ts")
    end_ts = chart.get("end_ts")
    baseline = _MARGIN_TOP + height + 18.0
    if start_ts:
        parts.append(
            f'<text class="axis" x="{_MARGIN_LEFT:g}" y="{baseline:g}" '
            f'text-anchor="start">{_esc(start_ts)}</text>'
        )
    if end_ts:
        parts.append(
            f'<text class="axis" x="{_MARGIN_LEFT + width:g}" y="{baseline:g}" '
            f'text-anchor="end">{_esc(end_ts)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{_esc(empty)}</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body_rows: list[str] = []
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def _side_badge(side: Any) -> str:
    text = _esc(side)
    cls = "buy" if str(side).lower() == "buy" else "sell"
    return f'<span class="side {cls}">{text}</span>'


def _fills_table(fills: list[dict[str, Any]]) -> str:
    rows = [
        [
            _esc(fill.get("ts")),
            _esc(fill.get("symbol")),
            _side_badge(fill.get("side")),
            _num(fill.get("qty")),
            _money(fill.get("price")),
            _money(fill.get("commission")),
        ]
        for fill in fills
    ]
    return _table(
        ["Timestamp", "Symbol", "Side", "Qty", "Price", "Commission"],
        rows,
        "No fills in this run.",
    )


def _clamps_table(clamps: list[dict[str, Any]]) -> str:
    rows = [
        [
            _esc(clamp.get("symbol")),
            _side_badge(clamp.get("side")),
            _num(clamp.get("original_qty")),
            _num(clamp.get("clamped_qty")),
            _esc(clamp.get("reason")),
        ]
        for clamp in clamps
    ]
    return _table(
        ["Symbol", "Side", "Original qty", "Clamped qty", "Reason"],
        rows,
        "No orders were clamped.",
    )


def _rejections_table(rejections: list[dict[str, Any]]) -> str:
    rows = [
        [
            _esc(rej.get("symbol")),
            _side_badge(rej.get("side")),
            _num(rej.get("qty")),
            _esc(rej.get("reason")),
        ]
        for rej in rejections
    ]
    return _table(
        ["Symbol", "Side", "Qty", "Reason"],
        rows,
        "No orders were rejected.",
    )


def _halt_banner(halt: dict[str, Any]) -> str:
    if not halt or not halt.get("halted"):
        return ""
    ts = halt.get("halt_ts")
    reason = halt.get("halt_reason") or "new entries halted"
    when = f" at {_esc(ts)}" if ts else ""
    return (
        '<div class="halt-banner" role="alert">'
        f"<strong>Kill switch fired{when}.</strong> {_esc(reason)} — "
        "new entries blocked while the halt latched (exits still allowed)."
        "</div>"
    )


def _header(document: dict[str, Any]) -> str:
    symbols = ", ".join(str(s) for s in document.get("symbols", []))
    facts = [
        ("Mode", _esc(document.get("mode"))),
        ("Frequency", _esc(document.get("frequency"))),
        ("Symbols", _esc(symbols)),
        ("Starting cash", _esc(_money(document.get("starting_cash")))),
        ("Final equity", _esc(_money(document.get("final_equity")))),
        ("Total return", _esc(_pct(document.get("total_return")))),
    ]
    items = "".join(
        f'<div class="fact"><span class="fact-label">{label}</span>'
        f'<span class="fact-value">{value}</span></div>'
        for label, value in facts
    )
    return f'<div class="facts">{items}</div>'


_STYLE = """
:root {
  --bg: #f7f8fa; --panel: #ffffff; --ink: #1a1d24; --muted: #667085;
  --border: #e3e6ec; --accent: #2f6feb; --benchmark: #8a94a6;
  --buy: #128a5a; --sell: #c0392b; --halt-bg: #fdecec; --halt-ink: #a01c1c;
  --frame: #eceef2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1218; --panel: #171b22; --ink: #e7eaf0; --muted: #99a2b2;
    --border: #262c37; --accent: #5b8dff; --benchmark: #6c7686;
    --buy: #35c98a; --sell: #ff6b5e; --halt-bg: #351a1a; --halt-ink: #ff8f86;
    --frame: #1f242d;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1040px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 0 0 12px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 18px; margin-bottom: 20px;
}
.facts { display: flex; flex-wrap: wrap; gap: 18px 32px; }
.fact { display: flex; flex-direction: column; }
.fact-label {
  color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em;
}
.fact-value { font-size: 1.02rem; font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.tile { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile-label { color: var(--muted); font-size: 0.78rem; }
.tile-value { font-size: 1.15rem; font-weight: 650; margin-top: 4px; }
.chart { width: 100%; height: auto; display: block; }
.chart .frame { fill: var(--frame); stroke: var(--border); stroke-width: 1; }
.chart .equity {
  fill: none; stroke: var(--accent); stroke-width: 2; vector-effect: non-scaling-stroke;
}
.chart .benchmark {
  fill: none; stroke: var(--benchmark); stroke-width: 1.5;
  stroke-dasharray: 5 4; vector-effect: non-scaling-stroke;
}
.chart .axis { fill: var(--muted); font-size: 11px; }
.legend { display: flex; gap: 18px; margin-top: 8px; color: var(--muted); font-size: 0.85rem; }
.legend .swatch {
  display: inline-block; width: 22px; height: 0; border-top: 3px solid var(--accent);
  vertical-align: middle; margin-right: 6px;
}
.legend .swatch.bench { border-top: 3px dashed var(--benchmark); }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
th, td {
  text-align: left; padding: 7px 12px; border-bottom: 1px solid var(--border); white-space: nowrap;
}
th {
  color: var(--muted); font-weight: 600; font-size: 0.8rem;
  text-transform: uppercase; letter-spacing: 0.02em;
}
.side { font-weight: 600; }
.side.buy { color: var(--buy); }
.side.sell { color: var(--sell); }
.muted { color: var(--muted); }
.halt-banner {
  background: var(--halt-bg); color: var(--halt-ink); border: 1px solid var(--halt-ink);
  border-radius: 10px; padding: 12px 16px; margin-bottom: 20px;
}
"""


def render_html(payload: dict[str, Any]) -> str:
    """Build the complete, self-contained HTML document for a normalized payload.

    ``payload`` is the mapping :func:`trading.dashboard.payload.build_payload`
    returns: the raw run ``document`` plus a precomputed ``chart`` block. The
    returned string is a full ``<!doctype html>`` document with inline styles,
    inline script, the embedded run JSON, and an inline-SVG equity chart — no
    external references.
    """
    document: dict[str, Any] = payload["document"]
    chart: dict[str, Any] = payload["chart"]

    symbols = ", ".join(str(s) for s in document.get("symbols", [])) or "run"
    subtitle = f"{_esc(symbols)} · {_esc(document.get('mode'))} · {_esc(document.get('frequency'))}"
    legend = (
        '<div class="legend"><span><span class="swatch"></span>Strategy</span>'
        '<span><span class="swatch bench"></span>Benchmark</span></div>'
        if chart.get("has_benchmark")
        else '<div class="legend"><span><span class="swatch"></span>Strategy</span></div>'
    )

    body = f"""<div class="wrap">
<h1>Trading run dashboard</h1>
<p class="sub">{subtitle}</p>
{_halt_banner(document.get("halt", {}))}
<section class="panel">{_header(document)}</section>
<section class="panel">
  <h2>Equity curve</h2>
  {_chart_svg(chart)}
  {legend}
</section>
<section class="panel">
  <h2>Performance metrics</h2>
  {_metrics_panel(document.get("metrics"))}
</section>
<section class="panel">
  <h2>Fills</h2>
  {_fills_table(document.get("fills", []))}
</section>
<section class="panel">
  <h2>Guardrail actions</h2>
  <h3 class="muted" style="font-size:0.85rem;margin:0 0 8px;">Clamps</h3>
  {_clamps_table(document.get("clamps", []))}
  <h3 class="muted" style="font-size:0.85rem;margin:16px 0 8px;">Rejections</h3>
  {_rejections_table(document.get("rejections", []))}
</section>
</div>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Trading run dashboard — {_esc(symbols)}</title>
<style>{_STYLE}</style>
</head>
<body>
{body}
<script type="application/json" id="run-data">
{_embed_json(document)}
</script>
<script>
(function () {{
  var el = document.getElementById("run-data");
  try {{ window.RUN_DATA = JSON.parse(el.textContent); }}
  catch (e) {{ window.RUN_DATA = null; }}
}})();
</script>
</body>
</html>
"""


def write_html(payload: dict[str, Any], path: str | Path) -> Path:
    """Render ``payload`` and write the self-contained HTML to ``path``.

    Creates parent directories as needed and returns the written :class:`Path`.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(payload), encoding="utf-8")
    return out
