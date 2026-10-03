from __future__ import annotations

import json

from codeinsight.application.graph_builder import GraphModel
from codeinsight.presentation.graph_export import model_to_dict

# 外部リソースを一切読み込まない、自己完結型のビューアー。
# ラベルやパスは解析対象（信頼できない入力）由来のため、DOMへは textContent / setAttribute
# のみで反映し、innerHTML は使わない。
_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>CodeInsight グラフビューアー</title>
<style>
:root { color-scheme: light dark; --bg:#ffffff; --fg:#1f2937; --muted:#6b7280; --line:#9ca3af;
  --node:#eff6ff; --node-stroke:#2563eb; --warn:#b45309; --warn-bg:#fef3c7; --ext:#6b7280; --ext-bg:#f3f4f6;
  --panel:#f9fafb; --border:#e5e7eb; --hit:#fde68a; }
@media (prefers-color-scheme: dark) { :root { --bg:#111827; --fg:#e5e7eb; --muted:#9ca3af; --line:#6b7280;
  --node:#1e3a8a; --node-stroke:#60a5fa; --warn:#fbbf24; --warn-bg:#451a03; --ext:#9ca3af; --ext-bg:#1f2937;
  --panel:#1f2937; --border:#374151; --hit:#854d0e; } }
body { margin:0; font:14px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header { padding:10px 16px; border-bottom:1px solid var(--border); }
h1 { font-size:16px; margin:0 0 4px; }
.notes { color:var(--muted); font-size:12px; margin:0; }
main { display:flex; height:calc(100vh - 92px); min-height:320px; }
#canvas { flex:1; overflow:auto; }
aside { width:300px; border-left:1px solid var(--border); background:var(--panel); padding:12px; overflow:auto; }
.toolbar { padding:8px 16px; border-bottom:1px solid var(--border); display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
input[type=search] { padding:4px 8px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--fg); }
.legend span { display:inline-flex; align-items:center; gap:4px; margin-right:10px; font-size:12px; color:var(--muted); }
.legend svg { width:28px; height:8px; }
.node rect { fill:var(--node); stroke:var(--node-stroke); }
.node.unresolved rect { fill:var(--warn-bg); stroke:var(--warn); stroke-dasharray:4 3; }
.node.external rect { fill:var(--ext-bg); stroke:var(--ext); stroke-dasharray:4 3; }
.node.hit rect { fill:var(--hit); }
.node.selected rect { stroke-width:3; }
.node.faded { opacity:0.25; }
button { padding:3px 10px; border:1px solid var(--border); border-radius:4px; background:var(--bg); color:var(--fg); cursor:pointer; }
button:hover { background:var(--panel); }
.node text { fill:var(--fg); font-size:12px; pointer-events:none; }
.node { cursor:pointer; }
.edge { fill:none; stroke:var(--line); stroke-width:1.4; }
.edge.inferred { stroke-dasharray:6 4; }
.edge.unresolved { stroke:var(--warn); stroke-dasharray:2 4; }
.edge.external { stroke:var(--ext); stroke-dasharray:2 4; }
.edge.dim { opacity:0.12; }
dt { color:var(--muted); font-size:12px; margin-top:8px; }
dd { margin:0; word-break:break-all; white-space:pre-wrap; }
@media (max-width: 640px) { main { flex-direction:column; height:auto; } aside { width:auto; border-left:none; border-top:1px solid var(--border); } #canvas { height:60vh; } }
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <p class="notes" id="notes"></p>
</header>
<div class="toolbar">
  <input id="filter" type="search" placeholder="ノードを検索" aria-label="ノードを検索">
  <span>
    <button id="zoom-out" type="button" aria-label="縮小">−</button>
    <button id="zoom-in" type="button" aria-label="拡大">＋</button>
    <button id="zoom-fit" type="button">全体表示</button>
  </span>
  <span class="legend" id="legend"></span>
  <span id="stats" class="notes"></span>
</div>
<main>
  <div id="canvas"><svg id="graph" role="img" aria-label="グラフ"></svg></div>
  <aside id="detail"><p class="notes">ノードまたは辺をクリックすると詳細を表示します。</p></aside>
</main>
<script id="graph-data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("graph-data").textContent);
  var NS = "http://www.w3.org/2000/svg";
  var NODE_W = 230, NODE_H = 30, GAP_X = 90, GAP_Y = 18, PAD = 24;

  document.getElementById("title").textContent = data.title;
  document.getElementById("notes").textContent = data.notes.join(" ");
  document.getElementById("stats").textContent = "ノード " + data.nodes.length + " / 辺 " + data.edges.length;

  var legend = document.getElementById("legend");
  [["confirmed", "確定"], ["inferred", "推定"], ["unresolved", "未解決"], ["external", "外部"]].forEach(function (item) {
    var span = document.createElement("span");
    var svg = document.createElementNS(NS, "svg");
    var line = document.createElementNS(NS, "line");
    line.setAttribute("x1", "0"); line.setAttribute("x2", "28"); line.setAttribute("y1", "4"); line.setAttribute("y2", "4");
    line.setAttribute("class", "edge " + item[0]);
    svg.appendChild(line); span.appendChild(svg);
    span.appendChild(document.createTextNode(item[1]));
    legend.appendChild(span);
  });

  var ids = {};
  data.nodes.forEach(function (n, i) { ids[n.id] = i; });
  var edges = data.edges.filter(function (e) { return e.source in ids && e.target in ids; });

  // 層の割り当て: 深さ優先で逆向きの辺(閉路)を除き、最長経路で層を決める。
  var out = data.nodes.map(function () { return []; });
  edges.forEach(function (e) { out[ids[e.source]].push(ids[e.target]); });
  var state = data.nodes.map(function () { return 0; });
  var dag = data.nodes.map(function () { return []; });
  var indegree = data.nodes.map(function () { return 0; });
  data.nodes.forEach(function (_, start) {
    if (state[start]) return;
    var stack = [[start, 0]]; state[start] = 1;
    while (stack.length) {
      var top = stack[stack.length - 1];
      if (top[1] < out[top[0]].length) {
        var next = out[top[0]][top[1]++];
        if (state[next] === 0) { state[next] = 1; dag[top[0]].push(next); indegree[next]++; stack.push([next, 0]); }
        else if (state[next] === 2) { dag[top[0]].push(next); indegree[next]++; }
      } else { state[top[0]] = 2; stack.pop(); }
    }
  });
  var rank = data.nodes.map(function () { return 0; });
  var queue = [];
  indegree.forEach(function (d, i) { if (d === 0) queue.push(i); });
  while (queue.length) {
    var current = queue.shift();
    dag[current].forEach(function (next) {
      rank[next] = Math.max(rank[next], rank[current] + 1);
      if (--indegree[next] === 0) queue.push(next);
    });
  }
  var columns = {};
  data.nodes.forEach(function (n, i) { (columns[rank[i]] = columns[rank[i]] || []).push(i); });
  var pos = [], maxX = 0, maxY = 0;
  Object.keys(columns).forEach(function (r) {
    columns[r].forEach(function (i, row) {
      pos[i] = { x: PAD + r * (NODE_W + GAP_X), y: PAD + row * (NODE_H + GAP_Y) };
      maxX = Math.max(maxX, pos[i].x + NODE_W + 50); maxY = Math.max(maxY, pos[i].y + NODE_H);
    });
  });

  var svg = document.getElementById("graph");
  var contentW = maxX + PAD, contentH = maxY + PAD, scale = 1;
  var canvas = document.getElementById("canvas");
  svg.setAttribute("viewBox", "0 0 " + contentW + " " + contentH);
  function applyScale(value) {
    scale = Math.min(3, Math.max(0.05, value));
    svg.setAttribute("width", Math.round(contentW * scale));
    svg.setAttribute("height", Math.round(contentH * scale));
  }
  function fitScale() { return Math.min(1, (canvas.clientWidth - 8) / contentW); }
  function fit() { applyScale(fitScale()); }
  document.getElementById("zoom-in").addEventListener("click", function () { applyScale(scale * 1.25); });
  document.getElementById("zoom-out").addEventListener("click", function () { applyScale(scale / 1.25); });
  document.getElementById("zoom-fit").addEventListener("click", fit);
  canvas.addEventListener("wheel", function (ev) {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    ev.preventDefault();
    applyScale(scale * (ev.deltaY < 0 ? 1.1 : 1 / 1.1));
  }, { passive: false });
  applyScale(Math.max(0.7, fitScale()));  // 初期表示は文字が読める倍率にし、全体表示はボタンで
  var defs = document.createElementNS(NS, "defs");
  var marker = document.createElementNS(NS, "marker");
  marker.setAttribute("id", "arrow"); marker.setAttribute("viewBox", "0 0 10 10"); marker.setAttribute("refX", "9");
  marker.setAttribute("refY", "5"); marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7");
  marker.setAttribute("orient", "auto-start-reverse");
  var arrow = document.createElementNS(NS, "path");
  arrow.setAttribute("d", "M0,0 L10,5 L0,10 z"); arrow.setAttribute("fill", "#9ca3af");
  marker.appendChild(arrow); defs.appendChild(marker); svg.appendChild(defs);

  var detail = document.getElementById("detail");
  function row(dl, term, value) {
    if (value === null || value === undefined || value === "") return;
    var dt = document.createElement("dt"); dt.textContent = term;
    var dd = document.createElement("dd"); dd.textContent = String(value);
    dl.appendChild(dt); dl.appendChild(dd);
  }
  function show(title, rows) {
    detail.textContent = "";
    var h = document.createElement("h2"); h.style.fontSize = "14px"; h.textContent = title; detail.appendChild(h);
    var dl = document.createElement("dl");
    rows.forEach(function (r) { row(dl, r[0], r[1]); });
    detail.appendChild(dl);
  }
  var STYLE_LABEL = { confirmed: "確定", inferred: "推定", unresolved: "未解決", external: "外部" };

  var edgeEls = [];
  edges.forEach(function (e) {
    var a = pos[ids[e.source]], b = pos[ids[e.target]];
    var x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
    if (b.x <= a.x) { x1 = a.x; x2 = b.x + NODE_W; }
    var mid = (x1 + x2) / 2;
    var path = document.createElementNS(NS, "path");
    if (e.source === e.target) {
      // 自己再帰: ノードの右側に小さなループを描く
      var rx = a.x + NODE_W, ry = a.y;
      path.setAttribute("d", "M" + rx + "," + (ry + 8) + " C" + (rx + 42) + "," + (ry - 10) + " " + (rx + 42) + "," + (ry + NODE_H + 10) + " " + rx + "," + (ry + NODE_H - 8));
    } else {
      path.setAttribute("d", "M" + x1 + "," + y1 + " C" + mid + "," + y1 + " " + mid + "," + y2 + " " + x2 + "," + y2);
    }
    path.setAttribute("class", "edge " + e.style);
    path.setAttribute("marker-end", "url(#arrow)");
    path.style.cursor = "pointer";
    path.addEventListener("click", function () {
      show("辺: " + e.kind, [
        ["確からしさ", STYLE_LABEL[e.style] || e.style],
        ["呼び出し元/依存元", data.nodes[ids[e.source]].label],
        ["呼び出し先/依存先", data.nodes[ids[e.target]].label],
        ["回数", e.count],
        ["根拠位置", e.evidence.join("\\n")],
        ["注記", e.note]
      ]);
    });
    svg.appendChild(path);
    edgeEls.push({ el: path, e: e });
  });

  var nodeEls = [];
  var selected = null;
  data.nodes.forEach(function (n, i) {
    var g = document.createElementNS(NS, "g");
    g.setAttribute("class", "node " + n.kind);
    g.setAttribute("transform", "translate(" + pos[i].x + "," + pos[i].y + ")");
    var rect = document.createElementNS(NS, "rect");
    rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H); rect.setAttribute("rx", "5");
    var text = document.createElementNS(NS, "text");
    text.setAttribute("x", "8"); text.setAttribute("y", "19");
    // 修飾名は末尾（関数名側）のほうが識別しやすいので、長い場合は先頭を省略する。
    var label = n.label.length > 32 ? "…" + n.label.slice(-31) : n.label;
    text.textContent = label;
    var title = document.createElementNS(NS, "title"); title.textContent = n.label;
    g.appendChild(title); g.appendChild(rect); g.appendChild(text);
    g.addEventListener("click", function () {
      if (selected) selected.classList.remove("selected");
      selected = g; g.classList.add("selected");
      var related = edges.filter(function (e) { return e.source === n.id || e.target === n.id; });
      var near = {}; near[n.id] = true;
      related.forEach(function (e) { near[e.source] = true; near[e.target] = true; });
      edgeEls.forEach(function (x) { x.el.classList.toggle("dim", related.indexOf(x.e) < 0); });
      nodeEls.forEach(function (x) { x.el.classList.toggle("faded", !near[x.n.id]); });
      show(n.label, [
        ["種別", n.kind],
        ["場所", n.path ? n.path + (n.line ? ":" + n.line : "") : ""],
        ["出る辺", edges.filter(function (e) { return e.source === n.id; }).length],
        ["入る辺", edges.filter(function (e) { return e.target === n.id; }).length]
      ]);
    });
    svg.appendChild(g);
    nodeEls.push({ el: g, n: n });
  });

  document.getElementById("filter").addEventListener("input", function (ev) {
    var q = ev.target.value.toLowerCase();
    var first = null;
    nodeEls.forEach(function (x, i) {
      var hit = q !== "" && x.n.label.toLowerCase().indexOf(q) >= 0;
      x.el.classList.toggle("hit", hit);
      if (hit && first === null) first = i;
    });
    if (first !== null) {
      canvas.scrollTo(Math.max(0, pos[first].x * scale - 40), Math.max(0, pos[first].y * scale - 40));
    }
  });
  svg.addEventListener("click", function (ev) {
    if (ev.target === svg) {
      edgeEls.forEach(function (x) { x.el.classList.remove("dim"); });
      nodeEls.forEach(function (x) { x.el.classList.remove("faded"); });
      if (selected) selected.classList.remove("selected");
      selected = null;
    }
  });
})();
</script>
</body>
</html>
"""


def _embed(model: GraphModel) -> str:
    """JSONをscriptタグ内へ安全に埋め込む（`</script>`等で抜け出せないようにする）。"""

    payload = json.dumps(model_to_dict(model), ensure_ascii=False)
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_html(model: GraphModel) -> str:
    return _TEMPLATE.replace("__DATA__", _embed(model))
