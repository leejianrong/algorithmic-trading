"""Render a live-session payload into a self-polling HTML page (ADR-0075).

This is the live counterpart to :mod:`trading.dashboard.static_export`: instead
of one finished ``result.json`` rendered once, this page's whole point is to
keep reflecting a session that is still changing. It embeds an initial
snapshot exactly the way :func:`static_export.render_html` embeds ``RUN_DATA``
(so the first paint needs no round trip), then polls ``GET /api/live`` — the
route :func:`trading.dashboard.server.create_live_app` adds — every few
seconds and re-renders in place.

Unlike the static export, **this page is meaningless without JavaScript**: a
live view that cannot poll is just a stale screenshot, so there is no
server-side-only fallback here. All rendering — the initial paint and every
subsequent poll — goes through the *same* small inline JavaScript function, so
there is exactly one templating language to keep in sync with
:mod:`trading.dashboard.live_payload`'s payload shape, not two.

CSS is reused verbatim from :mod:`trading.dashboard.static_export` (``_STYLE``)
so the live view matches the finished-run dashboard's look — same panel/tile/
table/chart classes, same light/dark handling — with a small addition for the
states only a live page has (a halted/running badge, a lost-connection banner).
"""

from __future__ import annotations

import html
import json
from typing import Any

from trading.dashboard.static_export import _STYLE

# How often the page re-fetches `/api/live`. Every read is a handful of small
# local file reads (`trading.dashboard.live_payload`), so this errs toward
# "responsive" rather than "gentle on a server" — there is exactly one client.
POLL_INTERVAL_MS = 5000

_EXTRA_STYLE = """
.conn-banner {
  background: var(--halt-bg); color: var(--halt-ink); border: 1px solid var(--halt-ink);
  border-radius: 8px; padding: 8px 14px; margin-bottom: 14px; font-size: 0.85rem;
}
.badge {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 0.78rem; font-weight: 600;
}
.badge.ok { background: rgba(18,138,90,0.15); color: var(--buy); }
.badge.halted { background: rgba(192,57,43,0.15); color: var(--sell); }
pre.log {
  white-space: pre-wrap; word-break: break-word; font-size: 0.82rem;
  max-height: 360px; overflow-y: auto; margin: 0;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}
"""


def _embed_json(payload: dict[str, Any]) -> str:
    """Serialize ``payload`` for a ``<script type="application/json">`` block.

    Same escaping :func:`static_export._embed_json` uses: ``<``/``>``/``&`` are
    replaced with their ``\\uXXXX`` forms so no ``</script>`` sequence in a log
    line or symbol can break out of the element, while the result is still
    valid JSON ``JSON.parse`` reads back unchanged.
    """
    raw = json.dumps(payload, indent=2)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _render_script(poll_interval_ms: int) -> str:
    """The one inline script that renders both the initial paint and every poll."""
    return (
        f"var POLL_INTERVAL_MS = {poll_interval_ms};\n"
        + r"""
function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
  });
}
function money(v) {
  if (v === null || v === undefined || isNaN(v)) { return "n/a"; }
  return "$" + Number(v).toLocaleString(
    undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}
  );
}
function pct(v, digits) {
  if (v === null || v === undefined || isNaN(v)) { return "n/a"; }
  return (Number(v) * 100).toFixed(digits === undefined ? 1 : digits) + "%";
}
function num(v, digits) {
  if (v === null || v === undefined || isNaN(v)) { return "n/a"; }
  return Number(v).toFixed(digits === undefined ? 4 : digits);
}
function tile(label, value) {
  return '<div class="tile"><div class="tile-label">' + esc(label) +
    '</div><div class="tile-value">' + esc(value) + '</div></div>';
}
function renderPositions(positions) {
  var symbols = Object.keys(positions || {});
  if (symbols.length === 0) {
    return '<p class="muted">No open positions.</p>';
  }
  var rows = symbols.map(function (sym) {
    var pos = positions[sym] || {};
    return "<tr><td>" + esc(sym) + "</td><td>" + num(pos.qty, 6) + "</td><td>" +
      esc(money(pos.avg_price)) + "</td></tr>";
  }).join("");
  return '<div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Qty</th>' +
    "<th>Avg price</th></tr></thead><tbody>" + rows + "</tbody></table></div>";
}
function renderChart(chart) {
  var points = chart && chart.equity_points;
  if (!points) {
    return '<p class="muted">No completed bars yet.</p>';
  }
  var width = chart.width, height = chart.height;
  var marginLeft = 64, marginRight = 16, marginTop = 16, marginBottom = 28;
  var viewW = width + marginLeft + marginRight, viewH = height + marginTop + marginBottom;
  var svg = '<svg class="chart" viewBox="0 0 ' + viewW + " " + viewH +
    '" role="img" aria-label="Live equity curve" preserveAspectRatio="none">' +
    '<g transform="translate(' + marginLeft + "," + marginTop + ')">' +
    '<rect class="frame" x="0" y="0" width="' + width + '" height="' + height + '" />' +
    '<polyline class="equity" points="' + esc(points) + '" /></g>' +
    '<text class="axis" x="' + (marginLeft - 6) + '" y="' + (marginTop + 4) +
    '" text-anchor="end">' + esc(money(chart.y_max)) + "</text>" +
    '<text class="axis" x="' + (marginLeft - 6) + '" y="' + (marginTop + height) +
    '" text-anchor="end">' + esc(money(chart.y_min)) + "</text>";
  if (chart.start_ts) {
    svg += '<text class="axis" x="' + marginLeft + '" y="' + (marginTop + height + 18) +
      '" text-anchor="start">' + esc(chart.start_ts) + "</text>";
  }
  if (chart.end_ts) {
    svg += '<text class="axis" x="' + (marginLeft + width) + '" y="' + (marginTop + height + 18) +
      '" text-anchor="end">' + esc(chart.end_ts) + "</text>";
  }
  svg += "</svg>";
  return svg;
}
function renderDivergence(d) {
  if (!d) {
    return '<section class="panel"><h2>Fill divergence</h2>' +
      '<p class="muted">This session was not run with --divergence, so there is ' +
      "nothing to compare live fills against.</p></section>";
  }
  var tiles = tile("Rows", d.row_count) + tile("Paired fills", d.paired_count) +
    tile("Mean realized (bps)", num(d.mean_realized_slippage_bps, 2)) +
    tile("Mean modelled (bps)", num(d.mean_modelled_slippage_bps, 2)) +
    tile("Median realized (bps)", num(d.median_realized_slippage_bps, 2));
  var note = d.sufficient_sample
    ? ""
    : '<p class="warn">Below ' + d.min_paired_fills + " paired fills (" + d.paired_count +
      " so far) &mdash; not enough yet to say anything about the cost model.</p>";
  return '<section class="panel"><h2>Fill divergence</h2><div class="tiles">' + tiles +
    "</div>" + note + "</section>";
}
function renderLogTail(lines) {
  if (!lines || lines.length === 0) {
    return '<p class="muted">No bars logged yet.</p>';
  }
  return '<pre class="log">' + esc(lines.join("\n")) + "</pre>";
}
function renderApp(data) {
  var state = data.state;
  var out = "";
  if (!state) {
    out += '<section class="panel"><p class="muted">Waiting for the session&#39;s first ' +
      "completed bar (paper_state.json has not been written yet).</p></section>";
  } else {
    var badge = state.halted
      ? '<span class="badge halted">HALTED</span>'
      : '<span class="badge ok">RUNNING</span>';
    out += '<section class="panel"><div class="facts">' +
      '<div class="fact"><span class="fact-label">Status</span><span class="fact-value">' +
      badge + "</span></div>" +
      '<div class="fact"><span class="fact-label">As of</span><span class="fact-value">' +
      esc(state.ts) + "</span></div>" +
      '<div class="fact"><span class="fact-label">Equity</span><span class="fact-value">' +
      esc(money(state.equity)) + "</span></div>" +
      '<div class="fact"><span class="fact-label">Exposure</span><span class="fact-value">' +
      esc(pct(state.exposure)) + "</span></div>" +
      '<div class="fact"><span class="fact-label">Cash</span><span class="fact-value">' +
      esc(money(state.cash)) + "</span></div>" +
      "</div></section>";
    out += '<section class="panel"><h2>Positions</h2>' +
      renderPositions(state.positions) + "</section>";
  }
  out += '<section class="panel"><h2>Equity curve</h2>' +
    renderChart(data.chart) + "</section>";
  out += '<section class="panel"><h2>Recent bars</h2>' +
    renderLogTail(data.log_tail) + "</section>";
  out += renderDivergence(data.divergence);
  document.getElementById("app").innerHTML = out;
  document.getElementById("subtitle").textContent = data.out_dir || "";
}
function showConnLost() {
  document.getElementById("conn-banner").innerHTML =
    '<div class="conn-banner">Lost contact with the dashboard server ' +
    "&mdash; retrying&hellip;</div>";
}
function clearConnBanner() {
  document.getElementById("conn-banner").innerHTML = "";
}
function poll() {
  fetch("/api/live")
    .then(function (r) {
      if (!r.ok) { throw new Error("bad status " + r.status); }
      return r.json();
    })
    .then(function (data) { clearConnBanner(); renderApp(data); })
    .catch(function () { showConnLost(); });
}
(function () {
  var el = document.getElementById("live-data");
  var initial = null;
  try { initial = JSON.parse(el.textContent); } catch (e) { initial = null; }
  if (initial) { renderApp(initial); }
  setInterval(poll, POLL_INTERVAL_MS);
})();
"""
    )


def render_live_html(payload: dict[str, Any]) -> str:
    """Build the live-session HTML page for an initial ``payload`` snapshot.

    ``payload`` is whatever :func:`trading.dashboard.live_payload.build_live_payload`
    returns. The page embeds it for the first paint, then polls
    ``GET /api/live`` every :data:`POLL_INTERVAL_MS` and re-renders through the
    same JavaScript function — see the module docstring for why there is no
    no-JavaScript fallback here, unlike the finished-run static export.
    """
    out_dir = html.escape(str(payload.get("out_dir", "")), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Live paper session dashboard</title>
<style>{_STYLE}{_EXTRA_STYLE}</style>
</head>
<body>
<div class="wrap">
<h1>Live paper session</h1>
<p class="sub" id="subtitle">{out_dir}</p>
<div id="conn-banner"></div>
<div id="app">Loading&hellip;</div>
</div>
<script type="application/json" id="live-data">
{_embed_json(payload)}
</script>
<script>
{_render_script(POLL_INTERVAL_MS)}
</script>
</body>
</html>
"""
